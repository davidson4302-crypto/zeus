"""urgentfallback: an urgent text to the owner that did not go gets one repair alarm and ONE email.

When it acts. notifier.py calls in here at the three places where it learns that a note did not reach the owner:
  - the texting line answered "failed" (notifier._send -> 'failed'),
  - a note with no receipt was read back in their Messages as never arrived (notifier._settle). The notifier sends that
    text again in the same run, so here the email waits one run and goes only if they still were not reached,
  - a note with no receipt still cannot be confirmed either way after 30 minutes (notifier._settle; the notifier and
    integrity.never_assumed already raise ONE repair ticket for that note, so no second ticket is raised here).
Only items the notifier itself treats as urgent count (their `urgency` is "now", the same test the notifier uses for
"urgent items never wait"). A note with no urgent item: nothing happens here, exactly as before.

What it does, each exactly once per item:
  (a) one repair alarm through worker._alarm (the path every other engine alarm takes). ONE
      alarm per failed note (`life:<note key>`), whatever number of urgent items the note carried;
  (b) ONE email from Zeus's own inbox to the owner's alert inbox (sendmail.alert), carrying the words of the text.

One send, the way onesend.py keeps it:
  - the item's mark and the email's record are saved and committed BEFORE the ticket or the email is attempted, so a
    restart finds them and starts nothing a second time;
  - the email record is "sending" before the attempt; a "sending" nobody is left to finish becomes "unknown";
  - an "unknown" email is settled ONLY by reading the destination, the owner's alert inbox (sendmail.alert_arrived):
    found = sent; the inbox could not be read = still unknown, nothing is sent; read twice, the last time ten minutes
    or more after the attempt, and it is not there = it did not go, and only then may it go once more;
  - a clear "no" from the mail service is tried again on a later run, three times in all, then it stops (the ticket
    is already open for the fixer).

Quiet hours. All fallback messages wait from 9pm until 9am on the owner clock.

Three more stops:
  - at most MAILS_PER_DAY stand-in emails are attempted in one day; past that the record waits (the ticket is open);
  - while Zeus is parked (STOP, ESTOP, PAUSED or gateway.parked in its profile, sendmail.alert_parked) nothing is
    sent and the record waits;
  - tick.py never hands the noticer an email whose sender is Zeus's own inbox, so the stand-in email is never read
    back as new urgent mail.

Where it is kept: the ledger's `meta` table (the one commit.py makes), one row per item (`urgent_fallback:<item>`) and
one per email (`urgent_fallback_mail:<ref>`). No new table. Switch file `urgent-fallback-on` in the life folder;
without it every door here does nothing. Standard library only. No model is asked. Never raises into the notifier.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import settings

SWITCH = 'urgent-fallback-on'
META = 'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)'      # the same table commit.py makes
K_ITEM, K_MAIL = 'urgent_fallback:', 'urgent_fallback_mail:'
TICKET_KIND = 'life_urgent_text_failed'
SEND_PATIENCE = 5 * 60       # an email "sending" older than this has nobody left to finish it (onesend.SEND_PATIENCE)
SETTLE_WAIT = 10 * 60        # an unknown email is called "did not go" only after two reads, the last this long after the attempt (onesend.SETTLE_WAIT)
MAIL_TRIES = 3               # attempts in all after a clear "no" or a proven "did not go"
MAILS_PER_DAY = 5            # a breaker, like the notifier's for texts: stand-in emails attempted in one day (their clock); past it they wait
CLOCK = None                 # the real clock the notifier re-reads right before a text (notifier.deliver sets it on every run); None = this machine's clock
ENDED = ('sent', 'not_needed', 'gave_up')
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
WHY = {'failed': 'the texting line said it did not go',
       'never_arrived': 'it was read back in their Messages and is not there',
       'unconfirmed': 'its delivery could not be confirmed after 30 minutes'}


def on(life) -> bool:
    try:
        return (Path(life) / SWITCH).exists()
    except (OSError, TypeError):
        return False


# ---- small readers and writers ------------------------------------------------------------------------------------------

def _dt(now) -> datetime:
    from ownertime import PACIFIC          # the clock they are on right now
    if isinstance(now, datetime):
        return now.astimezone(PACIFIC) if now.tzinfo else now.replace(tzinfo=PACIFIC)
    return datetime.fromtimestamp(float(now if now is not None else time.time()), PACIFIC)


def _get(con, key):
    r = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    try:
        return json.loads(tuple(r)[0]) if r else None
    except (TypeError, ValueError):
        return None


def _put(con, key, rec: dict) -> None:
    con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key, json.dumps(rec, ensure_ascii=False, default=str)))
    con.commit()


def _mails(con) -> list:
    out = []
    for (k, v) in con.execute('SELECT key,value FROM meta WHERE key LIKE ? ORDER BY key', (K_MAIL + '%',)).fetchall():
        try:
            out.append(json.loads(v))
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda m: m.get('at') or 0)


def records(con) -> dict:
    """Everything this file has on record -> {'items': {item key: mark}, 'mails': [records]}. Read only (tests, the health look)."""
    con.execute(META)
    items = {}
    for (k, v) in con.execute('SELECT key,value FROM meta WHERE key LIKE ? AND key NOT LIKE ?', (K_ITEM + '%', K_MAIL + '%')).fetchall():
        try:
            items[k[len(K_ITEM):]] = json.loads(v)
        except (TypeError, ValueError):
            continue
    return {'items': items, 'mails': _mails(con)}


def _move(con, mail: dict, state: str, why: str, now: float, **more) -> dict:
    mail['state'] = state
    mail.setdefault('history', []).append({'state': state, 'at': now, 'why': str(why)[:300]} | {k: v for k, v in more.items() if v is not None})
    mail.update({k: v for k, v in more.items() if k == 'receipt' and v is not None})
    _put(con, K_MAIL + mail['ref'], mail)
    return mail


def _note(con, mail: dict, what: str, now: float, **more) -> None:
    mail.setdefault('history', []).append({'note': what, 'at': now} | more)
    _put(con, K_MAIL + mail['ref'], mail)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def quiet(life, now_dt: datetime) -> bool:
    from ownertime import alert_open, now
    real = _dt((CLOCK or now)())
    return any(alert_open(at, life=life) > at for at in (now_dt, real))


def unresolved(con, key: str, since: float, note=None) -> bool:
    """Is this item still owed to them? Yes while it is open (or was only marked told by a note whose delivery nobody
    could confirm) and neither the note this record is about (confirmed later by a read-back) nor any text that
    carried the item since the failure is on record as delivered. Closed on their word or by evidence, or gone from the
    list: no."""
    row = con.execute('SELECT state,payload FROM opps WHERE key=?', (key,)).fetchone()
    if row is None:
        return False
    try:
        p = json.loads(tuple(row)[1])
    except (TypeError, ValueError):
        return True
    if isinstance(p.get('closed'), dict):
        return False
    reached = con.execute("SELECT 1 FROM note_items n JOIN outbox o ON o.key=n.note WHERE n.opp=? AND o.mode='live' AND o.state='sent' AND (o.at>=? OR o.key=?) LIMIT 1",
                          (key, float(since) - 5, str(note or ''))).fetchone()
    return reached is None


def _attempts_today(con, now_dt: datetime) -> int:
    """Stand-in emails handed to the mail service today, on their clock (every "sending" on record)."""
    day = now_dt.date()
    return sum(1 for m in _mails(con) for h in m.get('history') or [] if h.get('state') == 'sending' and _dt(h.get('at') or 0).date() == day)


# ---- the words (code writes them; no model) -------------------------------------------------------------------------------

def _clock(ts: float, now_dt: datetime) -> str:
    t = _dt(ts)
    hour = t.strftime('%I:%M%p').lstrip('0').lower()
    if t.date() == now_dt.date():
        return hour + ' today'
    return t.strftime('%a %b ') + str(t.day) + ' at ' + hour


def _greeting() -> str:
    """The opening of a code-written email: '<owner first name>, this is <agent name>. ' (no name set: 'Hi, this is ...')."""
    who = settings.OWNER_FIRST if settings.OWNER_FIRST != 'the owner' else 'Hi'
    return f'{who}, this is {settings.AGENT_NAME}. '


def words(mail: dict, now_dt: datetime) -> tuple:
    """-> (subject, body) of the stand-in email. Plain words; the reference in the subject is how their inbox is searched."""
    text = re.sub(r'\s*—\s*|\s+–\s+', ', ', str(mail.get('text') or '').replace('\r\n', '\n')).strip()   # house style: no em dashes
    when = _clock(mail['texted_at'], now_dt)
    if mail.get('why') == 'unconfirmed':
        lead = f'I texted you at {when} and I could not confirm that the text reached you, so here it is by email as well.'
    else:
        lead = f'I tried to text you at {when} and the text did not go through, so I am sending it here instead.'
    subject = f"{settings.AGENT_NAME} urgent: my text to you did not go through ({mail['ref']})"
    body = (f'{_greeting()}{lead}\n\nWhat the text said:\n\n{text}\n\n'
            f"A repair alarm is open for my texting line. Reference: {mail['ref']}")
    return subject, body


# ---- the two things, once per item ----------------------------------------------------------------------------------------

def _ticket(con, life, mail: dict, now: float) -> None:
    """ONE repair ticket for the failed note (where: life:<note key>), whatever number of urgent items it carried.
    "asked" is saved before the alarm is handed off, so a run that dies in the call never asks twice; only a clear
    "could not be reached" (False) is tried again on a later run."""
    if mail.get('ticket') not in ('wanted', 'retry'):
        return
    mail['ticket'] = 'asked'
    _put(con, K_MAIL + mail['ref'], mail)
    items = list(mail.get('items') or [])
    row = con.execute('SELECT payload FROM opps WHERE key=?', (items[0],)).fetchone() if items else None
    try:
        title = str(json.loads(tuple(row)[0]).get('title') or items[0]) if row else str(mail.get('note'))
    except (TypeError, ValueError):
        title = str(items[0])
    if len(items) > 1:
        title += f' (and {len(items) - 1} more)'
    try:
        import worker as W
        took = W._alarm(life, mail['note'], f'An urgent text from Zeus to {settings.OWNER_FIRST} did not go through: {title}', kind=TICKET_KIND,
                        detail=[f"note {mail.get('note')}: {WHY.get(mail.get('why'), mail.get('why'))}", f"items: {', '.join(items)[:200]}",
                                f"stand-in email reference {mail.get('ref')}"])
    except Exception:
        took = False
    mail['ticket'] = 'retry' if took is False else 'raised'      # None = a sandbox, which never raises real alarms
    mail['ticket_at'] = now
    _put(con, K_MAIL + mail['ref'], mail)


def _mail_step(con, life, mail: dict, now_dt: datetime, send=None, arrived=None) -> str:
    """One step for one email record -> its state after the step. The only place in this file that sends."""
    import sendmail as M
    now = now_dt.timestamp()
    state = mail.get('state')
    if state in ENDED:
        return state
    if state == 'sending':
        mark = next((h for h in reversed(mail.get('history') or []) if h.get('state') == 'sending'), {})
        if _alive(mark.get('pid')) and mark.get('pid') != os.getpid() and now - float(mark.get('at') or now) <= SEND_PATIENCE:
            return state                                  # another run is sending it right now
        mail = _move(con, mail, 'unknown', 'the run that was sending it ended before it could say how it went', now)
        state = 'unknown'
    if state == 'unknown':
        seen = (arrived or M.alert_arrived)(mail['ref'], life)
        _note(con, mail, 'read their inbox', now, found='there' if seen is True else 'not there' if seen is False else 'could not be read')
        if seen is True:
            _move(con, mail, 'sent', 'found in their inbox when it was read', now, receipt={'at': now, 'found_in_inbox': True})
            return 'sent'
        tried = float(next((h.get('at') for h in reversed(mail['history']) if h.get('state') == 'sending'), now))
        absent = [h for h in mail['history'] if h.get('note') == 'read their inbox' and h.get('found') == 'not there' and float(h.get('at') or 0) >= tried]
        if seen is False and len(absent) >= 2 and now - tried >= SETTLE_WAIT:
            if int(mail.get('tries') or 0) >= MAIL_TRIES:
                _move(con, mail, 'gave_up', f'their inbox was read {len(absent)} times and it is not there; it was tried {mail.get("tries")} times', now)
                return 'gave_up'
            _move(con, mail, 'waiting', f'their inbox was read {len(absent)} times and it is not there: it did not go', now)
        return mail['state']                              # it goes again, if at all, on a later run
    # waiting: nothing has been attempted (or it provably did not go)
    if quiet(life, now_dt):
        if not mail.get('held'):
            mail['held'] = True
            _put(con, K_MAIL + mail['ref'], mail)
        return 'waiting'                                  # the ticket is up; the email waits for 9am
    if mail.pop('held', None):
        _put(con, K_MAIL + mail['ref'], mail)
        return 'waiting'                                  # the first run after quiet hours sends the text again: the email waits this one run to see whether it arrives
    if not any(unresolved(con, k, mail['at'], mail.get('note')) for k in mail.get('items') or []):
        _move(con, mail, 'not_needed', 'by the time an email could go, they had been reached or the item was closed', now)
        return 'not_needed'
    held = M.alert_parked(life)
    if not held and _attempts_today(con, now_dt) >= MAILS_PER_DAY:
        held = f'{MAILS_PER_DAY} stand-in emails were already attempted today'
    if held:
        if mail.get('stopped_by') != str(held):
            mail['stopped_by'] = str(held)                # written once per reason; the record waits and no try is used up
            _note(con, mail, 'not sent', now, why=str(held)[:200])
        return 'waiting'
    subject, body = words(mail, now_dt)
    if quiet(life, now_dt):
        return 'waiting'
    mail['tries'] = int(mail.get('tries') or 0) + 1
    _move(con, mail, 'sending', 'the email is being handed to the mail service', now, pid=os.getpid())   # saved BEFORE the attempt
    try:
        receipt = (send or M.alert)(subject, body, life)
    except M.AlertHeld:
        mail['tries'] -= 1
        _move(con, mail, 'waiting', 'quiet hours began before the handoff', now)
        return 'waiting'
    except M.SendFailed as e:
        last = mail['tries'] >= MAIL_TRIES
        _move(con, mail, 'gave_up' if last else 'waiting', 'the mail service said no: ' + str(e)[:200], now)
        return mail['state']
    except Exception as e:                                # no clear answer, or anything unforeseen: it may have gone
        why = str(e)[:200] if isinstance(e, M.SendUncertain) else f'the send stopped part way ({type(e).__name__})'
        _move(con, mail, 'unknown', why, now)
        return 'unknown'
    _move(con, mail, 'sent', 'the mail service accepted it', now, receipt=receipt)
    return 'sent'


def text_failed(con, life, note_key: str, item_keys, now, why: str = 'failed', ticket_exists: bool = False, send=None, arrived=None):
    """notifier.py's call: this note did not reach them (or cannot be confirmed). For each URGENT item it carried that has
    no mark yet: the marks and ONE email record are saved, then ONE ticket for the note, then the email (unless it is night).
    -> {'items': [...], 'mail': state} when something was started, None otherwise. Never raises."""
    try:
        if not on(life) or not item_keys:
            return None
        con.execute(META)
        now_dt = _dt(now)
        ts = now_dt.timestamp()
        marks = ','.join('?' * len(item_keys))
        urgent = [r[0] for r in con.execute(f"SELECT key FROM opps WHERE key IN ({marks}) AND urgency='now' ORDER BY key", tuple(item_keys)).fetchall()]
        new = [k for k in urgent if _get(con, K_ITEM + k) is None]
        if not new:
            return None
        note = con.execute('SELECT text,at FROM outbox WHERE key=?', (note_key,)).fetchone()
        text, texted_at = (tuple(note) if note else ('', ts))
        ref = 'zeusref' + hashlib.sha256(f'{note_key}|{",".join(new)}'.encode()).hexdigest()[:10]
        mail = _get(con, K_MAIL + ref) or {'ref': ref, 'note': note_key, 'items': new, 'why': why, 'text': text, 'texted_at': float(texted_at or ts),
                                           'at': ts, 'state': 'waiting', 'tries': 0, 'ticket': 'existing' if ticket_exists else 'wanted',
                                           'history': [{'state': 'waiting', 'at': ts, 'why': WHY.get(why, why)}]}
        con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (K_MAIL + ref, json.dumps(mail, ensure_ascii=False, default=str)))
        for k in new:
            con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                        (K_ITEM + k, json.dumps({'item': k, 'note': note_key, 'at': ts, 'why': why, 'mail': ref}, default=str)))
        con.commit()                                      # on record before the ticket or the email is attempted
        _ticket(con, life, mail, ts)
        if why == 'never_arrived':                        # the notifier sends that text again in this same run: the email waits one run to see whether it arrived
            return {'items': new, 'mail': 'waiting', 'ref': ref}
        return {'items': new, 'mail': _mail_step(con, life, mail, now_dt, send, arrived), 'ref': ref}
    except Exception as e:
        try:
            if con.in_transaction:
                con.rollback()
        except Exception:
            pass
        return {'error': f'{type(e).__name__}: {str(e)[:160]}'}


def tend(con, life, now, send=None, arrived=None):
    """Every live run (notifier.deliver, after the read-backs): an email that waited through the night goes at 9am if
    the item is still unresolved, an unknown email is settled by reading their inbox, an alarm that could not be
    recorded is tried again. -> counts, or None when there was nothing to do. Never raises."""
    try:
        if not on(life):
            return None
        con.execute(META)
        now_dt = _dt(now)
        out = {}
        for mail in _mails(con):
            if mail.get('ticket') == 'retry':
                _ticket(con, life, mail, now_dt.timestamp())
                out['tickets_retried'] = out.get('tickets_retried', 0) + 1
            if mail.get('state') in ENDED:
                continue
            if mail.get('state') == 'waiting' and float(mail.get('at') or 0) >= now_dt.timestamp():
                continue                                  # put on record in this very run: the text is going again right now, so the email waits one run
            got = _mail_step(con, life, mail, now_dt, send, arrived)
            out[got] = out.get(got, 0) + 1
        backup = deadlines(con, life, now_dt)            # the off-Mac deadline backup (below). None when nothing changed
        if backup:
            out['deadline_backup'] = backup
        return out or None
    except Exception as e:
        try:
            if con.in_transaction:
                con.rollback()
        except Exception:
            pass
        return {'error': f'{type(e).__name__}: {str(e)[:160]}'}


# ==== the off-Mac deadline backup. Rides urgent-fallback-on; also needs features.deadline_backup. ===================
# Hard deadlines escalate in time even if the Mac is off, exactly once. This is an ESCALATION PATH, NOT AN EXECUTOR:
# if the Mac is down the scheduled email still lands; nothing reconciles or acts until the Mac returns. One
# hard-deadline type: bills (items `bill:...` with their own `deadline`; a bill on autopay is closed by the worker,
# which takes its backup down).
#
# For each open bill whose commitment is not stopped, the Mac keeps ONE draft scheduled on AgentMail's servers (Zeus's
# inbox to the owner's alert inbox, sendmail.drafts) with send_at = 9:15am the owner's local time the day before the
# deadline.
#   - correction (new revision or new deadline) or a changed send_at: the old draft is deleted and confirmed gone, and
#     only then is the new one scheduled. Never two live drafts for one commitment (one record per commitment).
#   - STOP (commitment stopped, item closed): the draft is deleted at once; nothing sends.
#   - the Mac alive at the moment (its first run from 9:00am on that day, 15 minutes before send_at): it deletes the draft
#     first, confirms it is gone, and only then hands the nudge to the normal text path (an urgent item `<bill>#deadline:<rev>`
#     that notifier.deliver sends in this same run, right after tend).
#   - a delete with no clear answer: the draft's status is read. "sending"/"sent", or gone at or after send_at minus 2
#     minutes, means the backup went: the Mac does not send. Gone well before send_at: the delete took effect (AgentMail
#     sends nothing before send_at), so the Mac sends.
#   - the Mac back after downtime: a draft that is gone and that the Mac did not delete is recorded "sent_by_backup" with
#     the receipt; no text repeats it.
#   - a create with no clear answer: the drafts are listed by the draft's own reference label before anything else; a
#     found draft is adopted, never made twice.
#   - a deliberate stop: `life stop` (the life folder's `off`, read on the tick's off path, notifier.settle_only) or
#     Zeus parked (STOP, ESTOP, PAUSED, gateway.parked; sendmail.alert_parked, read on the normal path). Stop/park =
#     silence: every backup draft this engine scheduled is deleted, then the scheduled list is read back and the result
#     kept (`deadline_backup_readback`). A delete with no clear answer, or a draft still listed, stays live on record
#     and is tried again on the next run while stopped; it is never counted as gone. On resume each bill still open gets
#     ONE draft again, for its current revision. A crash is not a stop: it cannot send a cancel, so the drafts still
#     fire (a dead Mac is the alarm).
# Kept in the ledger's meta table, one row per commitment (`deadline_backup:<commitment id>`). No new table or module.
# Standard library only, no model. Never raises into the notifier.
from datetime import timedelta, timezone  # noqa: E402

# The bill backup schedules REAL AgentMail drafts, so it stays off until the owner turns it on in the config file
# (features.deadline_backup = true). While held, deadlines() does nothing at all: no draft is listed, scheduled,
# deleted or handed over.
DEADLINE_BACKUP_HELD = str(settings.get('features.deadline_backup', False)).strip().lower() not in ('1', 'true', 'yes', 'on')
K_DL = 'deadline_backup:'
DL_TYPE = 'bill:'                     # the one hard-deadline type covered
DL_AT = (9, 15)                       # send_at: this time, their local clock, the day before the deadline (never before 9am)
DL_FROM_HOUR = 9                      # the Mac takes the nudge over from its first run at or after 9:00am that day
DL_WINDOW = 15 * 60                   # ... which is this long before send_at
DL_SURE = 2 * 60                      # a draft gone at least this long before send_at cannot have been sent by AgentMail
DL_ITEM_CLOSED = ('done', 'moot', 'declined', 'skipped')
DL_PHASE_OVER = ('stopped', 'done')
DL_LIVE = ('scheduling', 'scheduled', 'unknown', 'deleting')     # a draft may exist on the service
K_DL_READ = 'deadline_backup_readback'   # the last read-back of the scheduled list after a stop
DL_WORDS_DO = 'Make sure it is paid, or on autopay, before then.'


def _dl_ts(value):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return t.timestamp() if t.tzinfo else None        # a deadline with no zone is not clean: no backup


def dl_send_at(deadline_ts: float, life=None):
    """-> send_at (aware datetime, their local zone at the deadline): 9:15am the day before. Never before 9am."""
    try:
        import ownertime as OT
        z = OT.zone(deadline_ts, life)
    except Exception:
        from ownertime import PACIFIC as z
    d = datetime.fromtimestamp(deadline_ts, z) - timedelta(days=1)
    return d.replace(hour=DL_AT[0], minute=DL_AT[1], second=0, microsecond=0)


def _dl_stopped(life):
    """The owner's deliberate stop: -> 'life stop' (the life folder's `off`) or the parked file's name, else None."""
    try:
        if (Path(life) / 'off').exists():
            return 'life stop'
    except OSError:
        return 'life stop (unreadable)'
    import sendmail as M
    return M.alert_parked(life)


def _dl_z(ts: float) -> str:
    """send_at as the service writes it back: UTC, 'YYYY-MM-DDTHH:MM:SSZ'."""
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _dl_wanted(con, life) -> dict:
    """-> {commitment id: what should be scheduled} for every bill item (children left out) with a clean deadline."""
    out = {}
    for (key, state, payload) in con.execute("SELECT key,state,payload FROM opps WHERE key LIKE ? AND instr(key,'#')=0", (DL_TYPE + '%',)).fetchall():
        try:
            p = json.loads(payload)
        except (TypeError, ValueError):
            continue
        c = p.get('commitment') if isinstance(p.get('commitment'), dict) else {}
        row = con.execute('SELECT commitment_id,revision,phase FROM commitments WHERE opp_key=?', (key,)).fetchone() if _has(con, 'commitments') else None
        cid, rev, phase = (tuple(row) if row else (c.get('commitment_id'), c.get('revision') or 1, c.get('phase')))
        due = _dl_ts(p.get('deadline'))
        if not cid or due is None:
            continue
        stop = state in DL_ITEM_CLOSED or phase in DL_PHASE_OVER or isinstance(p.get('closed'), dict)
        send_at = dl_send_at(due, life)
        out[str(cid)] = {'cid': str(cid), 'item': key, 'revision': int(rev or 1), 'deadline': str(p.get('deadline')), 'deadline_ts': due,
                         'identity': f'{cid}:{int(rev or 1)}:{p.get("deadline")}', 'send_at': send_at.isoformat(), 'send_at_ts': send_at.timestamp(),
                         'title': str(p.get('title') or key), 'stop': bool(stop)}
    return out


def _has(con, table) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def dl_words(want: dict, ref: str) -> tuple:
    """-> (subject, body) of the backup email. Code writes it, no model. Honest about what it is."""
    import ownertime as OT
    d = datetime.fromtimestamp(want['deadline_ts'], OT.zone(want['deadline_ts']))
    due = d.strftime('%a %b ') + str(d.day) + ' at ' + d.strftime('%I:%M%p').lstrip('0').lower()
    title = re.sub(r'\s*—\s*|\s+–\s+', ', ', want['title']).strip()      # house style: no em dashes
    subject = f'Zeus deadline: {title} (due {d.strftime("%b")} {d.day}) [{ref}]'
    body = (f'{_greeting()}{title}. It is due {due}. {DL_WORDS_DO}\n\n'
            'This email was scheduled ahead of time as a backup. If my Mac is on, it cancels this email and texts you '
            'instead, so if you are reading this, my Mac was probably off: nothing else has been checked or done, and '
            'nothing will be until it is back.\n\n'
            f'Reference: {ref}')
    return subject, body


def _dl_put(con, rec: dict, state: str, why: str, now: float, **more) -> dict:
    rec['state'] = state
    rec.setdefault('history', []).append({'state': state, 'at': now, 'why': str(why)[:300]} | {k: v for k, v in more.items() if v is not None})
    rec.update({k: v for k, v in more.items() if k in ('draft_id', 'receipt') and v is not None})
    _put(con, K_DL + rec['cid'], rec)
    return rec


def dl_records(con) -> dict:
    """-> {commitment id: record}. Read only (tests, the health look)."""
    con.execute(META)
    out = {}
    for (k, v) in con.execute('SELECT key,value FROM meta WHERE key LIKE ?', (K_DL + '%',)).fetchall():
        try:
            out[k[len(K_DL):]] = json.loads(v)
        except (TypeError, ValueError):
            continue
    return out


def _dl_status(draft) -> str:
    """A draft as the service returned it -> 'gone' | 'scheduled' | 'sending' | 'sent' | 'failed'."""
    if draft is None:
        return 'gone'
    return str((draft or {}).get('send_status') or 'scheduled')


def _dl_take_down(con, api, rec: dict, now: float) -> str:
    """Delete this draft and confirm it is gone. -> 'ours' (gone, and nothing went), 'went' (the backup sent it),
    'wait' (unclear: nothing is decided, the next run looks again). Saves 'deleting' before the delete."""
    import sendmail as M
    sure = now < float(rec['send_at_ts']) - DL_SURE           # AgentMail sends nothing before send_at
    try:
        first = _dl_status(api.get(rec['draft_id']))
    except M.SendUncertain:
        return 'wait'
    except M.SendFailed:
        return 'wait'
    if first in ('sending', 'sent'):
        return 'went'
    if first == 'gone':
        return 'ours' if sure else 'went'
    _dl_put(con, rec, 'deleting', 'the draft is being deleted', now, was=rec['state'] if rec['state'] != 'deleting' else rec.get('was'))
    try:
        api.delete(rec['draft_id'])
    except M.SendFailed as e:
        rec.setdefault('history', []).append({'note': 'the service refused the delete: ' + str(e)[:150], 'at': now})
        _put(con, K_DL + rec['cid'], rec)
        return 'wait'
    except M.SendUncertain:
        pass                                                   # unclear: read the status (below), never decide blind
    try:
        after = _dl_status(api.get(rec['draft_id']))           # confirm it is gone
    except (M.SendUncertain, M.SendFailed):
        return 'wait'
    if after in ('sending', 'sent'):
        return 'went'
    if after == 'gone':
        return 'ours' if sure else 'went'
    return 'wait'                                              # still there: the next run tries again


def _dl_hand_to_mac(con, rec: dict, want: dict, now_dt: datetime) -> str:
    """The Mac is alive at the moment and the draft is confirmed gone: the nudge goes by the normal text path, as an
    urgent item the notifier sends in this same run. One key per revision, so it is made once."""
    key = f"{rec['item']}#deadline:{rec['revision']}"
    subject, body = dl_words(want or rec, rec['ref'])
    head = body.split('\n\n', 1)[0].replace(_greeting(), '')
    p = {'title': subject.split(' [', 1)[0].replace('Zeus deadline: ', 'Deadline: '), 'why': 'a hard deadline (bill) reaches its nudge moment',
         'evidence': f"deadline {rec['deadline']} on {rec['item']}", 'dedupe_key': key, '_source': 'deadline_backup', 'urgency': 'now',
         'parent': rec['item'], 'deadline': rec['deadline'], 'ready_at': now_dt.timestamp(),
         'deadline_backup': {'ref': rec['ref'], 'identity': rec['identity']},
         'packet': {'status': 'done', 'headline': head, 'details': '', 'decision': None, 'followups': [], 'learned': [], 'sources': []}}
    con.execute('INSERT OR IGNORE INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key, 'ready', 'auto', now_dt.isoformat(), 'now', 'notify_owner_now', json.dumps(p, ensure_ascii=False), json.dumps(['deadline:' + rec['item']]),
                 now_dt.timestamp(), now_dt.timestamp()))
    return key


def _dl_schedule(con, life, api, want: dict, rec, now: float) -> str:
    """Schedule the one draft for `want`. Saved as 'scheduling' BEFORE the call. -> the record's state."""
    import sendmail as M
    tries = int((rec or {}).get('tries') or 0) if (rec or {}).get('identity') == want['identity'] else 0
    ref = 'zeusdl' + hashlib.sha256(want['identity'].encode()).hexdigest()[:10]
    past = list((rec or {}).get('past') or [])
    if rec and rec.get('identity') and rec.get('identity') != want['identity']:
        past.append({k: rec.get(k) for k in ('identity', 'state', 'draft_id', 'send_at', 'receipt')})
    new = {k: want[k] for k in ('cid', 'item', 'revision', 'deadline', 'deadline_ts', 'identity', 'send_at', 'send_at_ts', 'title')}
    new |= {'ref': ref, 'tries': tries + 1, 'past': past, 'history': list((rec or {}).get('history') or [])[-20:]}
    _dl_put(con, new, 'scheduling', 'the backup draft is being scheduled', now)
    subject, body = dl_words(want, ref)
    try:
        got = api.create({'to': [M.ALERT_TO], 'subject': subject, 'text': body, 'send_at': _dl_z(want['send_at_ts'])})   # only the fields the service is known to take
    except M.SendFailed as e:
        _dl_put(con, new, 'none', 'the service refused it: ' + str(e)[:150], now)
        return 'none'
    except Exception as e:
        _dl_put(con, new, 'unknown', f'no clear answer ({type(e).__name__})', now)
        return 'unknown'
    _dl_put(con, new, 'scheduled', 'the service took it', now, draft_id=str(got.get('draft_id')))
    return 'scheduled'


def _dl_find(con, api, rec: dict, now: float) -> str:
    """A create with no clear answer: look for the draft by its own reference before anything else."""
    import sendmail as M
    try:
        found = [d for d in api.list('scheduled') if f"[{rec['ref']}]" in str(d.get('subject') or '')]   # the proven list query; the reference is in the subject
    except (M.SendUncertain, M.SendFailed):
        return 'unknown'
    if found:
        _dl_put(con, rec, 'scheduled', 'found by its reference after an unclear answer', now, draft_id=str(found[0].get('draft_id')))
        return 'scheduled'
    if now < float(rec['send_at_ts']) - DL_SURE:
        _dl_put(con, rec, 'none', 'not on the service: it was never made', now)
        return 'none'
    _dl_put(con, rec, 'sent_by_backup', 'not on the service after its time: it may have gone (unclear create)', now, receipt={'unclear_create': True, 'read_at': now})
    return 'sent_by_backup'


def _dl_step(con, life, api, rec, want, now_dt: datetime):
    """One commitment, one step. -> the new state when something changed, else None."""
    now = now_dt.timestamp()
    if rec and (rec.get('state') == 'unknown' or (rec.get('state') == 'scheduling' and not rec.get('draft_id'))):
        got = _dl_find(con, api, rec, now)          # a create with no clear answer, or a run that died inside it
        if got == 'unknown':
            return None
        rec = dl_records(con).get(rec['cid'])
    changed = None
    if rec and rec.get('state') in ('scheduling', 'scheduled', 'deleting') and rec.get('draft_id'):
        if want is None or want['stop']:
            why = 'stop'
        elif want['identity'] != rec['identity'] or want['send_at'] != rec['send_at']:
            why = 'replaced'
        elif now >= max(float(rec['send_at_ts']) - DL_WINDOW, datetime.fromisoformat(rec['send_at']).replace(hour=DL_FROM_HOUR, minute=0).timestamp()):
            why = 'moment'
        else:
            return None
        down = _dl_take_down(con, api, rec, now)
        if down == 'wait':
            return 'waiting'
        if down == 'went':
            receipt = {'draft_id': rec['draft_id'], 'send_at': rec['send_at'], 'read_at': now, 'seen': 'not scheduled any more, not deleted by the Mac'}
            _dl_put(con, rec, 'sent_by_backup', 'the backup email went (the Mac did not delete it)', now, receipt=receipt)
            return 'sent_by_backup'
        if why == 'stop' and want is not None and want.get('stood_down'):
            _dl_put(con, rec, 'stood_down', f"Zeus was stopped ({want['stood_down']}): the draft was deleted and confirmed gone; it comes back once on resume", now)
            return 'stood_down'
        if why == 'stop':
            _dl_put(con, rec, 'cancelled', 'stopped or closed: the draft was deleted, nothing sends', now)
            return 'cancelled'
        if why == 'moment':
            child = _dl_hand_to_mac(con, rec, want, now_dt)
            _dl_put(con, rec, 'handed_to_mac', 'the Mac is on at the moment: draft deleted and confirmed gone, the normal text carries it', now, child=child)
            return 'handed_to_mac'
        _dl_put(con, rec, 'replaced', 'a correction: the old draft was deleted and confirmed gone', now)
        changed = 'replaced'
    if want is None or want['stop']:
        return changed
    if rec and rec.get('identity') == want['identity'] and rec.get('state') not in ('none', 'replaced', 'stood_down'):   # stood_down: resumed, ONE draft again
        return changed                                         # this revision already has (or had) its one backup
    if now >= want['send_at_ts'] - DL_WINDOW:
        return changed                                         # too close to (or past) the moment: the Mac's normal path owns it
    got = _dl_schedule(con, life, api, want, dl_records(con).get(want['cid']), now)
    return got if got not in ('none',) else (changed or 'none')


def _dl_readback(con, api, why: str, now: float) -> dict:
    """After a stop took the drafts down, read the scheduled list back. A draft of ours still listed goes back on
    record as scheduled (tried again on the next run while stopped), never counted as gone. -> the kept result."""
    import sendmail as M
    recs = dl_records(con)
    try:
        listed = api.list('scheduled')
    except (M.SendUncertain, M.SendFailed) as e:
        res = {'at': now, 'stop': why, 'read': False, 'why': str(e)[:150], 'ok': False}
    else:
        left = []
        for d in listed:
            cid = next((c for c, r in recs.items() if (r.get('draft_id') and d.get('draft_id') == r['draft_id'])
                        or (r.get('ref') and f"[{r['ref']}]" in str(d.get('subject') or ''))), None)
            if cid is None:
                continue
            left.append(str(d.get('draft_id')))
            if recs[cid].get('state') not in DL_LIVE:
                _dl_put(con, recs[cid], 'scheduled', 'still on the service after the stop: not gone, taken down again next run', now, draft_id=str(d.get('draft_id')))
        live = sorted(c for c, r in dl_records(con).items() if r.get('state') in DL_LIVE)
        res = {'at': now, 'stop': why, 'read': True, 'scheduled_total': len(listed), 'ours_left': left, 'not_settled': live, 'ok': not left and not live}
    _put(con, K_DL_READ, res)
    return res


def deadlines(con, life, now_dt, api=None):
    """urgentfallback.tend's call, every live run: keep one backup draft per open bill, take it down on STOP or
    correction, hand the nudge to the normal text path when the Mac is on at the moment, and record what the backup
    sent while the Mac was down. -> {state: count} for what changed, or None. Never raises."""
    try:
        if DEADLINE_BACKUP_HELD or not on(life):
            return None
        con.execute(META)
        now_dt = _dt(now_dt)
        wanted, recs = _dl_wanted(con, life), dl_records(con)
        stopped = _dl_stopped(life)                            # a deliberate stop takes every backup down
        if stopped:
            for w in wanted.values():
                if not w['stop']:
                    w['stop'], w['stood_down'] = True, stopped
        if not wanted and not any(r.get('state') in DL_LIVE for r in recs.values()):
            return None
        if api is None:
            import sendmail as M
            try:
                api = M.drafts(life)
            except M.SendFailed:
                return None                                    # a sandbox without a pretend service: nothing is scheduled
        out = {}
        for cid in sorted(set(wanted) | set(recs)):
            got = _dl_step(con, life, api, recs.get(cid), wanted.get(cid), now_dt)
            if got:
                out[got] = out.get(got, 0) + 1
        if stopped and (out or any(r.get('state') in DL_LIVE for r in dl_records(con).values())):
            out['readback'] = _dl_readback(con, api, stopped, now_dt.timestamp())
        con.commit()
        return out or None
    except Exception as e:
        try:
            if con.in_transaction:
                con.rollback()
        except Exception:
            pass
        return {'error': f'{type(e).__name__}: {str(e)[:160]}'}

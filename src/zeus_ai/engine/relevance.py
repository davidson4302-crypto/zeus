"""relevance: ONE decision about what reaches the owner, and one honest answer to "did it reach the owner?".

Why. Without this, each rule about "should this go to the owner" would live where it was first needed: the minute key of a note, the
question cap in the notifier, the chat's own told stamp. Nothing remembered that a question had already been put to
the owner, so the same ask could come again in the next note or after the chat had asked it; and "sent" in the outbox was
read as "delivered" although it only means the texting line gave a receipt.

What is in here
  decide(con, life, content, now)   the one decision. `content` says what it is and for which job and version:
        kind           'answer'   an answer to what the owner just asked        -> always goes now
                       'result'   finished work, or a failure            -> goes now; only an exact repeat is dropped
                       'decision' a decision that is the owner's                 -> goes now, unless the same decision was
                                                                           already put to the owner and nothing changed
                       'deadline' something that lands soon              -> goes now; only an exact repeat is dropped
                       'routine'  information that can wait              -> goes now, on its own (there is no morning note)
        commitment_id, revision, opp_key, due, ask, target, fingerprint
     -> {'verdict': 'send_now' | 'next_brief' | 'duplicate' | 'replaces', 'why': plain words, ...}
     It can only make Zeus quieter or more exact. It never lets anything through quiet hours: the notifier calls it
     AFTER its own quiet-hour gates (policy.quiet_hold, notifier.night_stopped, can_go_at_night), which are untouched.
     Nothing meaningful is dropped: a result, a failure or an unfinished obligation always goes; only an exact repeat
     or an unchanged ask is dropped, and routine information waits for the brief (once: an item that already waited
     goes with the next note).
  asks (table)   one row per decision put to the owner, whatever the channel (a note, or the chat). decision_key = the job's
     id (the item's key when the item has no commitment) + its version + what is being asked. A changed job (a new
     version; for an item without versions, a changed question or prepared step) REPLACES the earlier ask: the old
     row becomes 'replaced', a prepared step still waiting on the owner's yes for the old version is set aside (actions.py),
     and the note writer is handed both questions as plain sentences so the note can say what changed.
     Silence gives no authority, and nothing here ever creates a follow-up whose only job is to chase an answer.
  delivery_state(con, opp_key)   what the outbox and the item already record, as one of:
        delivered_verified            the note was read back in the owner's Messages
        accepted_unverified           the line gave a receipt; it has not been read back (or it was told in chat)
        uncertain                     may or may not have gone: needs reconciling
        failed                        it provably did not reach the owner
        closed_no_notification        closed, nothing needed telling (with the recorded reason)
        superseded_stopped_declined   set aside, stopped, or the owner said no
     and, for what has not been attempted yet: 'waiting' (a quiet-hour hold is not a failure) or 'not_sent' (sending
     is switched off). "Delivered" is never made up: the outbox's own 'sent' is only a receipt from the line.
  tend(con, life, now, live)   every tick, after the notifier: asks made in chat are picked up from the ledger, a
     note the line accepted is read back once it can be (read-only), and a delivery still unresolved after 30 minutes
     gets the existing repair ticket (the same kind and key notifier._settle uses), once.
  health_part(life, note)   a small `deliveries` part in health.json.

Always on (no switch file).
Tables (made on first use, never in tick.SCHEMA): asks, relevance_log (what was dropped, moved, replaced, told and
read back, each with its record ids: the weekly scorecard counts from it).
Standard library only. No model is asked. Nothing here sends anything.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

SWITCH = 'relevance-on'
KINDS = ('answer', 'result', 'decision', 'deadline', 'routine')
SEND, DUPLICATE, REPLACES = 'send_now', 'duplicate', 'replaces'
VERDICTS = (SEND, DUPLICATE, REPLACES)
DELIVERED, ACCEPTED, UNCERTAIN, FAILED = 'delivered_verified', 'accepted_unverified', 'uncertain', 'failed'
CLOSED_QUIET, SET_ASIDE, WAITING, NOT_SENT = 'closed_no_notification', 'superseded_stopped_declined', 'waiting', 'not_sent'
DELIVERY_STATES = (DELIVERED, ACCEPTED, UNCERTAIN, FAILED, CLOSED_QUIET, SET_ASIDE)
UNRESOLVED_AFTER = 30 * 60        # = notifier.UNSETTLED_GIVE_UP: an eligible delivery unresolved this long has a repair ticket
VERIFY_TRIES = 6                  # a note the line accepted is read back at most this many times (about 30 minutes of ticks)
VERIFY_WINDOW = 24 * 3600
TICKET_WINDOW = 7 * 86400
TICKET_TITLE = "Zeus's note to the owner: delivery could not be confirmed after 30 minutes (it was not sent again)"   # notifier._settle's own words
TICKET_KIND = 'life_note_unconfirmed'
SOON = 24 * 3600
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
TABLES = """
CREATE TABLE IF NOT EXISTS asks(decision_key TEXT PRIMARY KEY, commitment_id TEXT, revision INTEGER, first_at REAL, last_at REAL,
  channel TEXT, state TEXT, note_key TEXT);
CREATE INDEX IF NOT EXISTS asks_by_commitment ON asks(commitment_id);
CREATE TABLE IF NOT EXISTS relevance_log(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, opp_key TEXT, commitment_id TEXT,
  revision INTEGER, kind TEXT, verdict TEXT, why TEXT, decision_key TEXT, note_key TEXT, fp TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS relevance_log_by_opp ON relevance_log(opp_key);
CREATE INDEX IF NOT EXISTS relevance_log_by_note ON relevance_log(note_key)
"""
ASK_STATES = ('pending', 'asked', 'replaced', 'answered', 'not_delivered')
# Items code itself makes for the owner (a monitor's news, the owed-replies list, a question about two rules, a trip ask, an
# offer, the weekly scorecard) are finished work, not something merely noticed.
MADE_BY_CODE = ('loops', 'conflicts', 'travel', 'monitor')


# ---- small things --------------------------------------------------------------------------------------------------------

def on(life) -> bool:
    try:
        return life is not None   # always on (no switch file)
    except (OSError, TypeError):
        return False


def ensure(con) -> None:
    """The two tables, made the first time anything here is used. A ledger that never uses this never gets them."""
    for stmt in TABLES.split(';'):
        if stmt.strip():
            con.execute(stmt)


def _has(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _ts(now) -> float:
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now) if now is not None else time.time()


def _local(now) -> datetime:
    """The moment on the owner's clock (ownertime: the home time zone unless the owner is away)."""
    from ownertime import PACIFIC
    if isinstance(now, datetime):
        return now.astimezone(PACIFIC) if now.tzinfo else now.replace(tzinfo=PACIFIC)
    return datetime.fromtimestamp(_ts(now), PACIFIC)


def _moment(value):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        from ownertime import PACIFIC
        t = t.replace(tzinfo=PACIFIC)
    return t


def _norm(text) -> str:
    return ' '.join(re.findall(r"[a-z0-9$%@.:/'-]+", str(text or '').lower()))


def _sha(*parts) -> str:
    return hashlib.sha1('\x1f'.join(_norm(p) for p in parts).encode()).hexdigest()[:12]


def _get(row, name, default=None):
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def _when_words(ts) -> str:
    try:
        t = _local(float(ts))
        return t.strftime('%a %b ') + str(t.day) + ' at ' + t.strftime('%I:%M%p').lstrip('0').lower()
    except (TypeError, ValueError, OSError):
        return 'earlier'


def _log(con, now, *, opp_key=None, ident=None, revision=None, kind=None, verdict=None, why=None, decision_key=None, note_key=None,
         fp=None, detail=None) -> int:
    cur = con.execute('INSERT INTO relevance_log(at,opp_key,commitment_id,revision,kind,verdict,why,decision_key,note_key,fp,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (_ts(now), opp_key, ident, revision, kind, verdict, str(why or '')[:300], decision_key, note_key, fp,
                       json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None))
    return cur.lastrowid


# ---- what an item is ------------------------------------------------------------------------------------------------------

def identity(con, opp_key, opp: dict) -> tuple:
    """-> (identity, version, commitment id or None). A job with a commitment is known by its id and its version (the
    index table has the last word); an item without one by its own key, version 0."""
    c = opp.get('commitment') if isinstance(opp, dict) and isinstance(opp.get('commitment'), dict) else {}
    cid = c.get('commitment_id')
    if cid:
        rev = c.get('revision')
        if _has(con, 'commitments'):
            row = con.execute('SELECT revision FROM commitments WHERE commitment_id=?', (cid,)).fetchone()
            if row is not None:
                rev = tuple(row)[0]
        try:
            return str(cid), int(rev or 1), str(cid)
        except (TypeError, ValueError):
            return str(cid), 1, str(cid)
    return str(opp_key), 0, None


def what_of(ask, target='') -> str:
    """What is being asked, as a short mark: the question and the prepared step the owner's yes is bound to (its words). The
    state of a frozen step (actions.py) is deliberately not part of it: that moves, and the mark must not."""
    return _sha(ask, target)


def decision_key(ident, revision, what) -> str:
    return f'{ident}|r{int(revision or 0)}|{what}'


def _prep_failed(why) -> bool:
    """Did background work on this fail (as against: none was needed)? The note writer's own reading of the worker's
    reason (composer.plain_prep), so the two never disagree. In doubt: yes, it failed, and a failure always goes."""
    if not why:
        return False
    try:
        import composer as C
        return C.plain_prep(why) == C.PREP_FAILED
    except Exception:
        return True


def kind_of(row, opp: dict, now_dt) -> str:
    """What an item of the notifier's is, from its own records. In doubt it is never 'routine': only something that
    asks nothing, has no time landing before the next morning note, carries no finished work and no failure, and is
    not owed to the owner, may wait for the brief."""
    p = opp.get('packet') or {}
    if p.get('decision'):
        return 'decision'
    key = str(_get(row, 'key') or '')
    now_dt = _local(now_dt)
    nine = now_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    brief = nine if now_dt < nine else nine + timedelta(days=1)
    horizon = max(now_dt + timedelta(seconds=SOON), brief + timedelta(minutes=30))
    lands = [t for t in (_moment(opp.get('deadline')), _moment(opp.get('event_start'))) if t]
    if (_get(row, 'urgency') == 'now' or opp.get('urgency') == 'now' or any(t <= horizon for t in lands) or '#fu' in key or opp.get('followup')
            or str(p.get('headline') or '').startswith('Reminder:') or p.get('verbatim')):
        return 'deadline'
    ex = opp.get('exec') if isinstance(opp.get('exec'), dict) else {}
    if (ex.get('job_id') or _prep_failed(p.get('prep')) or p.get('ended') or isinstance(opp.get('commitment'), dict) or opp.get('resend') or opp.get('update_of')
            or opp.get('suspicious_mail')
            or opp.get('_source') in MADE_BY_CODE + ('conversation',) or isinstance(opp.get('offer'), dict) or opp.get('release_test')
            or key.startswith(('monitor:', 'chat:')) or _get(row, 'state') == 'needs_attention' or p.get('status') not in (None, '', 'done')):
        return 'result'
    return 'routine'


def content_of(con, row, opp: dict, now_dt) -> dict:
    """The `content` decide() wants, for one item of the notifier's."""
    key = str(_get(row, 'key') or opp.get('dedupe_key') or '')
    p = opp.get('packet') or {}
    d = p.get('decision') if isinstance(p.get('decision'), dict) else {}
    ident, rev, cid = identity(con, key, opp)
    kind = kind_of(row, opp, now_dt)
    content = {'kind': kind, 'opp_key': key, 'commitment_id': cid, 'revision': rev, 'identity': ident, 'channel': 'note',
               'due': opp.get('deadline') or opp.get('event_start') or _get(row, 'surface_at'),
               'fingerprint': _sha(p.get('headline'), p.get('details'), d.get('ask'), d.get('ready')),
               'waited': bool((opp.get('relevance') or {}).get('brief_from') or opp.get('asked_later') or opp.get('held_overnight'))}
    if kind == 'decision':
        ask = d.get('ask') or (str(p.get('decision')) if not d else '') or p.get('headline') or opp.get('title') or ''
        content.update(ask=str(ask), target=str(d.get('ready') or ''))
    return content


# ---- asks -----------------------------------------------------------------------------------------------------------------

def _ask_rows(con, ident) -> list:
    return [dict(zip(('decision_key', 'commitment_id', 'revision', 'first_at', 'last_at', 'channel', 'state', 'note_key'), tuple(r)))
            for r in con.execute('SELECT decision_key,commitment_id,revision,first_at,last_at,channel,state,note_key FROM asks WHERE commitment_id=? ORDER BY first_at',
                                 (str(ident),))]


def _stands(con, ask: dict) -> bool:
    """Is this ask really with the owner? One that went in a note stands only while that note is not known to have failed:
    a note that provably never reached the owner asked nothing."""
    if ask['state'] != 'asked':
        return False
    note = ask.get('note_key')
    if not note or str(note).startswith('chat:'):
        return True
    row = con.execute('SELECT state FROM outbox WHERE key=?', (note,)).fetchone()
    if row is None or tuple(row)[0] == 'failed':
        con.execute("UPDATE asks SET state='not_delivered' WHERE decision_key=? AND state='asked'", (ask['decision_key'],))
        ask['state'] = 'not_delivered'
        return False
    return True


def _ask_text(con, dkey) -> dict:
    """The words of an ask as they were when it was put to the owner (kept in the log row that recorded it)."""
    row = con.execute("SELECT detail FROM relevance_log WHERE decision_key=? AND verdict IN ('asked','replaces','pending') AND detail IS NOT NULL ORDER BY id DESC LIMIT 1",
                      (dkey,)).fetchone()
    try:
        return json.loads(tuple(row)[0]) if row else {}
    except (TypeError, ValueError):
        return {}


def _answered(con, opp_key) -> str:
    """Has the owner already answered or handled this one? From stored facts only (the existing checks' records): the owner's recorded
    yes, a close, or a prepared step that is past the question. -> the plain reason, or ''."""
    if not opp_key:
        return ''
    row = con.execute('SELECT state,payload FROM opps WHERE key=?', (str(opp_key),)).fetchone()
    if row is None:
        return ''
    state, payload = tuple(row)
    try:
        p = json.loads(payload)
    except (TypeError, ValueError):
        return ''
    if p.get('approved'):
        return 'the owner\'s yes is already on record'
    if p.get('closed') or state in ('done', 'moot', 'declined'):
        return 'it is already closed'
    if _has(con, 'actions'):
        a = con.execute('SELECT state FROM actions WHERE item=? ORDER BY created DESC, rowid DESC LIMIT 1', (str(opp_key),)).fetchone()
        if a and tuple(a)[0] in ('approved', 'sending', 'sent', 'unknown', 'declined'):
            return 'the prepared step already has the owner\'s answer on record'
    return ''


def _set_aside_step(con, opp_key, old: dict, now) -> list:
    """The approval target of a replaced ask no longer counts: a prepared step that was put to the owner for the earlier
    version, and is still only waiting on the owner's word, is set aside through actions.move (never one that is being sent or
    may have gone). -> the record ids set aside."""
    if not opp_key or not _has(con, 'actions'):
        return []
    import actions as A
    out = []
    for r in con.execute("SELECT id,history,created FROM actions WHERE item=? AND state='asked'", (str(opp_key),)).fetchall():
        rid, history, created = tuple(r)
        try:
            asked = max([float(h.get('at') or 0) for h in json.loads(history or '[]') if h.get('state') == 'asked'] or [float(created or 0)])
        except (TypeError, ValueError):
            asked = float(created or 0)
        if asked <= float(old.get('last_at') or 0) + 1 and A.move(con, rid, 'replaced', 'what it was prepared for changed after it was put to the owner; a new question takes its place',
                                                                    now=_ts(now)):
            out.append(rid)
    return out


def record_ask(con, life, content: dict, now=None, *, channel: str = 'chat', note_key=None) -> dict:
    """Write down that a decision WAS put to the owner (the notifier after its note went; the chat after it asked).
    -> {'decision_key', 'state': 'asked'}. The first time is kept; asking again only moves last_at."""
    ensure(con)
    ident = content.get('identity') or content.get('commitment_id') or content.get('opp_key')
    if not ident or not str(content.get('ask') or '').strip():
        raise ValueError('an ask needs the job (commitment_id or opp_key) and the question (ask)')
    rev = int(content.get('revision') or 0)
    dkey = decision_key(ident, rev, content.get('what') or what_of(content.get('ask'), content.get('target')))
    t = _ts(now)
    con.execute("INSERT INTO asks(decision_key,commitment_id,revision,first_at,last_at,channel,state,note_key) VALUES(?,?,?,?,?,?,'asked',?) "
                "ON CONFLICT(decision_key) DO UPDATE SET last_at=excluded.last_at, channel=excluded.channel, state='asked', note_key=excluded.note_key",
                (dkey, str(ident), rev, t, t, channel, note_key))
    _log(con, t, opp_key=content.get('opp_key'), ident=str(ident), revision=rev, kind='decision', verdict='asked', why=f'put to the owner ({channel})', decision_key=dkey,
         note_key=note_key, detail={'ask': str(content.get('ask'))[:600], 'target': str(content.get('target') or '')[:600]})
    return {'decision_key': dkey, 'state': 'asked'}


def approval_target(con, opp_key):
    """The one ask the owner's yes may answer for this item right now: -> {'decision_key', 'revision', 'asked_at', 'channel'} or
    None. A replaced ask is never it, and neither is one whose note failed."""
    if not _has(con, 'asks'):
        return None
    row = con.execute('SELECT payload FROM opps WHERE key=?', (str(opp_key),)).fetchone()
    try:
        opp = json.loads(tuple(row)[0]) if row else {}
    except (TypeError, ValueError):
        opp = {}
    ident, _rev, _cid = identity(con, opp_key, opp)
    live = [a for a in _ask_rows(con, ident) if _stands(con, a)]
    if not live:
        return None
    a = live[-1]
    return {'decision_key': a['decision_key'], 'revision': a['revision'], 'asked_at': a['last_at'], 'channel': a['channel']}


def ask_valid(con, dkey) -> bool:
    """May the owner's yes still be taken as an answer to this ask? False once it was replaced, answered or never delivered."""
    if not _has(con, 'asks'):
        return False
    rows = [dict(zip(('decision_key', 'commitment_id', 'revision', 'first_at', 'last_at', 'channel', 'state', 'note_key'), tuple(r)))
            for r in con.execute('SELECT decision_key,commitment_id,revision,first_at,last_at,channel,state,note_key FROM asks WHERE decision_key=?', (dkey,))]
    return bool(rows) and _stands(con, rows[0])


def sync_chat_asks(con, now=None) -> int:
    """Asks the chat already put to the owner, read from the ledger (the chat stamps the item: told.note 'chat:...'), so a
    note never repeats them. -> how many were newly written down."""
    ensure(con)
    n = 0
    for key, payload in con.execute("SELECT key,payload FROM opps WHERE state='surfaced' AND json_extract(payload,'$.told.note') LIKE 'chat:%'").fetchall():
        try:
            opp = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if opp.get('closed') or opp.get('approved') or not (opp.get('packet') or {}).get('decision'):
            continue
        c = content_of(con, {'key': key, 'urgency': opp.get('urgency'), 'surface_at': None, 'state': 'surfaced'}, opp, now if now is not None else time.time())
        if c['kind'] != 'decision' or not c.get('ask'):
            continue
        dkey = decision_key(c['identity'], c['revision'], what_of(c['ask'], c['target']))
        if con.execute('SELECT 1 FROM asks WHERE decision_key=?', (dkey,)).fetchone():
            continue
        told = opp.get('told') or {}
        record_ask(con, None, c, told.get('at') or _ts(now), channel='chat', note_key=str(told.get('note')))
        n += 1
    return n


# ---- the one decision -----------------------------------------------------------------------------------------------------

def decide(con, life, content: dict, now=None, *, record: bool = True) -> dict:
    """The one relevance decision (the module text has the table). record=False works the answer out and writes
    nothing. Whatever it answers, the caller still goes through the quiet-hour gate exactly as before."""
    kind = content.get('kind')
    if kind not in KINDS:
        raise ValueError(f'kind must be one of {", ".join(KINDS)}')
    ensure(con)
    opp_key = content.get('opp_key')
    ident = content.get('identity') or content.get('commitment_id') or opp_key
    rev = int(content.get('revision') or 0)
    base = {'kind': kind, 'opp_key': opp_key, 'identity': ident, 'revision': rev}

    def answer(verdict, why, **more):
        return base | {'verdict': verdict, 'why': why} | more

    def note(verdict, why, **cols):
        if record:
            return _log(con, now, opp_key=opp_key, ident=str(ident) if ident else None, revision=rev, kind=kind, verdict=verdict, why=why, **cols)
        return None

    if kind == 'answer':
        return answer(SEND, 'an answer to what the owner just asked always goes')

    if kind == 'decision':
        ask = str(content.get('ask') or '').strip()
        if not ident or not ask:
            return answer(SEND, 'it does not say which job or what is asked, so it cannot be compared with an earlier ask: it goes')
        was = _answered(con, opp_key) if not content.get('ignore_answered') else ''
        if was:
            rid = note(DUPLICATE, 'already answered: ' + was)
            return answer(DUPLICATE, 'the owner already answered or handled it: ' + was, code='answered', log_id=rid)
        what = content.get('what') or what_of(ask, content.get('target'))
        dkey = decision_key(ident, rev, what)
        rows = _ask_rows(con, ident)
        same = next((a for a in rows if a['decision_key'] == dkey), None)
        if same and _stands(con, same):
            rid = note(DUPLICATE, 'the same decision was already put to the owner and nothing changed', decision_key=dkey, note_key=same.get('note_key'))
            return answer(DUPLICATE, 'the same decision was already put to the owner and nothing changed', code='same_ask', decision_key=dkey, log_id=rid,
                          earlier={'at': same['last_at'], 'first_at': same['first_at'], 'note': same.get('note_key'), 'channel': same.get('channel')})
        cid = content.get('commitment_id')
        older = [a for a in rows if a['decision_key'] != dkey and _stands(con, a) and (not cid or int(a.get('revision') or 0) != rev)]
        if older:
            was_ask = _ask_text(con, older[-1]['decision_key'])
            changed = {'earlier_ask': was_ask.get('ask') or '', 'earlier_at': older[-1]['last_at'], 'earlier_keys': [a['decision_key'] for a in older],
                       'ask_now': ask, 'earlier_revision': older[-1].get('revision'), 'revision': rev}
            set_aside = []
            if record:
                for a in older:
                    con.execute("UPDATE asks SET state='replaced' WHERE decision_key=? AND state='asked'", (a['decision_key'],))
                    set_aside += _set_aside_step(con, opp_key, a, now)
                t = _ts(now)
                con.execute("INSERT INTO asks(decision_key,commitment_id,revision,first_at,last_at,channel,state,note_key) VALUES(?,?,?,?,?,?,'pending',NULL) "
                            "ON CONFLICT(decision_key) DO UPDATE SET state='pending', last_at=excluded.last_at",
                            (dkey, str(ident), rev, t, t, content.get('channel') or 'note'))
                note(REPLACES, 'what is asked changed: the earlier ask no longer counts', decision_key=dkey,
                     detail={'ask': ask[:600], 'target': str(content.get('target') or '')[:600], 'changed': changed, 'steps_set_aside': set_aside})
            return answer(REPLACES, 'what is asked changed since the owner was last asked: the earlier ask is replaced and its yes no longer counts',
                          decision_key=dkey, replaced=[a['decision_key'] for a in older], changed=changed, steps_set_aside=set_aside)
        if same and same['state'] == 'pending':     # it replaced an earlier ask and has not gone yet: still carries what changed
            kept = _ask_text(con, dkey).get('changed')
            if kept:
                return answer(REPLACES, 'it replaces an earlier ask and has not been put to the owner yet', decision_key=dkey, replaced=kept.get('earlier_keys') or [], changed=kept)
        return answer(SEND, 'a decision that is the owner\'s, not asked before', decision_key=dkey)

    # a result, a deadline warning or routine information: only an exact repeat that already reached the owner is dropped
    fp = content.get('fingerprint')
    if fp and ident:
        for lid, note_key in con.execute("SELECT id,note_key FROM relevance_log WHERE verdict='told' AND commitment_id=? AND revision=? AND fp=? ORDER BY id DESC",
                                         (str(ident), rev, str(fp))).fetchall():
            st = con.execute('SELECT state FROM outbox WHERE key=?', (note_key,)).fetchone() if note_key else None
            if st is not None and tuple(st)[0] in ('sent', 'sent_unconfirmed', 'unknown', 'sending', 'logged'):
                rid = note(DUPLICATE, 'exactly this already reached the owner', note_key=note_key, fp=str(fp))
                return answer(DUPLICATE, 'exactly this already went to the owner in an earlier note', code='same_result', log_id=rid, earlier={'note': note_key, 'log_id': lid})
    if kind == 'routine':
        return answer(SEND, 'routine information: it goes on its own (there is no morning note to wait for)')
    return answer(SEND, 'finished work, or a failure: it always goes' if kind == 'result' else 'something that lands soon: it goes')


# ---- the notifier's three hooks --------------------------------------------------------------------------------------------

def _changed_words(changed: dict) -> str:
    """One plain fact for the note writer about an ask that replaces an earlier one. No field names."""
    when = _when_words(changed.get('earlier_at'))
    was, now_ask = str(changed.get('earlier_ask') or '').strip(), str(changed.get('ask_now') or '').strip()
    if was:
        return (f'You already asked the owner about this on {when}. It has changed since then. What you asked then: "{was}". What it is now: "{now_ask}". '
                'Say in a few words what changed, then ask the current question. An answer the owner gave to the earlier one does not count for this one.')
    return (f'You already asked the owner about this on {when}, and it has changed since then. Say that it changed, then ask the current question. '
            'An answer the owner gave to the earlier one does not count for this one.')


def screen(con, life, now_dt, keep: list, out: dict) -> list:
    """notifier.deliver, after its quiet-hour gates, the recheck and fresh.gate, right before the questions are paced
    (notifier.most_pressing): each item gets the one decision. -> the items that go in this note.
    An unchanged ask goes back to waiting on the owner's word (it points at the note that asked it). An exact repeat of
    something that already reached the owner is closed with that reason. Routine information is moved to the next morning
    note; it goes on its own. An ask that replaces an earlier one stays, and carries what changed for the writer.
    Any trouble with one item: that item goes exactly as it would have without this."""
    kept, seen = [], {}
    try:
        ensure(con)
        sync_chat_asks(con, now_dt)
    except Exception as e:
        out.setdefault('relevance', {})['error'] = f'{type(e).__name__}: {str(e)[:120]}'
        return keep
    rel = out.setdefault('relevance', {})

    def count(name):
        rel[name] = rel.get(name, 0) + 1

    def store(r, opp, state=None, surface_at=None):
        """One guarded write: only while the item is still ready (a drop from the owner's chat in the meantime wins)."""
        sets, args = ['payload=?', 'updated=?'], [json.dumps(opp, default=str), time.time()]
        if state:
            sets.append('state=?')
            args.append(state)
        if surface_at:
            sets.append('surface_at=?')
            args.append(surface_at)
        con.execute(f"UPDATE opps SET {', '.join(sets)} WHERE key=? AND state='ready'", (*args, r['key']))

    for r, opp in keep:
        try:
            c = content_of(con, r, opp, now_dt)
            d = decide(con, life, c, now_dt)
            v = d['verdict']
            mark = opp.get('relevance') if isinstance(opp.get('relevance'), dict) else {}
            new = dict(opp)                          # changes are made on a copy: if anything below fails, the item goes on untouched
            if v == SEND and c['kind'] == 'decision' and d.get('decision_key') in seen:
                v, d = DUPLICATE, d | {'code': 'same_ask_in_this_note', 'earlier': {'item': seen[d['decision_key']]}}
            if v == SEND:
                if c['kind'] == 'decision' and d.get('decision_key'):
                    seen[d['decision_key']] = r['key']
                kept.append((r, opp))
            elif v == REPLACES:
                new['relevance'] = mark | {'changed': d['changed'] | {'decision_key': d['decision_key'], 'at': _ts(now_dt)}}
                store(r, new)
                seen[d['decision_key']] = r['key']
                count('replaced_asks')
                kept.append((r, new))
            elif d.get('code') == 'same_result':
                new['told'] = {'at': _ts(now_dt), 'note': (d.get('earlier') or {}).get('note'), 'by': 'relevance', 'repeat_of_note': True}
                new['closed'] = {'state': 'done', 'at': time.time(), 'by': 'relevance', 'why': 'exactly this already reached the owner in an earlier note; not said twice'}
                store(r, new, state='done')
                count('duplicate_results')
            else:
                # an unchanged ask (or one the owner already answered): the item goes back to waiting on the owner's word, as if this
                # note had carried it, pointing at where it was really asked. Nothing is closed and nothing is lost.
                was = d.get('earlier') or {}
                new['told'] = {'at': float(was.get('at') or _ts(now_dt)), 'note': was.get('note'), 'by': 'relevance', 'same_ask': d.get('decision_key') or d.get('code'),
                               'why': d.get('why')}
                store(r, new, state='surfaced')
                count('answered_asks' if d.get('code') == 'answered' else 'duplicate_asks')
        except Exception as e:                       # one item's trouble never holds the note: it goes as before
            rel['errors'] = rel.get('errors', 0) + 1
            rel['last_error'] = f"{type(e).__name__}: {str(e)[:120]}"
            kept.append((r, opp))
    con.commit()
    if not rel:
        out.pop('relevance', None)
    return kept


def composing(compose, keep: list):
    """notifier.deliver, around the note writer: an item whose ask replaces an earlier one is handed one more plain
    fact (what the owner was asked then, what it is now), so the note can say what changed. With no such item this is the
    same writer, untouched. An item fresh.py already marked as replacing the owner's earlier ask (packet 'replaces', switch
    fresh-on) is left to fresh.py's own fact, so the change is never handed over twice."""
    facts = {}
    for i, (_r, opp) in enumerate(keep):
        ch = (opp.get('relevance') or {}).get('changed') if isinstance(opp.get('relevance'), dict) else None
        if isinstance(ch, dict) and not ch.get('told_in') and not (opp.get('packet') or {}).get('replaces'):
            facts[i] = _changed_words(ch)
    if not facts:
        return compose

    def with_what_changed(items, *a, **k):
        items = [dict(it) | ({'what_changed_since_you_last_asked_owner': facts[i]} if i in facts and i < len(keep) else {}) for i, it in enumerate(items)]
        return compose(items, *a, **k)
    return with_what_changed


def told(con, life, now_dt, note_key: str, keep: list, carried: list, out: dict) -> None:
    """notifier.deliver, after the note went and its items were marked: every question in it is written down as
    asked (so it is not repeated in the next note or by the chat), everything else as told. Never raises."""
    try:
        ensure(con)
        for r, opp in keep:
            if r['key'] not in carried:
                continue
            c = content_of(con, r, opp, now_dt)
            if c['kind'] == 'decision' and c.get('ask'):
                got = record_ask(con, life, c, now_dt, channel='note', note_key=note_key)
                ch = (opp.get('relevance') or {}).get('changed') if isinstance(opp.get('relevance'), dict) else None
                if isinstance(ch, dict) and not ch.get('told_in'):
                    row = con.execute('SELECT payload FROM opps WHERE key=?', (r['key'],)).fetchone()
                    p = json.loads(tuple(row)[0]) if row else None
                    if p is not None and isinstance((p.get('relevance') or {}).get('changed'), dict):
                        p['relevance']['changed']['told_in'] = note_key
                        con.execute('UPDATE opps SET payload=? WHERE key=?', (json.dumps(p, default=str), r['key']))
                out.setdefault('relevance', {}).setdefault('asked', []).append(got['decision_key'])
            else:
                _log(con, now_dt, opp_key=r['key'], ident=c['identity'], revision=c['revision'], kind=c['kind'], verdict='told', why='went in a note',
                     note_key=note_key, fp=c['fingerprint'])
        con.commit()
    except Exception as e:
        out.setdefault('relevance', {})['record_error'] = f'{type(e).__name__}: {str(e)[:120]}'


# ---- delivery states ------------------------------------------------------------------------------------------------------

def _verified(con, note_key):
    """-> True (read back in the owner's Messages), False (the line gave a receipt but it was not found), None (not read back)."""
    if not _has(con, 'relevance_log'):
        return None
    row = con.execute("SELECT verdict FROM relevance_log WHERE kind='delivery' AND note_key=? AND verdict IN ('verified','not_found') ORDER BY id DESC LIMIT 1",
                      (note_key,)).fetchone()
    return None if row is None else tuple(row)[0] == 'verified'


def note_state(con, note_key) -> dict:
    """One note of the outbox -> {'state', 'why', 'outbox', 'at'}. The outbox's 'sent' is the line's receipt, nothing
    more: only a read-back makes it delivered_verified."""
    row = con.execute('SELECT mode,at,state FROM outbox WHERE key=?', (note_key,)).fetchone() if note_key else None
    if row is None:
        return {'state': UNCERTAIN, 'why': 'the item names a note the outbox has no record of', 'outbox': None, 'at': None, 'note': note_key}
    mode, at, st = tuple(row)
    base = {'outbox': st, 'at': at, 'note': note_key}
    if mode != 'live' or st == 'logged':
        return base | {'state': NOT_SENT, 'why': 'sending is switched off: the note was written down, not sent'}
    if st == 'failed':
        return base | {'state': FAILED, 'why': 'it provably did not reach the owner'}
    if st == 'sent':
        v = _verified(con, note_key)
        if v is True:
            return base | {'state': DELIVERED, 'why': 'read back in the owner\'s Messages'}
        if v is False:
            return base | {'state': UNCERTAIN, 'why': 'the line gave a receipt, but the message was not found in the owner\'s Messages'}
        return base | {'state': ACCEPTED, 'why': 'the line gave a receipt; it has not been read back in the owner\'s Messages'}
    why = {'sending': 'a run ended while it was being sent; it has not been settled yet',
           'unknown': 'the line gave no receipt; it may or may not have gone',
           'sent_unconfirmed': 'it could not be confirmed either way after 30 minutes; it was not sent again'}.get(st, f'the outbox says "{st}"')
    return base | {'state': UNCERTAIN, 'why': why}


def _notes_of(con, opp_key, p: dict) -> list:
    """Every live or shadow note that carried this item, newest first."""
    keys = {str(x[0]) for x in con.execute('SELECT note FROM note_items WHERE opp=?', (str(opp_key),)).fetchall()} if _has(con, 'note_items') else set()
    told_note = (p.get('told') or {}).get('note') if isinstance(p.get('told'), dict) else None
    if told_note and not str(told_note).startswith('chat:'):
        keys.add(str(told_note))
    rows = [(k, con.execute('SELECT at FROM outbox WHERE key=?', (k,)).fetchone()) for k in keys]
    return [k for k, at in sorted(rows, key=lambda x: -(float(tuple(x[1])[0] or 0) if x[1] else 0))]


def delivery_state(con, opp_key) -> dict:
    """Where one item stands with the owner -> {'state': one of DELIVERY_STATES (or 'waiting' / 'not_sent' when nothing was
    attempted yet), 'why': the recorded reason in plain words, 'note', 'outbox'}. Read only. Never says delivered
    without a read-back, and a result that needed no notification is closed WITH its reason."""
    row = con.execute('SELECT state,surface_at,payload FROM opps WHERE key=?', (str(opp_key),)).fetchone()
    if row is None:
        return {'state': None, 'why': 'no such item', 'note': None, 'outbox': None}
    state, surface_at, payload = tuple(row)
    try:
        p = json.loads(payload)
    except (TypeError, ValueError):
        return {'state': UNCERTAIN, 'why': 'the item could not be read', 'note': None, 'outbox': None}
    told_ = p.get('told') if isinstance(p.get('told'), dict) else {}
    closed = p.get('closed') if isinstance(p.get('closed'), dict) else {}
    notes = _notes_of(con, opp_key, p)
    if notes:
        ns = note_state(con, notes[0])
        if ns['state'] == FAILED and state == 'ready':
            ns = ns | {'why': 'the note provably did not reach the owner; the item is waiting to go again'}
        if ns['state'] not in (FAILED, NOT_SENT) or state in ('ready', 'surfaced', 'done'):
            return {'item': state} | ns
    if state == 'declined' or closed.get('state') == 'declined':
        return {'state': SET_ASIDE, 'why': str(closed.get('why') or 'it was stopped or declined; no reason was recorded'), 'note': None, 'outbox': None, 'item': state}
    if told_.get('silent'):
        return {'state': CLOSED_QUIET, 'why': 'the note writer judged it not worth a text', 'note': None, 'outbox': None, 'item': state}
    if told_.get('by') == 'relevance' and told_.get('same_ask') == 'answered':
        return {'state': CLOSED_QUIET, 'why': 'the owner had already answered it, so it was not asked again', 'note': None, 'outbox': None, 'item': state}
    if str(told_.get('note') or '').startswith('chat:') or told_.get('by') == 'zeus chat':
        return {'state': ACCEPTED, 'why': 'it was told in the owner\'s chat; no receipt for a chat reply is kept here', 'note': told_.get('note'), 'outbox': None, 'item': state}
    if closed or state in ('done', 'moot'):
        return {'state': CLOSED_QUIET, 'why': str(closed.get('why') or 'closed; no reason was recorded'), 'note': None, 'outbox': None, 'item': state}
    held = p.get('held_overnight') if isinstance(p.get('held_overnight'), dict) else None
    if state == 'ready' and held:
        return {'state': WAITING, 'why': f"held for quiet hours until {held.get('until')}: {held.get('why')}", 'note': None, 'outbox': None, 'item': state}
    words = {'ready': 'finished, waiting for the next note', 'pending': 'not worked yet', 'executing': 'being worked in the background',
             'surfaced': 'marked as put to the owner, but no note or chat stamp says where', 'needs_attention': 'it needs attention before anything can be told'}
    return {'state': UNCERTAIN if state == 'surfaced' else WAITING, 'why': words.get(state, f'the item is "{state}"'), 'note': None, 'outbox': None, 'item': state}


def unresolved(con, now=None) -> list:
    """Live notes whose delivery is still not known after 30 minutes -> [{'note', 'state', 'why', 'age_s'}]. A held
    item has no note, so a quiet-hour hold is never in this list."""
    t = _ts(now)
    out = []
    for key, at in con.execute("SELECT key,at FROM outbox WHERE mode='live' AND state IN ('sending','unknown','sent_unconfirmed','sent') AND at>=? ORDER BY at",
                               (t - TICKET_WINDOW,)).fetchall():
        ns = note_state(con, key)
        if ns['state'] == UNCERTAIN and t - float(at or 0) > UNRESOLVED_AFTER:
            out.append({'note': key, 'state': ns['state'], 'why': ns['why'], 'age_s': round(t - float(at or 0))})
    return out


def tend(con, life, now_dt, live: bool, *, line=None, alarm=None):
    """Every tick, after the notifier and the watchdog. Sends nothing. -> counts, or None when there was nothing to do.
    1. Asks the chat put to the owner are written down (from the ledger).
    2. live only: a note the line accepted is read back in the owner's Messages (read-only, a few tries), so "delivered" is
       only ever said with proof.
    3. live only: a delivery still unresolved after 30 minutes gets the existing repair ticket, once per note."""
    ensure(con)
    out = {}
    t = _ts(now_dt)
    n = sync_chat_asks(con, now_dt)
    if n:
        out['chat_asks'] = n
    if live:
        todo = []
        for key, text, at in con.execute("SELECT key,text,at FROM outbox WHERE mode='live' AND state='sent' AND at>=? ORDER BY at", (t - VERIFY_WINDOW,)).fetchall():
            tries = [tuple(x)[0] for x in con.execute("SELECT verdict FROM relevance_log WHERE kind='delivery' AND note_key=? AND verdict IN ('verified','not_found','cannot_tell')",
                                                      (key,)).fetchall()]
            if 'verified' not in tries and len(tries) < VERIFY_TRIES:
                todo.append((key, text, at))
        if todo:
            if line is None:
                import notifier as NT
                (Path(life) / 'line').mkdir(parents=True, exist_ok=True)
                line = NT._line(Path(life) / 'line')
            for key, text, at in todo:
                try:
                    ok = line.readback(text, float(at or t) - 5)
                except Exception:
                    ok = None
                verdict = 'verified' if ok is True else 'not_found' if ok is False else 'cannot_tell'
                _log(con, t, kind='delivery', verdict=verdict, note_key=key, why='read back in the owner\'s Messages' if ok is True else 'not found in the owner\'s Messages' if ok is False
                     else 'the owner\'s Messages could not be read just now')
                out[verdict] = out.get(verdict, 0) + 1
        for u in unresolved(con, t):
            if con.execute("SELECT 1 FROM relevance_log WHERE kind='delivery' AND verdict='ticket' AND note_key=?", (u['note'],)).fetchone():
                continue
            try:
                if alarm is None:
                    import worker as W
                    alarm = W._alarm
                took = alarm(Path(life), u['note'], TICKET_TITLE, kind=TICKET_KIND, detail=f"delivery state: {u['why']}")
            except Exception:
                took = False
            if took is False:                       # the repair-ticket store could not be reached: tried again on the next run
                out['ticket_failed'] = out.get('ticket_failed', 0) + 1
                continue
            _log(con, t, kind='delivery', verdict='ticket', note_key=u['note'], why=u['why'])
            out['tickets'] = out.get('tickets', 0) + 1
    con.commit()
    return out or None


def summary(con, now=None, days: float = 1.0) -> dict:
    """Counts for the health note and the nightly closure: notes of the last `days` by delivery state, and the items
    held for quiet hours right now."""
    t = _ts(now)
    states = {}
    for (key,) in con.execute("SELECT key FROM outbox WHERE mode='live' AND at>=?", (t - days * 86400,)).fetchall():
        s = note_state(con, key)['state']
        states[s] = states.get(s, 0) + 1
    late = unresolved(con, t)
    held = 0
    for (until,) in con.execute("SELECT json_extract(payload,'$.held_overnight.until') FROM opps WHERE state='ready' AND json_extract(payload,'$.held_overnight.until') IS NOT NULL"):
        m = _moment(until)
        held += bool(m and m.timestamp() > t)
    tickets = con.execute("SELECT COUNT(*) FROM relevance_log WHERE kind='delivery' AND verdict='ticket' AND at>=?", (t - days * 86400,)).fetchone()[0] \
        if _has(con, 'relevance_log') else 0
    return {'notes': sum(states.values()), 'by_state': states, 'unresolved_over_30_min': [u['note'] for u in late][:10],
            'oldest_unresolved_minutes': round(max([u['age_s'] for u in late] or [0]) / 60), 'held_for_quiet_hours': held, 'tickets_raised': tickets}


def health_part(life, note: dict) -> None:
    """tick.health_note's call. Only with the switch relevance-on: a small `deliveries` part (the last day's notes by
    delivery state, what is unresolved, what is held for quiet hours). Read only. Without the switch the note is
    exactly what it was. Never raises."""
    try:
        life = Path(life)
        if not on(life) or not (life / 'ledger.sqlite3').exists():
            return
        with closing(sqlite3.connect(f"file:{life / 'ledger.sqlite3'}?mode=ro", uri=True, timeout=5)) as con:
            try:
                note['deliveries'] = {'readable': True} | summary(con, note.get('when_ts'))
            except sqlite3.OperationalError as e:
                note['deliveries'] = {'readable': False, 'why': str(e)[:120]}
        note.setdefault('switches', {})[SWITCH] = True
    except Exception as e:
        note['relevance_health_error'] = f'{type(e).__name__}: {str(e)[:120]}'


# ---- `life relevance <verb>`: for the chat ---------------------------------------------------------------------------------

def cli(a: list, life, db) -> int:
    """`life relevance decide|asked '<json>'`, `life relevance delivery|target <item key>`: one JSON object out.
    decide: the one decision for something the chat is about to say. asked: the chat put a decision to the owner."""
    verb = a[0] if a else ''
    try:
        arg = a[1] if len(a) > 1 else sys.stdin.read()
        with closing(db()) as con:
            if verb in ('decide', 'asked'):
                content = json.loads(arg)
                if not isinstance(content, dict):
                    raise ValueError('one JSON object is needed')
                if content.get('opp_key') and not content.get('commitment_id'):
                    row = con.execute('SELECT payload FROM opps WHERE key=?', (content['opp_key'],)).fetchone()
                    if row:
                        ident, rev, cid = identity(con, content['opp_key'], json.loads(tuple(row)[0]))
                        content = {'identity': ident, 'revision': rev, 'commitment_id': cid} | content
                got = decide(con, life, content | {'channel': 'chat'}) if verb == 'decide' else record_ask(con, life, content, channel='chat')
                con.commit()
            elif verb == 'delivery':
                got = delivery_state(con, arg.strip())
            elif verb == 'target':
                got = {'target': approval_target(con, arg.strip())}
            else:
                print('Usage: life relevance decide|asked \'<one JSON object>\'  |  life relevance delivery|target <item key>')
                return 2
        print(json.dumps(got, ensure_ascii=False, default=str))
        return 0
    except (ValueError, sqlite3.Error) as e:
        print(json.dumps({'status': 'refused', 'why': str(e)[:300]}))
        return 2

"""onesend: one yes, one send. The flows around the action record (actions.py).

The rules this implements: ordinary code controls approvals and execution; a yes keeps exact draft binding and
duplicate/no-replay checks; before retrying any uncertain send or write, the destination is checked; approvals are
plain words, never a code, and "ok" never approves; a send whose every recipient is on the owner's configured team
(settings.TEAM) needs no yes. When a thing changes after Zeus asked, the draft is redone and a fresh ask replaces the
old one; an ask that is never answered ends when the thing's own deadline passes.

What code does, with the switch file `one-send-on` in the life folder (without it every door here answers "not
mine" and the engine runs without it):

  worker   A job may hand back a whole email reply (job_prompt, with_send). When a result with a step that needs the
           owner's yes is saved, that step is frozen as an action record (on_ready). A standing-OK job is marked
           "being carried out" BEFORE it is booked (may_start), and a second run is only booked after reading what
           the first one did (may_retry).
  chat     `life approve <item> "<the owner's message>"` (approve): the REAL message is found in the chat store and
           read on its own (hisreply.py). A plain yes: code sends the stored email reply itself, word for word, once;
           for any other kind of step it records the hand-over and prints the exact step. A yes with a change:
           nothing runs, the ask is closed as replaced and one plain instruction is printed (redo, `life redraft`,
           ask again). A no: declined. Two things waiting and a bare yes: "which one?", once, and nothing runs.
  tick     tend(): a "sending" nobody is left to finish becomes "unknown"; an unknown email is settled ONLY by reading
           the thread; an ask whose deadline passed expires; with the second switch `one-send-team-on`, a reply whose
           every recipient is on the team is sent without asking and the owner is told after.

What code really sends: ONE kind of step, an email reply inside the thread the item came from, to people already on
that thread (sendmail.py). A new email, a text, a booking, an order: the chat Zeus still does those with its own
tools; code only keeps the record around them. Notices to other agents are not supported (see peer_notice).

Every line printed by approve / redraft is read by the Zeus in their chat and may be repeated to them: plain words.
"""
from __future__ import annotations

import json
import os
import re
import hashlib
import subprocess
import time
from datetime import datetime
from pathlib import Path

import actions as A
import hisreply as H
import settings

LIVE_HOME = Path(settings.get('paths.life_home', str(settings.PROFILE_DIR / 'workspace' / 'life-engine')))   # the real life folder (never the LIFE_HOME env of a sandbox)
TEAM_SWITCH = 'one-send-team-on'
TOOL = 'life'
RECENT = 15 * 60            # their reply must be this fresh (the same 15 minutes as sendmail.OWNER_FRESH)
WHICH_KEEPS = 60 * 60       # how long an open "which one?" waits for them to name it
SEND_PATIENCE = 5 * 60      # an email "sending" older than this has nobody left to finish it (the send itself gets 90 seconds)
STEP_PATIENCE = 20 * 60     # a step handed to the chat Zeus and not reported back (actcheck.UNFINISHED_AFTER)
SETTLE_WAIT = 10 * 60       # an unknown email is called "did not go" only after two reads at least this long after the attempt
WHICH_FILE = 'one-send-which.json'
STEP_KINDS = ('email_reply', 'step', 'peer_notice')
# The one line added to the background job's instructions.
SEND_ANCHOR = ' "followups": [{"when": "ISO time with offset", "check": "what to check then", "tell_if": "when it is worth telling them"}],\n'
SEND_LINE = (' "send": null, or ONLY when the step that needs their yes is an email reply in the very thread this item came from: {"to": ["address"], '
             '"cc": [], "body": "the complete reply exactly as it should go out, from greeting to sign-off, plain text"}. After their yes your pipes '
             'send exactly these words from their mailbox, so they must be the same reply that "ready" describes, with no blank left to fill in, and '
             'only people who are already on that thread. Leave it null for anything else (a new email, a text, a task or calendar change),\n')
_flight = {}                # the standing-OK attempt whose job is being booked right now (set by may_start / may_retry, used by wrap_create)


def _mail():
    import sendmail as M
    return M


def _sandbox(life_home) -> bool:
    return Path(life_home).resolve() != LIVE_HOME.resolve()


# ---- small readers ---------------------------------------------------------------------------------------------------

def facts_version(opp: dict) -> str:
    """The version of the facts a step was built from: the newest email of its thread and the item's own times.
    New mail merged into the item, or a moved event or deadline, changes it."""
    mail = opp.get('latest_email') or {}
    return json.dumps([mail.get('message_id'), opp.get('event_start'), opp.get('deadline')], default=str)


def team_addresses(life_home):
    """The mailboxes of the owner's configured team (settings.TEAM, each member's 'emails'), or None when there is no
    team (then nobody counts as team). LIFE_TEAM_FILE points a sandbox at a pretend list (a JSON list of members, or
    {"members": [...]}); the live folder always reads settings."""
    members = settings.TEAM
    named = os.environ.get('LIFE_TEAM_FILE')
    try:
        if named and _sandbox(life_home):
            got = json.loads(Path(named).read_text())
            members = (got.get('members') or got.get('agents') or []) if isinstance(got, dict) else got
        S = _mail().S
        found = {S.same_mailbox(a) for m in members or [] if isinstance(m, dict) for e in (m.get('emails') or []) for a in S._ADDRESS.findall(str(e))}
    except Exception:
        return None
    return {a for a in found if a} or None


def team_only(draft: dict, life_home) -> bool:
    """Is every recipient on the owner's team? Unknown roster, nobody named, or one outside address: no."""
    team = team_addresses(life_home)
    everyone = list(draft.get('to') or []) + list(draft.get('cc') or [])
    S = _mail().S
    return bool(team) and bool(everyone) and all(S.same_mailbox(a) in team for a in everyone)


def _draft(rec: dict) -> dict:
    d = rec['details']
    return {'kind': 'email_reply'} | {k: d.get(k) for k in ('mailbox', 'thread_id', 'reply_to', 'to', 'cc', 'body')}


def _who(rec: dict) -> str:
    return ', '.join(rec['details'].get('who') or []) or 'the people on it'


def _vocab(title, ask, who=None) -> set:
    names = []
    for a in who or []:
        names += re.split(r'[@._\-+]', str(a))
    return H.vocabulary(title, ask, names)


def open_asks(con) -> list:
    """Everything waiting on their word right now: prepared steps the owner was told about, and offers of a standing way of
    working. A bare "yes" can only mean one of them when there is exactly one."""
    out = []
    for r in con.execute("SELECT key,payload FROM opps WHERE state='surfaced' ORDER BY updated").fetchall():
        try:
            p = json.loads(r[1])
        except ValueError:
            continue
        d = (p.get('packet') or {}).get('decision') or {}
        offer = isinstance(p.get('offer'), dict) and p['offer'].get('rule_key')
        if p.get('closed') or p.get('approved') or not (offer or ((d.get('ask') or d.get('ready')) and d.get('by') != 'owner')):
            continue
        send = (p.get('packet') or {}).get('send') or {}
        out.append({'key': r[0], 'title': str(p.get('title') or ''), 'ask': str(d.get('ask') or ''),
                    'vocab': _vocab(p.get('title'), d.get('ask'), list(send.get('to') or []) + list(send.get('cc') or []))})
    return out


def his_reply(words: str, since: float, now: float, life_home):
    """Their real message to read: the newest one that carries these words, or else simply their newest one, since the
    ask and at most 15 minutes old. -> (message, their later messages) or (None, []) when the owner sent none in that time.
    Raises when the chat store cannot be read. A sandbox must name
    its own pretend chat store (LIFE_CHAT_DB): it never falls back to their real one."""
    import chats as C
    store = os.environ.get('LIFE_CHAT_DB') or None
    if store is None and _sandbox(life_home):
        raise RuntimeError('a sandbox has no chat store of its own')
    msgs = [m for m in C.messages(max(now - RECENT, float(since or 0) - 1), path=store) if m['who'] == 'owner']
    if not msgs:
        return None, []
    quote = H._flat(words)
    hits = [m for m in msgs if quote and quote in H._flat(m['text'])]
    # The words the chat model passes only help to pick among their recent messages. When they match none (it tidied a
    # typo, it paraphrased), their newest message is the one that is read. What the owner really wrote decides, never the quote.
    found = hits[-1] if hits else msgs[-1]
    return found, [m for m in msgs if (m['at'], m['id']) > (found['at'], found['id'])]


JUST_ASKED = 15 * 60      # how long after an ask the owner's reply still plainly answers it
# After the owner's yes, a step not yet done is one line, never the setup read back.
NOT_YET = (' If it is not on or not done yet, tell them in one short line what is not on yet and what happens next. '
           'Never read the step or its settings back to them: the owner has seen them.')


def _asked_at(con, key: str):
    """When this ask was put to them: its record's 'asked', else when its item says the owner was told. None = not known."""
    rec = A.latest(con, key)
    at = A.last(rec, 'asked').get('at') if rec else None
    if at:
        return float(at)
    row = con.execute('SELECT payload FROM opps WHERE key=?', (key,)).fetchone()
    try:
        return float((json.loads(row[0]).get('told') or {})['at'])
    except (TypeError, ValueError, KeyError):
        return None


def just_asked(con, rec: dict, msg: dict, others: list, life_home) -> bool:
    """Is their message the answer to THIS ask, by the times on record? All of: this ask was put to them at most 15
    minutes before their message; every other open ask was put to them at least a minute before this one (so this is the
    ask Zeus just made); and the owner wrote nothing else in between. Any time that cannot be read means no."""
    try:
        asked = float(A.last(rec, 'asked').get('at') or 0)
        if not asked or not (asked - 1 <= float(msg['at']) <= asked + JUST_ASKED):
            return False
        for other in others:
            at = _asked_at(con, other['key'])
            if at is None or at > asked - 60:
                return False
        import chats as C
        store = os.environ.get('LIFE_CHAT_DB') or None
        if store is None and _sandbox(life_home):
            return False
        between = [m for m in C.messages(asked - 1, path=store) if m['who'] == 'owner' and (m['at'], m['id']) < (msg['at'], msg['id'])]
        return not between
    except Exception:
        return False


def _chat_store(life_home):
    """Their chat store to read, or None: a sandbox reads only its own pretend store (LIFE_CHAT_DB), never their real one."""
    store = os.environ.get('LIFE_CHAT_DB') or None
    if store is None and _sandbox(life_home):
        return None
    import chats as C
    return store or C.STORE


def _last_said(con, msg: dict, life_home):
    """What Zeus's last message before the owner's was about (lastsaid.py), or None when that cannot be read."""
    try:
        store = _chat_store(life_home)
        return None if store is None else __import__('lastsaid').last(con, float(msg['at']), store=store)
    except Exception:
        return None


def _nothing_between(since: float, msg: dict, life_home) -> bool:
    """The owner wrote nothing else between Zeus's last message and this one. Unreadable: False."""
    try:
        store = _chat_store(life_home)
        if store is None:
            return False
        import chats as C
        return not [m for m in C.messages(since, path=store) if m['who'] == 'owner' and (m['at'], m['id']) < (msg['at'], msg['id'])]
    except Exception:
        return False


def answers_the_last_ask(con, rec: dict, msg: dict, reading: dict, others: list, life_home) -> dict:
    """"Sounds good" is a yes only to the question Zeus just asked.
    What Zeus's last message to them was about decides, from stored facts (lastsaid.py):
      "sounds good"   their yes only when that last message asked about THIS one and nothing else, at most 15 minutes
                      before, with nothing else from them in between. After a heads-up that asked nothing: not a yes.
      a plain yes/no  names this one when that last message was about it alone; when it was about one other ask it
                      answers that one ('answers'); after news, several asks at once, or (with other asks open) an ask
                      more than 15 minutes old, it is not tied ('unbound': Zeus asks once which). A yes that names its step
                      in their own words is left as it was.
    Not known (the chat store could not be read): "sounds good" is not a yes, and a plain yes or no reads as before.
    Everything after this is unchanged: one message decides one thing, and a step under way is never run twice."""
    key = rec['item']
    last = _last_said(con, msg, life_home)
    if reading.get('answer') == 'other' and H.agrees(msg['text']):
        if last and last['asks'] == [key] and last['question'] and float(msg['at']) - last['at'] <= JUST_ASKED \
                and _nothing_between(last['at'], msg, life_home):
            return dict(reading, answer='yes', bare=True, points=True, why='the owner said "sounds good" right after this was put to them')
        return reading
    if reading.get('answer') not in ('yes', 'no') or reading.get('points') or reading.get('all'):
        return reading
    if last is None:
        if others and just_asked(con, rec, msg, others, life_home):
            return dict(reading, points=True, why=str(reading.get('why') or '') + ', right after this was put to them')
        return reading
    fresh = float(msg['at']) - last['at'] <= JUST_ASKED or not others      # with other asks open, only a fresh ask binds a bare yes
    if last['asks'] == [key] and fresh:
        return dict(reading, points=True, why=str(reading.get('why') or '') + ', right after this was put to them')
    if len(last['asks']) == 1 and fresh:
        return dict(reading, answers=last['asks'][0])
    return dict(reading, unbound=True)


def used_before(con, message_id, but_not: str, now: float):
    """Has this very message of theirs already decided another step? One message, one step."""
    for rec in A.in_states(con, A.STATES):
        if rec['id'] == but_not or rec['updated'] < now - 2 * 86400:
            continue
        if any(h.get('message_id') == message_id and h.get('state') in ('approved', 'declined', 'replaced') for h in rec['history']):
            return rec
    return None


def _which(life_home, now: float):
    try:
        w = json.loads((Path(life_home) / WHICH_FILE).read_text())
        return w if now - float(w['asked_at']) <= WHICH_KEEPS else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save(con, key: str, p: dict, state: str | None = None, surface_at: str | None = None):
    sets, vals = ['payload=?', 'updated=?'], [json.dumps(p, default=str), time.time()]
    if state:
        sets.append('state=?')
        vals.append(state)
    if surface_at:
        sets.append('surface_at=?')
        vals.append(surface_at)
    con.execute(f"UPDATE opps SET {', '.join(sets)} WHERE key=?", (*vals, key))
    con.commit()


def _closer():
    import lifecli
    return lifecli.close


# ---- freezing a step -------------------------------------------------------------------------------------------------

def freeze(con, key: str, opp: dict, packet: dict, life_home, why: str = 'prepared') -> dict:
    """Freeze the step this result puts to them as one action record. An earlier one still waiting on the same item is
    replaced by it; one that is already under way is left alone and nothing new is frozen."""
    old = A.latest(con, key)
    if (old and old['kind'] == 'peer_notice' and old['details'].get('peer_notice') == packet.get('peer_notice')
            and old['state'] in A.WAITING):
        return old
    if old and old['kind'] in STEP_KINDS:
        if old['state'] in ('approved', 'sending', 'unknown'):
            return old
        if old['state'] in A.WAITING:
            A.move(con, old['id'], 'replaced', 'a newer version was prepared')
    dec = packet.get('decision') or {}
    send = packet.get('send') if isinstance(packet.get('send'), dict) else None
    title = str(opp.get('title') or '')[:200]
    if packet.get('peer_notice'):
        spec = peer_notice(packet['peer_notice'])
        idx = __import__('integrity').ensure(con,life_home,key)
        if not idx:
            raise ValueError('A team send requires its original commitment.')
        details = {'peer_notice':spec,'who':spec['recipients'],'words':peer_offer(spec),
                   'ask':peer_offer(spec),'title':title,'cid':idx['commitment_id'],'revision':idx['revision']}
        return A.new(con,key,'peer_notice',details,facts_version(opp),why=why)
    if send and send.get('kind') == 'email_reply':
        S = _mail().S
        details = {k: send.get(k) for k in ('mailbox', 'thread_id', 'reply_to', 'to', 'cc', 'body')}
        details |= {'who': list(send.get('to') or []) + list(send.get('cc') or []), 'account': S.MAILBOX_ADDRESS.get(send.get('mailbox')),
                    'words': send.get('body'), 'ask': dec.get('ask'), 'title': title, 'team_only': team_only(send, life_home)}
        return A.new(con, key, 'email_reply', details, facts_version(opp), why=why)
    details = {'who': None, 'account': None, 'words': dec.get('ready') or dec.get('ask'), 'ask': dec.get('ask'), 'title': title}
    return A.new(con, key, 'step', details, facts_version(opp), why=why)


# ---- worker hooks ----------------------------------------------------------------------------------------------------

def job_prompt(prompt: str) -> str:
    """The job's instructions with the one line that lets it hand back a whole email reply. If the place for that
    line is not found exactly once the instructions are left as they are (the job then hands back no reply and the
    step stays with the chat Zeus, as before)."""
    return prompt.replace(SEND_ANCHOR, SEND_LINE + SEND_ANCHOR) if prompt.count(SEND_ANCHOR) == 1 else prompt


def wrap_create(create, life_home):
    """Hook in worker.run. Switch off: the same function comes back. On: jobs get the extra line, and a standing-OK
    attempt that is being booked learns how the booking ended."""
    if not A.on(life_home):
        return create

    def booked(prompt, run_at, name):
        flight = dict(_flight) if _flight and _flight.get('title', '') in str(name) and 'granted' in str(name) else {}
        _flight.clear()
        try:
            job = create(job_prompt(prompt), run_at, name)
        except subprocess.TimeoutExpired:
            if flight:
                A.move(flight['con'], flight['id'], 'unknown', 'booking the job gave no answer, so a job may or may not exist')
            raise
        except Exception:
            if flight:
                A.move(flight['con'], flight['id'], 'did_not_run', 'the job could not be booked')
            raise
        if flight:
            A.note(flight['con'], flight['id'], 'job booked', job=job)
        return job
    return booked


def with_send(con, packet, text):
    """Hook in worker._harvest_one, right after the job's answer is parsed: keep the whole reply the job wrote out
    ("send": people and words). Only for a step Zeus can carry out itself. Switch off: the result is untouched."""
    try:
        if packet is None or packet.get('unparsed') or not A.on_for(con):
            return packet
        raw = str(text or '')
        obj = json.loads(raw[raw.find('{'):raw.rfind('}') + 1])
        send, dec = obj.get('send'), packet.get('decision')
        if dec is not None and dec.get('by') == 'zeus' and isinstance(send, dict) and str(send.get('body') or '').strip():
            listed = lambda v: [str(x)[:200] for x in v if x][:6] if isinstance(v, list) else ([str(v)[:200]] if isinstance(v, str) and v else [])
            packet['send'] = {'to': listed(send.get('to')), 'cc': listed(send.get('cc')), 'body': str(send['body']).strip()[:6000]}
    except Exception:
        pass
    return packet


def on_ready(con, key: str, opp: dict, packet: dict):
    """Hook at the top of worker._ready. Completes the reply from the item's own thread (which mailbox and which
    thread never come from the job), freezes the step as an action record, and writes down how a standing-OK job
    ended. Never raises: a fault here leaves the item exactly on the old path, with one line saying so."""
    try:
        if not A.on_for(con):
            return
        life_home = A.home_of(con)
        A.ready(con)
        raw = packet.pop('send', None)
        _granted_outcome(con, key, opp, packet)
        dec = packet.get('decision')
        if packet.get('status') in ('moot', 'nothing_useful') or not dec or dec.get('by') == 'owner' or not (dec.get('ask') or dec.get('ready')):
            return
        if raw:
            M = _mail()
            draft = M.from_job(raw, opp)
            if draft:
                packet['send'] = draft | {'fingerprint': M.fingerprint(draft)}
        freeze(con, key, opp, packet, life_home)
    except Exception as e:
        opp['one_send_error'] = f'{type(e).__name__}: {str(e)[:160]}'


def _granted_words(opp: dict) -> str:
    import worker as W
    return '; '.join(W.standing_ok(opp))[:300]


def _granted_start(con, key: str, opp: dict, attempt: int) -> dict:
    by = (opp.get('decision') or {}).get('granted_by') or {}
    details = {'who': None, 'account': None, 'words': _granted_words(opp), 'ask': None, 'title': str(opp.get('title') or '')[:200],
               'his_words': by.get('his_words'), 'attempt': attempt}
    rec = A.new(con, key, 'granted_step', details, facts_version(opp), why='a step the owner gave a standing OK for')
    A.move(con, rec['id'], 'approved', 'their standing OK: "%s"' % str(by.get('his_words') or '')[:200])
    A.move(con, rec['id'], 'sending', 'a background job is being booked to carry it out', attempt=attempt)   # no patience: the harvest step settles it
    _flight.clear()
    _flight.update(con=con, id=rec['id'], title=str(opp.get('title'))[:40])
    return rec


def may_start(con, key: str, opp: dict, now_dt, res: dict) -> bool:
    """Hook in worker.run, before a job is booked. A step the owner gave a standing OK for is done for real by its job, so
    it is marked "being carried out" first. If the record says an earlier attempt already went, or may have gone,
    no job is booked: the owner is told plainly instead. Everything else: True."""
    try:
        import worker as W
        if not A.on_for(con) or not W.standing_ok(opp):
            return True
        A.ready(con)
        rec = A.latest(con, key, 'granted_step')
        if rec is None or rec['state'] == 'did_not_run':
            _granted_start(con, key, opp, 1 if rec is None else int(rec['details'].get('attempt') or 1) + 1)
            return True
        if rec['state'] == 'sending':
            A.move(con, rec['id'], 'unknown', 'an earlier run marked it as started and never came back')
        W._ready(con, key, opp, W.synth_packet(opp, 'not started again'), now_dt, res)
        return False
    except Exception as e:
        res.setdefault('errors', []).append(f'one-send start {key[:40]}: {type(e).__name__}: {str(e)[:80]}')
        return True


def may_retry(con, key: str, opp: dict, ex: dict, audit) -> bool:
    """Hook in worker._harvest_one, before a failed job is booked a second time. For a standing-OK step the first
    run may already have done it, so its own record is read first: only when that shows it did nothing is a second
    run booked. It did something, or what it did cannot be read: unknown, and never tried again."""
    try:
        import worker as W
        if not A.on_for(con) or not W.standing_ok(opp):
            return True
        A.ready(con)
        rec = A.latest(con, key, 'granted_step')
        if 'never ran' in str(ex.get('dropped') or ''):
            did = []
        else:
            try:
                did = audit(ex.get('job_id')) if audit else None
            except Exception:
                did = None
        changes = None if did is None else [str(x) for x in did if not str(x).startswith('private read')]
        if rec is not None and rec['state'] == 'sending':
            if changes is None:
                A.move(con, rec['id'], 'unknown', 'the first run failed and what it did could not be read')
            elif changes:
                A.move(con, rec['id'], 'unknown', 'the first run failed after it had already sent or changed something', receipt={'did': changes[:5], 'job': ex.get('job_id')})
            else:
                A.move(con, rec['id'], 'did_not_run', 'the first run was read back: it sent and changed nothing')
        if changes is None or changes:
            return False
        _granted_start(con, key, opp, int((rec or {}).get('details', {}).get('attempt') or 1) + 1)
        return True
    except Exception:
        return False        # when in doubt a real-world step is not started a second time


def _granted_outcome(con, key: str, opp: dict, packet: dict):
    """How a standing-OK job ended, from what the audit read back (worker._harvest_one keeps that on the item)."""
    rec = A.latest(con, key, 'granted_step')
    if rec is None:
        return
    ex = opp.get('exec') or {}
    stand_in = bool(packet.get('prep'))                 # the engine's own stand-in result: the job handed back none
    if rec['state'] == 'sending':
        did = ex.get('did_under_standing_ok') or ex.get('broke_a_limit')
        if stand_in:
            A.move(con, rec['id'], 'unknown', 'the run ended without a result')
        elif ex.get('audit_skipped'):
            A.move(con, rec['id'], 'unknown', 'the job finished, and what it did could not be read back')
        elif did:
            A.move(con, rec['id'], 'sent', 'the job carried it out', receipt={'did': [str(x)[:200] for x in did][:5], 'job': ex.get('job_id')})
        else:
            A.move(con, rec['id'], 'did_not_run', 'the job finished without sending or changing anything')
        rec = A.get(con, rec['id'])
    step = rec['details'].get('words') or 'the step'
    if rec['state'] == 'unknown' and not A.notes(rec, 'told'):
        found = (rec.get('receipt') or {}).get('did')
        packet.update(status='done', decision=None,
                      headline=f'I started on this under your standing OK and the run stopped before it reported back, so it may or may not have been done: {step}',
                      details=('What I found when I read the run back: ' + '; '.join(found) + '. ' if found else 'I could not read back what the run did. ')
                      + 'I have not tried it a second time.')
        A.note(con, rec['id'], 'told')
    elif rec['state'] == 'sent' and stand_in:
        packet.update(status='done', decision=None, headline=f'This was already done under your standing OK, and I did not do it again: {step}')
    elif rec['state'] == 'did_not_run' and stand_in:
        packet['details'] = (str(packet.get('details') or '') + ' The step you gave a standing OK for was not done: ' + step).strip()


# ---- the chat side: their answer ---------------------------------------------------------------------------------------

def chat(cmd: str, con, r, p: dict, words: list, life_home, close) -> int | None:
    """Hook in lifecli.main for `approve` and `redraft`. -> an exit code, or None when this is not ours (switch off,
    or nothing here was prepared for their yes): lifecli then goes on exactly as before."""
    if not A.on(life_home):
        return None
    A.ready(con)
    if isinstance(p.get('offer'), dict):              # an offer of a standing way of working is answered with `life rule`, never here
        if cmd == 'redraft':
            print('That one is an offer to handle something the same way every time, not a prepared step, so there is nothing to redo. Nothing was stored.')
            return 2
        return None
    if cmd == 'redraft':
        return redraft(con, r, p, list(words), life_home)
    return approve(con, r, p, ' '.join(words).strip(), life_home, close)


def sync(con, r, p: dict, rec: dict, now: float | None = None) -> dict:
    """Bring a record in line with what happened to its item since (the owner was told; the chat Zeus closed it)."""
    if rec is None or rec['kind'] not in STEP_KINDS:
        return rec
    now = time.time() if now is None else now
    cl, state = p.get('closed') or {}, r['state']
    if rec['state'] == 'prepared' and state == 'surfaced' and not cl:
        A.move(con, rec['id'], 'asked', 'put to them', now=float((p.get('told') or {}).get('at') or now))
    elif cl and rec['kind'] == 'step' and rec['state'] in ('sending', 'unknown'):
        if cl.get('state') == 'done':
            A.move(con, rec['id'], 'sent', 'the chat Zeus reported it done', receipt={'reported': str(cl.get('why') or '')[:300], 'by': cl.get('by'), 'at': cl.get('at')}, now=now)
        else:
            A.move(con, rec['id'], 'did_not_run', 'closed as not done: ' + str(cl.get('why') or '')[:200], now=now)
    elif cl and rec['state'] in ('prepared', 'asked', 'approved'):
        A.move(con, rec['id'], 'declined' if cl.get('state') == 'declined' else 'expired', 'its item was closed: ' + str(cl.get('why') or cl.get('state') or '')[:200], now=now)
    elif rec['state'] in A.WAITING and state in ('pending', 'executing') and __import__('glue').sent_back_since(p, rec):
        A.move(con, rec['id'], 'replaced', 'it changed after it was prepared, and the item went back for fresh work', now=now)   # glue.py
    return A.get(con, rec['id'])


def _redo(key: str, emailed: bool) -> str:
    what = 'the complete new reply' if emailed else 'the new step, exactly'
    return (f'Write the new version now. Then store it: {TOOL} redraft {key} "<{what}>" (it prints back what was stored). '
            'Show them exactly those words and ask once. Their yes after that carries out the new version.')


def not_an_answer(text: str, why: str, key: str, emailed: bool) -> str:
    """What the chat Zeus reads when the owner's message says neither yes nor no (switch file chat-rules-v2). It says
    plainly what the message was, and that the reply answers only what they wrote, so "ok thanks" never brings the
    whole ask back."""
    head = f'Their message ("{text[:200]}") is not a yes or a no, so it is not their yes. Nothing was sent or done. '
    if why == 'it asks something':
        return head + 'Answer their question, and only that. Do not ask them for their yes again in this reply.'
    if why == 'it says yes and no':
        return head + 'If you cannot tell what the owner wants, ask them once, in a few words, whether it is a yes.'
    if str(why).startswith('it has words in it that are not part of the prepared step'):
        # Code could not place some of the owner's words; that is code's doubt, never their fault, so the reply never
        # asks them to type "yes" again or says their words did not register.
        unplaced = str(why).split(': ', 1)[-1]
        return (f'Their message ("{text[:200]}") has words code could not place ({unplaced}), so nothing was sent or done yet. '
                'Never ask them to say yes again, and never tell them their words did not count or register. If their words change '
                'the step itself, redo it: ' + _redo(key, emailed) + ' If you cannot tell what they change, ask one short question '
                'about those words only. Never read the step or its settings back to them.')
    return head + 'Reply only to what the owner actually said, briefly. Do not bring this one up again in this reply.'


def approve(con, r, p: dict, note: str, life_home, close) -> int | None:
    now = time.time()
    key = r['key']
    A.sweep(con, now)
    rec = A.latest(con, key)
    rec = sync(con, r, p, rec, now) if rec and rec['kind'] in STEP_KINDS else None
    decision = (p.get('packet') or {}).get('decision') or {}
    if rec and rec['kind'] == 'peer_notice' and rec['state'] in ('approved','sending','unknown'):
        print('This exact notice already has approval; delivery remains tracked per recipient.')
        return 3
    if rec and rec['state'] == 'sent':
        went = A.last(rec, 'sent')
        print(f"This already went at {A.clock(went.get('at'))}" + (f", to {_who(rec)}" if rec['kind'] == 'email_reply' else '')
              + '. It was not done a second time. Do NOT do the step again. If the owner is asking for something new, treat it as a new request.')
        return 3
    if rec and rec['state'] == 'sending':
        print('This one is being carried out right now by an earlier yes. Nothing was started a second time. Do NOT do the step again.')
        return 3
    if rec and rec['state'] == 'unknown':
        if rec['kind'] != 'email_reply':
            print(f"Their yes was already recorded at {A.clock(A.last(rec, 'approved').get('at'))}, and the step was handed over then. It may have been done. "
                  f'Do NOT do it again. Check where it went (the sent folder, the task, the calendar). If it is done: {TOOL} done {key} "<what was done>". '
                  'If it did not go through, tell them exactly that.')
            return 3
        outcome = settle(con, rec, now)
        if outcome == 'sent':
            _after_settle(con, life_home, key, A.get(con, rec['id']), outcome, close=close, tell=False)
            print(f'It did go. I read the thread just now and the reply to {_who(rec)} is there, word for word. Nothing was sent again. '
                  'Tell them in one short line that it went.')
            return 0
        if outcome != 'did_not_run':
            print(_may_have_gone(A.get(con, rec['id'])) + ' Do NOT send it again by any means.')
            return 8
        rec = None                                    # read back twice, not there: their fresh yes below may send it, once
    if p.get('closed') or r['state'] not in ('surfaced', 'ready') or not (decision.get('ask') or decision.get('ready')) or decision.get('by') == 'owner':
        # glue.py (switch fresh-on): if it changed and is being redone, or that ask ended, the owner hears exactly that.
        # Otherwise None, as before: lifecli's own answers for "not waiting on them" and "nothing was prepared".
        return __import__('glue').yes_held(con, life_home, r, p, rec, words=note)
    if rec and rec['state'] == 'approved':            # a run that stopped between their yes and the attempt: nothing went
        A.move(con, rec['id'], 'did_not_run', 'stopped before anything was attempted')
        rec = A.get(con, rec['id'])
    if rec and rec['state'] in ('declined', 'replaced', 'expired'):
        why = {'declined': 'the owner said no to it', 'replaced': 'it was changed after it was prepared',
               'expired': 'the time it was for has passed'}[rec['state']]
        print(f'That version is closed ({why}), so nothing was sent or done. If the owner wants it now, it needs a fresh version. ' + _redo(key, bool(p.get('thread'))))
        return 9
    if rec is None or rec['state'] == 'did_not_run':
        rec = freeze(con, key, p, p.get('packet') or {}, life_home, why='frozen when the owner answered')
        rec = sync(con, r, p, rec, now)
    if not note:
        print(f'Quote their message, word for word: {TOOL} approve {key} "<their message>". Nothing was done.')
        return 2
    asked = A.last(rec, 'asked').get('at') or rec['created']
    try:
        msg, later = his_reply(note, float(asked), now, life_home)
    except Exception as e:
        print(f'Their messages could not be read just now ({type(e).__name__}), so nothing was recorded and nothing was sent. Try once more in a minute.')
        return 1
    if msg is None:
        print('There is no message from them in the last 15 minutes, after this was put to them. So this is not their yes, and nothing was sent or done. '
              'If the owner did answer, ask them once more.')
        return 3
    if _mail().owner_spoke(life_home) is False:
        print(f'No message from {settings.OWNER_FIRST} reached you in the last 15 minutes, so this is not their yes and nothing was sent. '
              'If the owner did just say it, ask them once more in their own chat.')
        return 4
    for m in later:
        took_back = H.read(m['text'])
        if took_back['answer'] in ('no', 'changed') or str(took_back['why']).endswith('not decided yet'):
            A.note(con, rec['id'], 'their reply was read', reading='taken back', message_id=m['id'], his_words=m['text'][:300])
            print(f'After that message the owner wrote: "{m["text"][:200]}". So their yes does not stand, and nothing was sent or done. Go by their newest message.')
            return 9
    asks = open_asks(con)
    others = [a for a in asks if a['key'] != key]
    own = _vocab(p.get('title'), decision.get('ask'), rec['details'].get('who'))
    reading = H.bind(H.read(msg['text']), own, [a['vocab'] for a in others])
    reading = answers_the_last_ask(con, rec, msg, reading, others, life_home)       # "sounds good" to the ask just made
    if (Path(life_home) / 'tapback-yes-on').exists():   # tapback.py: a 👍 on the message that asked is a yes only for small free steps; never money, someone else, calendar, deletes, offers
        reading = __import__('tapback').read(con, life_home, rec, p, msg, others, reading)
    # A yes with words of feeling next to it is read exactly like a plain yes (hisreply.bind ignores them),
    # so which ask it answers is decided above from the times on record, as for "yes" alone.
    which = _which(life_home, now)
    named = False
    if reading['answer'] == 'other' and which and key in which.get('items', []) and msg['id'] != which.get('message_id') and msg['at'] >= float(which['asked_at']) - 1:
        pick = H.selector(msg['text'], own, [a['vocab'] for a in others])
        spot = which['items'].index(key) + 1
        if pick in ('all', 'this') or (isinstance(pick, int) and (pick == spot or (pick == -1 and spot == len(which['items'])))):
            reading = dict(reading, answer=which.get('answer', 'yes'), points=True, all=pick == 'all', why='the owner named it after being asked which one')
            named = True
            if pick != 'all':
                (Path(life_home) / WHICH_FILE).unlink(missing_ok=True)
    A.note(con, rec['id'], 'their reply was read', reading=reading['answer'], why=reading['why'], message_id=msg['id'], his_words=msg['text'][:300])
    if reading.get('tapback') and reading.get('say'):   # a tapback that is not a yes here: what the chat Zeus says (ask once in words, or nothing more)
        print(reading['say'])
        return 9
    if reading['answer'] == 'other':
        if (Path(life_home) / 'chat-rules-v2').exists():         # a non-answer is not a cue to ask again
            print(not_an_answer(msg['text'], reading['why'], key, rec['kind'] == 'email_reply'))
            return 9
        print(f'Their message ("{msg["text"][:200]}") is not a plain yes or no: {reading["why"]}. Nothing was sent or done, and this is still waiting on them. '
              'If the owner changed something, redo it: ' + _redo(key, rec['kind'] == 'email_reply') + ' If it is a question, answer it. "ok" or a thank-you is not a yes.')
        return 9
    earlier = used_before(con, msg['id'], rec['id'], now)
    if earlier and not reading.get('all'):
        print(f'That same message of theirs already decided another one ("{earlier["details"].get("title")}"). One message decides one thing. '
              'Nothing was sent or done here. If the owner means this one too, the owner has to say so.')
        return 9
    if reading.get('answers') and reading['answers'] != key and not named:      # it answers the ask in Zeus's last message
        other = next((a for a in asks if a['key'] == reading['answers']), {'title': 'another thing', 'key': reading['answers']})
        print(f'Their "{msg["text"][:80]}" answers the last thing you asked them, which was about {other["title"]}, not this one. '
              f'Nothing was sent or done here. Run: {TOOL} approve {other["key"]} "<their message, word for word>" and do what it says. '
              'Do not ask them again.')
        return 9
    if reading.get('unbound') and not others and not named:                     # a bare yes or no right after news
        print(f'Their "{msg["text"][:80]}" came right after a message of yours that was not about this one '
              f'({p.get("title") or "the thing waiting on them"}), so it may not answer it. Nothing was sent or done. Ask them once, in a few words, whether that '
              f'is their {reading["answer"]} to it. Do not read it back in full.')
        return 9
    if reading['answer'] in ('yes', 'no') and others and not reading.get('points') and not reading.get('all') and not named:
        names = [a for a in asks if a['key'] == key] + others
        listing = '; '.join(f"{i}) {a['title']}" + (f" ({a['ask'].rstrip('.?')})" if a['ask'] else '') for i, a in enumerate(names, 1))
        if which and which.get('message_id') == msg['id']:
            print('You already asked them which one the owner means. Nothing was sent or done. Do not ask again: when the owner names one, run approve for it with that new message.')
            return 9
        (Path(life_home) / WHICH_FILE).write_text(json.dumps({'asked_at': now, 'message_id': msg['id'], 'answer': reading['answer'], 'items': [a['key'] for a in names]}))
        print(f'{len(names)} things are waiting on their word, and their "{msg["text"][:80]}" does not say which: {listing}. Nothing was sent or done. '
              f'Ask them once, in one short line, which one the owner means. When the owner names it, run: {TOOL} approve <that one> "<their new message>".')
        return 9
    said = {'his_words': msg['text'][:600], 'message_id': msg['id'], 'said_at': msg['at']}
    if rec and rec['kind'] == 'peer_notice':
        said['owner_session'] = msg.get('session')
    if reading['answer'] == 'no':
        held = __import__('glue').yes_held(con, life_home, r, p, rec, said, answer='no')   # glue.py (switch fresh-on): was their no to a version that changed? None = an ordinary no
        if held is not None:
            return held
        A.move(con, rec['id'], 'declined', 'the owner said no', **said)
        close(con, r, 'declined', 'zeus chat', 'the owner said no: ' + msg['text'][:200])
        print('Their no is recorded. Nothing was sent or done, and this one is closed. If their message also asks for something else, treat that as a new request.')
        return 0
    if reading['answer'] == 'changed':
        A.move(con, rec['id'], 'replaced', 'the owner changed it: ' + reading['why'], **said)
        print(f'The owner changed it ("{msg["text"][:200]}"), so the prepared version is off. Nothing was sent or done. ' + _redo(key, rec['kind'] == 'email_reply'))
        return 9
    held = __import__('glue').yes_held(con, life_home, r, p, rec, said)   # glue.py (switch fresh-on): read again before their yes counts; None = go
    if held is not None:
        return held
    return carry_out(con, r, p, rec, said, life_home, close)


def _replace_for_facts(con, key: str, rec: dict, why: str, emailed: bool) -> int:
    A.move(con, rec['id'], 'replaced', why)
    print(f'{why[0].upper() + why[1:]}, so the prepared version was NOT sent. Look at how things stand now. ' + _redo(key, emailed))
    return 9


def carry_out(con, r, p: dict, rec: dict, said: dict, life_home, close) -> int:
    """Their plain yes is in hand. The stored step is carried out once, or not at all."""
    key, decision = r['key'], (p.get('packet') or {}).get('decision') or {}
    emailed = rec['kind'] == 'email_reply'
    if rec.get('facts') and rec['facts'] != facts_version(p):
        return _replace_for_facts(con, key, rec, 'something new came in on this after it was prepared', emailed)
    mirror = {'at': time.time(), 'by': 'zeus chat', 'his_words': said['his_words'], 'ask': decision.get('ask'), 'step': decision.get('ready')}
    if rec['kind'] == 'peer_notice':
        spec = rec['details']['peer_notice']
        full = peer_state(con,key)
        note = (p.get('told') or {}).get('note')
        presented = con.execute("SELECT o.text FROM outbox o JOIN note_items n ON n.note=o.key "
                                "WHERE o.key=? AND o.mode='live' AND o.state='sent' AND n.opp=?",
                                (note,key)).fetchone()
        import chats as C
        store = _chat_store(life_home)
        prior = [m for m in C.messages(said['said_at'] - RECENT, said['said_at'] + 0.001, path=store)
                 if m['who'] == 'zeus' and (m['at'], m['id']) < (said['said_at'], said['message_id'])
                 and m['session'] == said.get('owner_session')] if store else []
        # The notifier persisted these exact bytes with the item before it sent
        # the offer. Company scope can add one owner-only caution to the front.
        terms = peer_offer(spec)
        eligible = presented and presented[0] in (terms, 'Company unverified. ' + terms)
        paired = bool(eligible and prior and prior[-1]['text'] == presented[0])
        if not (full and full[4] and paired):
            print('The exact notice and recipients have not been confirmed in their thread. Nothing sent.')
            return 9
        if not A.move(con,rec['id'],'approved','their yes to exact body and direct recipients',**said):
            return 3
        print('Their approval is saved. The existing action tick will check each receiving peer.')
        return 0
    if not emailed:
        if not A.move(con, rec['id'], 'approved', 'their plain yes', **said):
            print('Another run got to this one first. Nothing was started a second time.')
            return 3
        p['approved'] = mirror
        _save(con, key, p)
        A.move(con, rec['id'], 'sending', 'handed to the chat Zeus to do once', patience=STEP_PATIENCE)
        print('Their yes is recorded. Do exactly this now, and nothing more:\n\n' + str(decision.get('ready') or decision.get('ask')) +
              f'\n\nWhen it is done: {TOOL} done {key} "<what you did>". If any part cannot be done exactly as written, '
              'stop and tell them what is in the way. Do not improvise and do not widen it.'
              + (NOT_YET if (Path(life_home) / 'chat-rules-v2').exists() else ''))
        return 0
    M = _mail()
    draft, who = _draft(rec), _who(rec)
    stored = (p.get('packet') or {}).get('send')
    if not isinstance(stored, dict) or M.fingerprint(stored) != M.fingerprint(draft) or A.fingerprint('email_reply', key, rec['details']) != rec['fingerprint']:
        print('The stored reply is not the one that was prepared, so nothing was sent. Tell them it needs a fresh draft.')
        return 5
    found = M.problems(draft, M.thread_people(draft))
    if found:
        print('The prepared reply cannot go out as it stands: ' + '; '.join(found) + '. Nothing was sent. Tell them exactly that.')
        return 5
    there = M.thread_state(draft)
    if there is None:
        print('The email thread could not be read just now, and a reply is never sent blind, so nothing was sent. '
              'Try once more in a minute. If it still fails, tell them.')
        return 6
    if there == 'other':
        print('A reply already went out from their mailbox in that thread, after the email this one answers. Nothing was sent. '
              'Tell them that, and ask whether the owner still wants this reply to go.')
        return 3
    if there == 'moved':
        return _replace_for_facts(con, key, rec, 'a newer message arrived in that thread after this reply was prepared', True)
    verdict, twin = A.fence(con, rec)
    if verdict == 'sent':
        A.move(con, rec['id'], 'expired', 'the same reply already went under another item')
        print(f"This exact reply already went at {A.clock(A.last(twin, 'sent').get('at'))}, to {who}. It was not sent again. Tell them it already went.")
        return 3
    if verdict == 'held':
        print('This exact reply is already waiting, being sent, or may have gone under another item '
              f'("{twin["details"].get("title")}"). Nothing was sent. Settle that one first.')
        return 3
    # Their yes goes on record BEFORE the send, and "sending" before the attempt: no crash and no second yes can send twice.
    if not A.move(con, rec['id'], 'approved', 'their plain yes', **said):
        print('Another run got to this one first. Nothing was sent a second time.')
        return 3
    p['approved'] = mirror | {'send': {'fingerprint': rec['fingerprint'], 'state': 'sending'}}
    _save(con, key, p)
    outcome, info = send_now(con, rec, life_home, already_there=there == 'ours')
    rec = A.get(con, rec['id'])
    if outcome == 'sent':
        _close_sent(con, key, rec, close)
        print(f"SENT, word for word as prepared, to {who} from {rec['details'].get('account') or 'their mailbox'}"
              + (' (it was already in the thread)' if info.get('found_in_thread') else '') + '. Nothing else went out. '
              'Tell them in one short line that it went and to whom. Do not send, retype or add anything.')
        return 0
    row = con.execute('SELECT payload FROM opps WHERE key=?', (key,)).fetchone()
    p = json.loads(row[0])
    if outcome == 'did_not_run':
        p.pop('approved', None)
        p['send_failed'] = {'at': time.time(), 'why': str(info.get('why'))[:200]}
        _save(con, key, p)
        print(f"The mail service refused it ({info.get('why')}). Nothing was sent. Tell them it did not go. If the owner says yes again it can be tried once more.")
        return 7
    p.setdefault('approved', mirror)['send'] = {'fingerprint': rec['fingerprint'], 'state': 'unknown', 'why': str(info.get('why'))[:200], 'at': time.time()}
    _save(con, key, p)
    print(_may_have_gone(rec) + ' Do NOT send it again by any means.')
    return 8


def send_now(con, rec: dict, life_home, already_there: bool = False):
    """The attempt itself, for a record that is 'approved'. "sending" is written first. -> ('sent' | 'did_not_run' |
    'unknown', what is known). The one place in this build that calls the mail service to send."""
    M = _mail()
    draft = _draft(rec)
    if not A.move(con, rec['id'], 'sending', 'the reply is being handed to the mail service', pid=os.getpid(), patience=SEND_PATIENCE):
        return 'unknown', {'why': 'another run is already sending it'}
    if already_there:
        receipt = {'at': time.time(), 'fingerprint': rec['fingerprint'], 'found_in_thread': True}
        A.move(con, rec['id'], 'sent', 'it was already in the thread', receipt=receipt)
        return 'sent', receipt
    try:
        receipt = M.send(draft, life_home)
    except M.SendFailed as e:
        A.move(con, rec['id'], 'did_not_run', 'the mail service said no: ' + str(e)[:200])
        return 'did_not_run', {'why': str(e)[:200]}
    except Exception as e:                            # no clear answer (SendUncertain) or anything unforeseen: it may have gone
        why = str(e)[:200] if isinstance(e, M.SendUncertain) else f'the send stopped part way ({type(e).__name__})'
        seen = M.thread_state(draft)
        A.note(con, rec['id'], 'read the thread', found=seen or 'could not be read')
        if seen == 'ours':
            receipt = {'at': time.time(), 'fingerprint': rec['fingerprint'], 'found_in_thread': True}
            A.move(con, rec['id'], 'sent', 'no clear answer came back, and the reply was found in the thread', receipt=receipt)
            return 'sent', receipt
        A.move(con, rec['id'], 'unknown', why)
        return 'unknown', {'why': why}
    A.move(con, rec['id'], 'sent', 'the mail service accepted it', receipt=receipt)
    return 'sent', receipt


def _close_sent(con, key: str, rec: dict, close):
    """The item of a reply that went: their yes and the receipt stay on it, and it closes as done."""
    row = con.execute('SELECT key,state,payload FROM opps WHERE key=?', (key,)).fetchone()
    p = json.loads(row[2])
    if p.get('closed'):
        return
    receipt = rec.get('receipt') or {}
    p.setdefault('approved', {'at': A.last(rec, 'approved').get('at'), 'by': 'zeus chat', 'his_words': A.last(rec, 'approved').get('his_words')})
    p['approved']['send'] = receipt | {'state': 'dry run' if receipt.get('dry') else 'sent'}
    _save(con, key, p)
    row = con.execute('SELECT key,state,payload FROM opps WHERE key=?', (key,)).fetchone()
    close(con, row, 'done', 'zeus chat', 'sent by code after their yes, to ' + _who(rec), {'sent': p['approved']['send']})


def _may_have_gone(rec: dict) -> str:
    """The plain words for "unknown": it may have gone, how it was checked, what was found."""
    reads = A.notes(rec, 'read the thread')
    who = _who(rec)
    if not reads:
        return (f'Your reply to {who} may have gone: the mail service gave no clear answer. I have not been able to read the thread yet. '
                'I will read it before anything else, and I am not sending it twice. Tell them exactly that.')
    newest = reads[-1]
    found = {'nothing': 'it is not there yet', 'moved': 'it is not there yet, and a newer message from the other side is',
             'other': 'there is a reply from your mailbox there, but it is not word for word the one I prepared',
             'could not be read': 'the thread could not be read'}.get(newest.get('found'), 'it is not there yet')
    return (f'Your reply to {who} may have gone: the mail service gave no clear answer. I checked by reading the thread at {A.clock(newest.get("at"))}, '
            f'and {found}. I will read it again in a few minutes and tell you what I find. I am not sending it twice. Tell them exactly that.')


def redraft(con, r, p: dict, args: list, life_home) -> int:
    """`life redraft <item> [--to a@b,c@d] [--cc ...] (--file <path> | "<the complete new text>")`: store the new
    version after the owner changed something, or after the thing itself changed. The old ask is closed as replaced; their
    next yes carries out exactly what is stored here."""
    key, now = r['key'], time.time()
    if (p.get('packet') or {}).get('peer_notice'):
        print('Changing a recipient or body requires a new exact notice shown to the owner. This notice was not changed.')
        return 3
    opts, words, i = {}, [], 0
    while i < len(args):
        if args[i] in ('--to', '--cc', '--file') and i + 1 < len(args):
            opts[args[i][2:]] = args[i + 1]
            i += 2
        else:
            words.append(args[i])
            i += 1
    text = ' '.join(words).strip()
    if opts.get('file'):
        try:
            text = Path(opts['file']).read_text(encoding='utf-8').strip()
        except OSError:
            print('That file could not be read. Nothing was stored.')
            return 2
    packet = p.get('packet') or {}
    decision = packet.get('decision') or {}
    if p.get('closed') or r['state'] not in ('surfaced', 'ready') or not (decision.get('ask') or decision.get('ready')):
        print('This one is not waiting on them, so there is nothing to redo here. If the owner wants something new, treat it as a new request.')
        return 3
    rec = A.latest(con, key)
    rec = sync(con, r, p, rec, now) if rec and rec['kind'] in STEP_KINDS else None
    if rec and rec['state'] in ('approved', 'sending', 'unknown', 'sent'):
        print('This one is already under way or done, so it cannot be changed now. Nothing was stored. Tell them where it stands.')
        return 3
    if len(text) < 10:
        print(f'Give the complete new text: {TOOL} redraft {key} "<all of it>" or --file <path>. Nothing was stored.')
        return 2
    M = _mail()
    old = packet.get('send') if isinstance(packet.get('send'), dict) else {}
    split = lambda v: [x.strip() for x in str(v).split(',') if x.strip()]
    draft = M.from_job({'to': split(opts['to']) if opts.get('to') else old.get('to'), 'cc': split(opts['cc']) if 'cc' in opts else old.get('cc'), 'body': text}, p) \
        if (old or opts.get('to')) else None
    if draft:
        blank = M.BLANK.search(draft['body'])
        if blank or not M.MIN_BODY <= len(draft['body']) <= M.MAX_BODY:
            print('That text cannot go out as it stands' + (f': it still has a blank to fill in ("{blank.group(0)}")' if blank else ': it is almost empty or far too long')
                  + '. Nothing was stored.')
            return 5
        packet['send'] = draft | {'fingerprint': M.fingerprint(draft)}
        decision['ready'] = ('Reply to ' + ', '.join(draft['to']) + ': ' + draft['body'])[:2400]
    else:
        packet.pop('send', None)
        decision['ready'] = text[:2400]
    packet['decision'] = decision
    p['packet'] = packet
    p['redrafted'] = {'at': now, 'by': 'zeus chat'}
    p['told'] = {'at': now, 'note': 'chat:' + datetime.now().strftime('%Y-%m-%dT%H:%M'), 'by': 'zeus chat'}
    _save(con, key, p, state='surfaced')
    new = freeze(con, key, p, packet, life_home, why='redone in chat')
    A.move(con, new['id'], 'asked', 'the new version is being shown to them now', now=now)
    (Path(life_home) / WHICH_FILE).unlink(missing_ok=True)
    print('Stored, word for word:\n\n' + (draft['body'] if draft else text) + ('\n\nIt will go to: ' + ', '.join(draft['to'] + draft['cc']) if draft else '')
          + f'\n\nNothing has been sent. Show them exactly these words and ask once. When the owner answers: {TOOL} approve {key} "<their message>".')
    return 0


def show_more(con, r, p: dict, view: dict, life_home) -> dict:
    """Hook in `life show`: the exact stored reply, where the step stands by the record, and what to run when the owner
    answers. Switch off: the view comes back untouched."""
    try:
        if not A.on(life_home):
            return view
        A.ready(con)
        packet = p.get('packet') or {}
        send = packet.get('send') if isinstance(packet.get('send'), dict) else None
        rec = A.latest(con, r['key'])
        if send:
            view['the_exact_reply'] = {'from': _mail().S.MAILBOX_ADDRESS.get(send.get('mailbox')), 'to': send.get('to'), 'cc': send.get('cc'), 'body': send.get('body')}
        if rec:
            stands = {'prepared': 'prepared, the owner has not been asked yet', 'asked': 'put to them, waiting on their word', 'approved': 'their yes is on record, nothing has gone yet',
                      'sending': 'being carried out right now', 'sent': 'it went, at ' + A.clock(A.last(rec, 'sent').get('at')),
                      'did_not_run': 'it did not go: ' + str(A.last(rec, 'did_not_run').get('why') or ''), 'unknown': 'it may have gone: ' + str(A.last(rec, 'unknown').get('why') or ''),
                      'declined': 'the owner said no', 'replaced': 'changed after it was prepared; it needs a fresh version', 'expired': 'its time passed before the owner answered'}
            view['where_it_stands'] = stands.get(rec['state'], rec['state'])
        if 'when_he_says_yes' in view and not p.get('approved') and not (isinstance(p.get('offer'), dict)):
            view['when_he_says_yes'] = (f'Run: {TOOL} approve {r["key"]} "<their message, word for word>". It reads their real message itself. '
                                        + ('On a plain yes it sends the reply above itself, word for word, and tells you it went: never send or retype that email yourself. '
                                          if send else 'On a plain yes it prints the exact step for you to do once. ')
                                        + 'If the owner changed anything, or said no, it tells you what to do instead. "ok" or a thank-you is not a yes.')
    except Exception:
        pass
    return view


# ---- settling an unknown email, and the tick step --------------------------------------------------------------------

def settle(con, rec: dict, now: float | None = None) -> str:
    """An unknown email reply is settled ONLY by reading the thread it would be in, never by sending again.
    -> 'sent' (found, word for word) | 'did_not_run' (read at least twice, the last time ten minutes or more after
    the attempt: not there) | 'other' (a different reply from their mailbox is there) | 'unknown' (cannot tell yet)."""
    now = time.time() if now is None else now
    seen = _mail().thread_state(_draft(rec))
    A.note(con, rec['id'], 'read the thread', found=seen or 'could not be read', now=now)
    rec = A.get(con, rec['id'])
    if seen == 'ours':
        A.move(con, rec['id'], 'sent', 'found in the thread when it was read back', now=now,
               receipt={'at': now, 'fingerprint': rec['fingerprint'], 'found_in_thread': True})
        return 'sent'
    if seen is None:
        return 'unknown'
    tried = float(A.last(rec, 'sending').get('at') or rec['updated'])
    absent = [n for n in A.notes(rec, 'read the thread') if n.get('found') in ('nothing', 'moved')]
    if seen in ('nothing', 'moved') and len(absent) >= 2 and now - tried >= SETTLE_WAIT:
        A.move(con, rec['id'], 'did_not_run', f'the thread was read {len(absent)} times, the last at {A.clock(now)}, and the reply is not there', now=now)
        return 'did_not_run'
    return 'other' if seen == 'other' else 'unknown'


def _tell(con, key: str, p: dict, headline: str, details: str, now_dt, *, child=None, peer_action=None):
    """One short thing the owner must hear about a send the owner approved: its own small item, ready now, so the ordinary note
    carries it (with the ordinary quiet hours). It is a child of the item, so it is not checked against the thread."""
    child = child or f"{key.split('#')[0]}#fu-send:{now_dt.strftime('%m%d%H%M')}"
    if con.execute('SELECT 1 FROM opps WHERE key=?', (child,)).fetchone():
        return
    step = {'do': headline, 'action_class': 'notify_owner_now', 'autonomy': 'auto'}
    payload = {'title': 'What happened to a reply you approved', 'why': str(p.get('title') or ''), 'evidence': '', 'urgency': 'now', 'dedupe_key': child,
               'parent': key, 'followup': {'check': headline, 'when': now_dt.isoformat(), 'reminder': True}, '_source': p.get('_source'), 'owner_involved': 'yes',
               'confidence': 1.0, 'steps': [step], 'ready_at': now_dt.timestamp(),
               'decision': {'autonomy': 'auto', 'surface_at': now_dt.isoformat(), 'channel': 'text_now', 'steps': [step], 'granted': False,
                            'reason': 'the outcome of a send the owner approved'},
               'packet': {'status': 'done', 'headline': headline, 'details': details, 'decision': None, 'followups': [], 'learned': []}}
    if peer_action:
        payload.update(peer_action=peer_action, urgency='later')
        payload['packet']['verbatim'] = True
    con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)', (child, 'ready', 'auto', now_dt.isoformat(), payload['urgency'], 'notify_owner_now',
                                                               json.dumps(payload, default=str), '[]', time.time(), time.time()))
    con.commit()


def _after_settle(con, life_home, key: str, rec: dict, outcome: str, *, close=None, tell: bool = True, now_dt=None):
    """What a settled (or still open) unknown means for its item, and what the owner hears."""
    now_dt = now_dt or datetime.now().astimezone()
    row = con.execute('SELECT key,state,payload,surface_at FROM opps WHERE key=?', (key,)).fetchone()
    if row is None:
        return
    p = json.loads(row[2])
    who, by_team = _who(rec), bool(rec['details'].get('by_team'))
    packet = p.get('packet') or {}
    if outcome == 'sent':
        if by_team:
            if not p.get('closed'):
                packet.update(status='done', decision=None, headline=f'Your reply to {who} did go out. I read the thread and it is there, word for word.')
                p['packet'] = packet
                p['team_sent'] = rec.get('receipt')
                _save(con, key, p)
            return
        _close_sent(con, key, rec, close or _closer())
        if tell:
            _tell(con, key, p, f'Your reply to {who} did go out. I read the thread at {A.clock(time.time())} and it is there, word for word.',
                  'Nothing was sent twice.', now_dt)
    elif outcome == 'did_not_run':
        if p.get('closed'):
            return
        held = packet.pop('decision_held', None)
        if held:
            packet['decision'] = held
        packet['status'] = 'needs_decision'
        packet['headline'] = (f'Your reply to {who} did not go out. The mail service gave no clear answer, so I read the thread twice, ten minutes apart, '
                              'and it is not there. It is still ready to send.')
        p['packet'] = packet
        if p.get('approved'):
            p.setdefault('approved_before', []).append(p.pop('approved'))
        p.pop('told', None)
        p['ready_at'] = time.time()
        _save(con, key, p, state='ready', surface_at=now_dt.isoformat())
        fresh = freeze(con, key, p, packet, life_home, why='asked again: the first try did not go')
        if by_team:
            A.note(con, fresh['id'], 'team rule not used', why='the first try did not go')
    elif outcome == 'other' and not A.notes(rec, 'told'):
        A.note(con, rec['id'], 'told')
        if tell:
            _tell(con, key, p, f'There is a reply from your mailbox in the thread with {who}, but it is not word for word the one I prepared, '
                               'so I cannot tell whether mine went.', 'I have not sent anything else. Take a look at that thread.', now_dt)
        if not p.get('closed'):
            if p.get('approved'):                     # the record keeps their yes; the item no longer claims a step under way
                p.setdefault('approved_before', []).append(p.pop('approved'))
            p['closed'] = {'state': 'moot', 'at': time.time(), 'by': 'one-send readback', 'why': 'a reply from their mailbox is already in that thread'}
            _save(con, key, p, state='moot')


def _deadline(p: dict):
    for k in ('deadline', 'event_start'):
        try:
            t = datetime.fromisoformat(str(p.get(k)).replace('Z', '+00:00'))
            return (t if t.tzinfo else t.astimezone()).timestamp()
        except (TypeError, ValueError):
            continue
    return None


def _team_send(con, life_home, key: str, p: dict, rec: dict, now_dt) -> str:
    """A reply whose every recipient is on the owner's configured team needs no yes. Same last looks
    as after a yes; anything not clean and the owner is simply asked, as for any other reply. -> what happened."""
    M = _mail()
    draft = _draft(rec)
    stored = (p.get('packet') or {}).get('send') or {}
    held_back = None
    if not team_only(draft, life_home) or M.fingerprint(stored) != M.fingerprint(draft):
        held_back = 'not every recipient is on their team, or the stored reply changed'
    elif M.problems(draft, M.thread_people(draft)):
        held_back = 'the reply cannot go out as it stands'
    elif M.thread_state(draft) != 'nothing':
        held_back = 'the thread is not as it was, or could not be read'
    elif A.fence(con, rec)[0] != 'clear':
        held_back = 'the same reply is already on record elsewhere'
    elif rec.get('facts') != facts_version(p):
        held_back = 'something new came in after it was prepared'
    if held_back:
        A.note(con, rec['id'], 'team rule not used', why=held_back)
        return 'asked'
    if not A.move(con, rec['id'], 'approved', 'every recipient is on their own team, so no yes is needed', by='team rule'):
        return 'asked'
    rec['details']['by_team'] = True
    con.execute('UPDATE actions SET details=? WHERE id=?', (json.dumps(rec['details'], ensure_ascii=False, default=str), rec['id']))
    con.commit()
    outcome, info = send_now(con, rec, life_home)
    rec = A.get(con, rec['id'])
    packet = p.get('packet') or {}
    who = _who(rec)
    if outcome == 'sent':
        packet.update(status='done', decision=None, headline=f'I sent your reply to {who}, who is on your team. ' + str(packet.get('headline') or ''),
                      details=(str(packet.get('details') or '') + ' What went: "' + str(draft['body'])[:300] + '"').strip())
        p['team_sent'] = rec.get('receipt')
    elif outcome == 'did_not_run':
        fresh = freeze(con, key, p, packet, life_home, why='asked as usual: the team send did not go')
        A.note(con, fresh['id'], 'team rule not used', why='the first try did not go')
        return outcome
    else:
        packet['decision_held'] = packet.get('decision')
        packet.update(status='done', decision=None,
                      headline=f'I tried to send your reply to {who}, who is on your team, and got no clear answer back. It may have gone. '
                               'I am reading the thread before anything else and will not send it twice.')
    p['packet'] = packet
    _save(con, key, p)
    return outcome


def tend(con, life_home, now_dt) -> dict:
    """The tick's step (hook in tick.run_phases, between the work and the note). Sends nothing on a guess: it sweeps,
    reads back, expires, and, only with the second switch, carries out team-only replies. -> counts for the report."""
    if not A.on(life_home):
        return {}
    A.ready(con)
    now = now_dt.timestamp()
    out = {}

    def count(what):
        out[what] = out.get(what, 0) + 1

    for _ in A.sweep(con, now):
        count('became_unknown')
    team_on = (Path(life_home) / TEAM_SWITCH).exists()
    for rec in A.in_states(con, ('prepared', 'asked', 'approved', 'sending', 'unknown')):
        try:
            if rec['kind'] == 'peer_notice' and rec['state'] in ('approved','sending','unknown'):
                peer_tick(con,life_home,rec)
                continue
            if rec['kind'] not in STEP_KINDS:
                continue
            row = con.execute('SELECT key,state,payload,surface_at FROM opps WHERE key=?', (rec['item'],)).fetchone()
            if row is None:
                if rec['state'] in ('prepared', 'asked', 'approved'):
                    A.move(con, rec['id'], 'expired', 'its item is gone', now=now)
                continue
            p = json.loads(row[2])
            rec = sync(con, {'key': row[0], 'state': row[1]}, p, rec, now)
            due = _deadline(p)
            if rec['state'] in A.WAITING and due is not None and due < now:
                A.move(con, rec['id'], 'expired', 'the time it was for passed before the owner answered', now=now)
                count('expired')
            elif rec['state'] == 'unknown' and rec['kind'] == 'email_reply':
                outcome = settle(con, rec, now)
                _after_settle(con, life_home, rec['item'], A.get(con, rec['id']), outcome, now_dt=now_dt)
                __import__('recover').settled(con, life_home, rec, outcome, now_dt, out)   # recover.py (switch recover-on): the read-back goes into the attempt history; the policy never answers "send again"; nothing without the switch
                count('settled_' + outcome if outcome in ('sent', 'did_not_run') else 'still_unknown')
            elif (team_on and rec['state'] == 'prepared' and rec['kind'] == 'email_reply' and rec['details'].get('team_only')
                  and row[1] == 'ready' and not p.get('closed') and not A.notes(rec, 'team rule not used')):
                count('team_' + _team_send(con, life_home, rec['item'], p, rec, now_dt))
        except Exception as e:
            out.setdefault('errors', []).append(f"{rec['id'][:40]}: {type(e).__name__}: {str(e)[:80]}")
    return out

# ---- notices to other agents -----------------------------------------------------------------------------------------
# Zeus never sends to or reads from another agent. The functions below keep the one shape other modules call
# (research.py, notifier.py, lifecli.py), and every one of them refuses: a proposal for a notice to other agents is
# rejected before it reaches the owner, nothing is frozen, nothing is sent, and no old record of that kind can close.
PEER_REFUSED = 'Notices to other agents are not supported.'


def peer_notice(value):
    """Always refuses: Zeus does not send notices to other agents."""
    raise ValueError(PEER_REFUSED)


def peer_prepare_offer(life, proposal):
    return peer_notice(proposal)


def peer_offer(spec):
    """The plain words an offer would use (formatting only; nothing reads or sends anything)."""
    spec = spec if isinstance(spec, dict) else {}
    extra = (' Still open after direct receipt: ' + str(spec['downstream'])) if spec.get('downstream') else ''
    return 'Send this exact notice to ' + ', '.join(map(str, spec.get('recipients') or [])) + '?\n\n' + str(spec.get('body') or '') + '\n\n' \
        + 'I will verify delivery to each named recipient.' + extra


def freeze_peer_offer(con, life, key, proposed):
    """A notice to other agents is never frozen."""
    return False


def peer_call(life, tool, args):
    raise RuntimeError(PEER_REFUSED)


def peer_state(con, key):
    rec = A.latest(con, key)
    if not rec or rec['kind'] != 'peer_notice':
        return None
    row = con.execute('SELECT state,payload FROM opps WHERE key=?', (key,)).fetchone()
    p = json.loads(row[1]) if row else {}
    spec = rec['details'].get('peer_notice') or {}
    progress = rec.get('receipt') or {}
    missing = [n for n in spec.get('recipients') or [] if progress.get(n, {}).get('state') != 'received']
    return rec, row, p, missing, False


def peer_tick(con, life, action):
    """A record of this kind is never carried out: it is ended as not done, once."""
    if action.get('state') in ('approved', 'sending'):
        A.move(con, action['id'], 'unknown' if action['state'] == 'sending' else 'did_not_run', PEER_REFUSED)


def peer_summary(rec):
    spec = rec['details'].get('peer_notice') or {}
    names = ', '.join(map(str, spec.get('recipients') or []))
    return ('Outstanding: ' + names + '.' if names else ''), False


def peer_report_key(rec, text):
    revision = rec['details'].get('revision')
    return rec['item'] + '#fu-peer:' + str(revision) + ':' + hashlib.sha256(text.encode()).hexdigest()[:24]


def peer_report_current(con, child):
    """A follow-up about a notice to other agents is never current, so it is never sent."""
    return False


def peer_allows_close(con, key, payload):
    """A job about a notice to other agents never closes as done by prose; nor does an older approval whose step is
    addressed to the whole team. A personal reply that only mentions a team closes as usual."""
    commitment = payload.get('commitment') or {}
    packet = payload.get('packet') or {}
    typed = (packet.get('peer_notice') or commitment.get('offered_peer_notice')
             or (commitment.get('delivery_ref') or {}).get('peer_action'))
    has_actions = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='actions'").fetchone()
    action = A.latest(con, key) if has_actions else None
    if typed or (action and action['kind'] == 'peer_notice'):
        return False
    approved = payload.get('approved') or {}
    instruction = (approved.get('step') or approved.get('ask') or '') if isinstance(approved, dict) else ''
    return not legacy_peer_instruction(instruction)


def _team_names() -> set:
    names = set()
    for m in settings.TEAM:
        if isinstance(m, dict):
            names |= {str(n).casefold() for n in [m.get('name')] + list(m.get('aliases') or []) if n}
    return names


def legacy_peer_instruction(instruction):
    """Is this step addressed to the whole team (or to two or more configured team members by name)?"""
    if not isinstance(instruction, str):
        return False
    directive = re.match(r"(?i)^\s*(?:please\s+)?(send|deliver|forward|relay|tell|notify|message|warn|email|text)\s+(.+)",
                         instruction.strip())
    if not directive:
        return False
    first = re.split(r"[.;:\n]", directive[2], maxsplit=1)[0].strip()
    # "the team photo to Alex" addresses Alex. "the team a photo" addresses
    # the team. Resolve a stated destination before interpreting group wording.
    collective = r"(?:(?:the|my)\s+)?(?:(?:whole|entire)\s+)?(?:team|fleet)"
    everyone = r"everyone(?:\s+(?:on|in|across)\s+(?:(?:the|my)\s+)?(?:team|fleet))?"
    if directive[1].casefold() in ('tell', 'notify', 'warn') and re.match(
            r"(?i)^(?:" + collective + '|' + everyone + r")(?=\s|$)", first):
        return True
    if re.match(r"(?i)^" + collective + r"\s+(?:a|an|the|this|that|their|our)\s+",first):
        return True
    if re.match(r"(?i)^(?:a|an|the|this|that|those|these|my|our)\b", first):
        parts = re.split(r"(?i)\s+to\s+", first, maxsplit=1)
        if len(parts) == 2:
            first = parts[1].strip()
        elif not re.fullmatch('(?i)' + collective, first):
            return False
    if re.fullmatch('(?i)(?:' + collective + '|' + everyone + ')', first):
        return True
    group = re.match(r"(?i)^([a-z0-9_-]+)((?:(?:\s*,\s*(?:and\s+)?|\s+(?:and|&)\s+)[a-z0-9_-]+)+)(?=\s|$)",first)
    if not group:
        return False
    names = [word.casefold() for word in re.findall(r"(?i)[a-z0-9_-]+", group[0]) if word.casefold() != 'and']
    # Recognition can hold an old closure only. It never authorizes a new send or replay.
    team = _team_names()
    if not team:
        return bool(re.search(r"(?i)\b(?:relay|notify|warn|tell)\b[^.!?]{0,60}\b(?:crews?|fleet|team)\b",instruction))
    return len(set(names)) >= 2 and all(name in team for name in names)


def peer_can_close(con, key):
    """No notice to other agents is ever sent, so none is ever proven delivered."""
    return False

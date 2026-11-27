"""integrity: one identity per commitment, from intake to closure.

Why. Matching items by "the same real-world thing" on loose evidence (two texts sharing one word with a digit in it,
such as an account name that appears in many alerts) can fold an unrelated email into an open item. The merge then
rewrites the item's title, plan and newest-email mark, a later pass sees "a newer email about it" and retires the
original ask, and a result for the other email closes the item: a real deadline is lost.

What this file adds, all behind ONE switch file (`commit-integrity-on` in the life folder; without it nobody calls in
here and every path is what it was):

  1 identity   every item has a commitment row (commit.py) that carries where it came from (the email thread, the
               calendar entry, the watch, the chat item), its id, its version, and one attempt id per background run.
  2 no merge   similar words, or a key the noticer reused, never fold ANOTHER source into an item. The new arrival
               becomes its own item with its own commitment, and the likeness is only written down (link_suggested).
               Folding stays for the exact same source: the same message, the same thread, the same calendar entry,
               the watch that belongs to the item.
  3 claim      the dispatch claim (commitment id + version + attempt id) is saved and committed BEFORE the scheduler is
               asked to book a job. A claim with no job on record is settled on the next run by READING the scheduler:
               found = that job is the attempt's job; not found = the attempt is over and only then may another be
               booked; could not be read = nothing is booked. One attempt is never booked twice.
  4 gate       at the point of action (booking, using a job's result, the job's own tool calls, the note send) the off
               switch, the item's state, the current version and the permission for that action are read again. A check
               that fails, or cannot be made, holds THAT action only.
  5 receipt    a model saying "done" or "nothing needed" closes nothing. Closing needs a receipt: the note's send read
               back; the job's run record with a clean audit and a named place it read (for a scheduled check, the run
               record and clean audit); or the event the item was about having passed. Without one: something the owner asked
               for in chat, or with a deadline ahead, is TOLD once, in one line of its own, and STAYS
               OPEN (telling the owner is not a receipt for the outcome): it closes on real evidence, on the owner's own word, or when
               its deadline has passed and its later checks have run. Anything else stays open ("awaiting_receipt")
               and is looked at again on every run.

Nothing here sends, books a job by itself, or asks a model. Standard library only. No new table: claims and receipts
live on the item, their history in commit_events.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import commit as CM  # noqa: E402

SWITCH = 'commit-integrity-on'
BOOK_GRACE = 180                # a claim younger than this with no job listed waits one more run (the booking call itself gets 60 seconds)
BOOK_ALARM_AFTER = 45 * 60      # a claim whose booking still cannot be read after this long: one repair ticket (worker.NEVER_RAN_AFTER)
PARK_FOR = 24 * 3600            # an item waiting for a receipt is looked at on every run; its own time is kept this far ahead so nothing tells or re-works it
PARK_NEAR = 2 * 3600
CLOSE_VERBS = ('done', 'drop', 'mute', 'told')    # `told` marks a worked item done too, so it needs the item's key like the others
CHECK_CLOSES_ON_AUDIT = True    # a scheduled follow-up CHECK that ran to the end with a clean audit record may close quietly; False = it waits for a receipt like any item
OWED_ALWAYS_TOLD = False        # True = a "nothing needed" on something the owner asked for in chat, or with a deadline ahead, is told even when the job's run record and named sources stand behind it
RESTART_CAP = 2                 # times one item is started again because the job gate refused a run that made no call; after that the owner is told without the prep
MANDATE_WAIT = 15 * 60          # a mandate file that cannot be read holds the background runs this long; then the old rule (told without prep) and one ticket
MANDATE_MARK = 'integrity-mandate-unreadable.json'
HOLDS = 'integrity-holds.jsonl'
LAST = {}                       # the last hold of each kind in this process (for the run's report and for tests)
_clock = time.time              # the real clock (a claim's age is never measured against the run's own start time); a test may stand in for it
_HOME = None                    # the life folder of the ledger this process last worked on (for the two hooks that are handed none)


def on(life) -> bool:
    global _HOME
    try:
        if life is None:
            return False
        _HOME = Path(life)
        return (_HOME / SWITCH).exists()
    except (OSError, TypeError):
        return False


def on_for(con) -> bool:
    return on(CM._home(con))


def _default_home() -> Path:
    """The life folder of this process (the same rule tick.py and lifecli.py use), for the one hook that is handed no folder."""
    import os
    import settings
    return Path(os.environ.get('LIFE_HOME') or settings.LIFE_HOME)


def _here() -> Path:
    """The life folder for a hook that is handed none: the one of the ledger this process last worked on, else the
    process's own (the rule tick.py and lifecli.py use)."""
    return _HOME if _HOME is not None else _default_home()


def _moment(value):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else None


def _hold(life, action: str, key, why: str, res=None) -> None:
    """One held action: said in the run's report, kept for this process, and one line in the life folder."""
    LAST[action] = {'key': key, 'why': why, 'at': time.time()}
    if isinstance(res, dict):
        res.setdefault('integrity_held', []).append(f'{action} {str(key)[:60]}: {why}'[:200])
    try:
        with open(Path(life) / HOLDS, 'a') as fh:
            fh.write(json.dumps({'at': time.time(), 'action': action, 'key': key, 'why': why}, default=str) + '\n')
    except OSError:
        pass


def _event(con, key, kind: str, after: dict, now=None, op_id=None) -> None:
    """One line in the commitment's history. It carries no copy of the commitment, so it changes nothing about it."""
    CM.ensure_schema(con)
    idx = CM._index(con, key)
    if idx is not None:
        CM._event(con, idx['commitment_id'], idx['revision'], kind, 'tick', None, op_id, None, after, CM._now(now))


# ---- 1. identity -----------------------------------------------------------------------------------------------------------

def source_of(obs: dict) -> str:
    """Where an observation came from, as one exact name: the mail thread (the message when no thread is known), the
    calendar entry, the watch."""
    oid = str(obs.get('id') or '')
    if obs.get('source') == 'email':
        box = str(obs.get('mailbox') or '')
        if obs.get('thread_id'):
            return f"email:{box}:thread:{obs['thread_id']}"
        return f"email:{box}:msg:{oid.split(':', 2)[-1]}"
    if oid.startswith('watch:'):
        return 'watch:' + oid[len('watch:'):].rpartition(':')[0]
    return oid


def item_source(key: str, payload: dict, sources) -> str:
    """The same name for an item that is already in the ledger, from what the item itself recorded when it was made."""
    if '#fu' in key:
        return 'followup:' + key.split('#fu', 1)[0]
    th = payload.get('thread') if isinstance(payload.get('thread'), dict) else {}
    if th.get('thread_id'):
        return f"email:{th.get('mailbox') or ''}:thread:{th['thread_id']}"
    for s in sources if isinstance(sources, list) else []:
        s = str(s)
        if s.startswith('email:'):
            _, box, mid = (s.split(':', 2) + ['', ''])[:3]
            return f'email:{box}:msg:{mid}'
        if s.startswith('watch:'):
            return 'watch:' + s[len('watch:'):].rpartition(':')[0]
        if s:
            return s
    return ('chat:' if payload.get('_source') == 'conversation' else 'item:') + key


def same_source(life, row, obs: dict) -> bool:
    """Is this observation from the very source this item came from? Exact tests only: the same record id, the same
    recorded source name, the same mail thread, the same calendar occurrence, the watch that belongs to the item.
    Anything that cannot be shown to be the same is another source."""
    try:
        p, srcs = json.loads(row['payload']), json.loads(row['sources'])
    except (TypeError, ValueError):
        return False
    oid, mine = str(obs.get('id') or ''), source_of(obs)
    if oid and oid in srcs:
        return True
    c = p.get('commitment') if isinstance(p.get('commitment'), dict) else {}
    if mine and any(isinstance(x, dict) and x.get('id') == mine for x in c.get('source_refs') or []):
        return True
    if obs.get('source') == 'email':
        th = p.get('thread') if isinstance(p.get('thread'), dict) else {}
        return bool(obs.get('thread_id')) and th.get('thread_id') == obs.get('thread_id') and th.get('mailbox') in (None, '', obs.get('mailbox'))
    if obs.get('source') == 'calendar':
        seen = (p.get('fresh') or {}).get('seen') if isinstance(p.get('fresh'), dict) else None
        if isinstance(seen, dict) and seen.get('kind') == 'calendar':
            try:
                import fresh as FR
                return FR.same_occurrence(seen, FR.stamp_of(obs))
            except Exception:
                return False
        return False
    if oid.startswith('watch:'):
        wid = oid[len('watch:'):].rpartition(':')[0]
        return (CM._watch_rows(life, [wid]).get(wid) or {}).get('item') == row['key']
    return False


def _sources(con, key) -> list:
    r = con.execute('SELECT sources FROM opps WHERE key=?', (key,)).fetchone()
    try:
        return json.loads(tuple(r)[0]) if r else []
    except (TypeError, ValueError):
        return []


def ensure(con, life, key, source=None, now=None):
    """Every item that can be worked or closed has a commitment row. -> its index row (made now when it had none), or
    None when the item is gone. Joins the save the caller has open."""
    CM.ensure_schema(con)
    idx = CM._index(con, key)
    if idx is not None:
        return idx
    opp = CM._opp(con, key)
    if opp is None:
        return None
    src = source or item_source(key, opp['payload'], _sources(con, key))
    CM._joined(con, 'ci_ensure', lambda: CM._adopt(con, Path(life), key, 'tick', src, None, CM._now(now), closed=True))
    return CM._index(con, key)


def _carry(con, key, opp: dict) -> None:
    """The caller saves its own copy of the item next: it must carry the commitment the ledger has."""
    item = CM._opp(con, key)
    if item and isinstance(item['payload'].get('commitment'), dict):
        opp['commitment'] = item['payload']['commitment']


def ensure_all(con, life, now=None) -> dict:
    """Every open item without a commitment gets one (later checks included), and a commitment that never recorded
    where it came from is told now. Nothing else about an item changes, its `updated` time included."""
    out = {}
    marks = ','.join('?' * len(CM.OPEN))
    CM.ensure_schema(con)
    for r in con.execute(f'SELECT key FROM opps WHERE state IN ({marks}) AND key NOT IN (SELECT opp_key FROM commitments) ORDER BY first_seen, key', CM.OPEN).fetchall():
        try:
            if ensure(con, life, tuple(r)[0], now=now) is not None:
                out['adopted'] = out.get('adopted', 0) + 1
        except Exception as e:
            out.setdefault('errors', []).append(f'{str(tuple(r)[0])[:40]}: {type(e).__name__}: {str(e)[:80]}')
    con.commit()
    now = CM._now(now)
    for idx, opp, obj in CM._open(con):
        refs = obj.get('source_refs') if isinstance(obj.get('source_refs'), list) else []
        if refs and isinstance(refs[0], dict) and refs[0].get('id'):
            continue
        src = item_source(opp['key'], opp['payload'], _sources(con, opp['key']))
        with CM._tx(con):
            idx = CM._index(con, idx['commitment_id'])
            opp = CM._opp(con, idx['opp_key']) if idx else None
            obj = CM._object(con, idx, opp) if opp else None
            if obj is None or not CM._live(opp, obj):
                continue
            before = copy.deepcopy(obj)
            refs = obj.get('source_refs') if isinstance(obj.get('source_refs'), list) and obj.get('source_refs') else [{'kind': 'note', 'id': '', 'at': CM._iso(now)}]
            refs[0] = dict(refs[0], id=src)
            obj['source_refs'] = refs
            CM._put(con, idx, opp, obj, now, touch=False)
            CM._event(con, idx['commitment_id'], idx['revision'], 'identity', 'tick', src, None, {'commitment': before}, {'commitment': obj}, now)
        out['sourced'] = out.get('sourced', 0) + 1
    return out


# ---- 2. similar is not the same (tick.upsert) ----------------------------------------------------------------------------

def _link(row, why: str) -> dict:
    try:
        title = json.loads(row['payload']).get('title')
    except (TypeError, ValueError):
        title = None
    return {'key': row['key'], 'title': title, 'state': row['state'], 'why': why}


def intake(con, life, obs: dict, opp: dict, row, twin_of):
    """tick.upsert, before anything is folded. `row` is the item saved under this very key, or None.
    -> (the item this arrival may be folded into, or None for a new item; the opportunity, re-keyed when its key belongs
        to another source's item; a suggested link, or None)."""
    if not on(life):                                  # without the switch: exactly what tick.upsert always did
        if row is None:
            twin, _ = twin_of(con, opp)
            if twin:
                row = con.execute('SELECT * FROM opps WHERE key=?', (twin,)).fetchone()
        return row, opp, None
    if row is None:
        twin, _ = twin_of(con, opp)
        trow = con.execute('SELECT * FROM opps WHERE key=?', (twin,)).fetchone() if twin else None
        if trow is None:
            return None, opp, None
        if same_source(life, trow, obs):
            return trow, opp, None                   # the same thread or entry under another key: folded as always
        return None, opp, _link(trow, 'similar words')
    if row['state'] == 'skipped' or same_source(life, row, obs):
        return row, opp, None
    key = f"{row['key']}~s{hashlib.sha1(source_of(obs).encode()).hexdigest()[:6]}"
    own = con.execute('SELECT * FROM opps WHERE key=?', (key,)).fetchone()
    return own, {**opp, 'dedupe_key': key}, (None if own is not None else _link(row, 'the same key, read from another source'))


def born(con, life, key: str, obs: dict, link) -> None:
    """tick.upsert, right after a new item was saved: it gets its commitment, with the source it came from, in the same
    save. A suggested link is written on the NEW item and as one history line on each side. The other item is not
    touched. Never raises: an item is never lost over its bookkeeping (ensure_all gives it its commitment on the next run)."""
    try:
        if not on(life):
            return
        ensure(con, life, key, source=source_of(obs))
        if not link:
            return
        note = dict(link, at=time.time(), source=source_of(obs))
        row = con.execute('SELECT payload FROM opps WHERE key=?', (key,)).fetchone()
        p = json.loads(tuple(row)[0])
        p['link_suggested'] = (list(p.get('link_suggested') or []) + [note])[-5:]
        con.execute('UPDATE opps SET payload=? WHERE key=?', (json.dumps(p, default=str), key))
        _event(con, key, 'link_suggested', {'to': link['key'], 'why': link['why'], 'merged': False})
        _event(con, link['key'], 'link_suggested', {'from': key, 'why': link['why'], 'merged': False})
    except Exception:
        return
    for step in (nudge, carry_corrections):          # each stands alone: trouble in one never loses the item or the other
        try:
            step(con, life, key, link['key'])
        except Exception:
            pass


def nudge(con, life, key: str, link_key: str) -> bool:
    """An URGENT new item with a suggested link to a job that is still waiting to be worked: that job becomes due when
    the urgent news is due, so it is looked at again now, at its own current version. Only its time moves, and only
    earlier. Its title, plan, deadline, version and everything else stay exactly as they are; nothing is merged.
    (So a "CANCELLED" on the owner's calendar brings a related job the owner gave in chat forward.) -> True when
    the time was moved."""
    if not on(life):
        return False
    new = con.execute('SELECT urgency,surface_at FROM opps WHERE key=?', (key,)).fetchone()
    old = con.execute('SELECT state,surface_at FROM opps WHERE key=?', (link_key,)).fetchone()
    if new is None or old is None or tuple(new)[0] != 'now' or tuple(old)[0] != 'pending':
        return False
    a, b = _moment(tuple(new)[1]), _moment(tuple(old)[1])
    if a is None or (b is not None and b <= a):
        return False
    con.execute("UPDATE opps SET surface_at=? WHERE key=? AND state='pending'", (tuple(new)[1], link_key))
    _event(con, link_key, 'nudged', {'by': key, 'from': tuple(old)[1], 'to': tuple(new)[1], 'why': 'urgent news with a suggested link to this job'})
    return True


def link_corrections(con, link_key: str) -> list:
    """What the owner corrected on a job, from the job's own record: every fact that keeps its old value, and a time the owner moved.
    -> [{'name', 'value', 'was'}] (at most six)."""
    CM.ensure_schema(con)
    obj = CM.get(con, link_key) or {}
    out = [{'name': f.get('name'), 'value': f.get('value'), 'was': f.get('was')} for f in obj.get('facts') or []
           if isinstance(f, dict) and 'was' in f and f.get('name') != 'correction' and f.get('was') not in (None, '')]
    for r in con.execute("SELECT before, after FROM commit_events WHERE commitment_id=? AND kind='corrected' AND before IS NOT NULL AND after IS NOT NULL ORDER BY seq",
                         (obj.get('commitment_id'),)).fetchall():
        try:                                          # a time THAT correction itself moved (never a later move by something else, such as a nudge)
            was, to = (((json.loads(x) or {}).get('item') or {}).get('surface_at') for x in tuple(r))
        except (TypeError, ValueError):
            continue
        if _moment(was) is not None and _moment(to) is not None and abs((_moment(was) - _moment(to)).total_seconds()) >= 60:
            out.append({'name': 'time', 'value': to, 'was': was})
    seen, kept = set(), []
    for c in out:
        mark = json.dumps([c['value'], c['was']], default=str)
        if mark not in seen:
            seen.add(mark)
            kept.append(c)
    return kept[:6]


def carry_corrections(con, life, key: str, link_key: str) -> int:
    """A new item with a suggested link to a job the owner corrected: what the owner corrected there is handed to the new item as given
    facts (on the item for the code check, and on its commitment, so the worker's prompt says them through the line
    commit.py already writes: "Corrected since first asked: arrival is 8:40PM, not 8:40AM"). The linked job is only read.
    -> how many were carried."""
    if not on(life):
        return 0
    found = link_corrections(con, link_key)
    if not found:
        return 0
    row = con.execute('SELECT payload FROM opps WHERE key=?', (key,)).fetchone()
    p = json.loads(tuple(row)[0])
    p['link_corrections'] = [dict(c, of=link_key) for c in found]
    con.execute('UPDATE opps SET payload=? WHERE key=?', (json.dumps(p, default=str), key))
    idx = CM._index(con, key)
    opp = CM._opp(con, key)
    obj = CM._object(con, idx, opp) if idx and opp else None
    if obj is not None:
        now = CM._now()
        before = copy.deepcopy(obj)
        linked = CM._index(con, link_key) or {}
        have = {(f.get('name'), json.dumps(f.get('value'), default=str)) for f in obj.get('facts') or [] if isinstance(f, dict)}
        obj['facts'] = list(obj.get('facts') or []) + [
            {'name': c['name'], 'value': c['value'], 'was': c['was'], 'source_ref': linked.get('commitment_id') or link_key, 'certain': True, 'at': CM._iso(now), 'by': 'link'}
            for c in found if (c['name'], json.dumps(c['value'], default=str)) not in have]
        CM._put(con, idx, opp, obj, now, touch=False)
        CM._event(con, idx['commitment_id'], idx['revision'], 'observed', 'tick', None, None, {'commitment': before}, {'commitment': obj, 'corrections_of': link_key}, now)
    return len(found)


_CONTRAST = None


def _tokens(value):
    """How a value shows up in a sentence: a time as its clock ("8:40am", "8 am") and as its day ("Nov 16"); anything
    else as its own words. -> (clock regex or None, day regex or None, flat words or '')."""
    import re
    t = _moment(value)
    if t is None:
        return None, None, ' '.join(str(value or '').lower().split())
    h, m, ap = t.hour % 12 or 12, t.minute, 'a' if t.hour < 12 else 'p'
    alts = [rf'{h}:{m:02d}\s?{ap}\.?\s?m\b'] + ([rf'{h}\s?{ap}\.?\s?m\b'] if m == 0 else [])
    clock = re.compile(r'(?<![\d:])0?(?:' + '|'.join(alts) + ')', re.I)
    day = re.compile(rf"\b{t.strftime('%b')}[a-z]*\.?\s+0?{t.day}\b|(?<![\d/]){t.month}/0?{t.day}\b", re.I)
    return clock, day, ''


def states_superseded(text, corrections) -> list:
    """Does this text state, as a fact, a value the owner corrected away? -> one plain line per such value ([] = it does not).
    A sentence counts when it carries the OLD value, does not also carry the corrected one, and is not plainly setting
    the old one aside ("not 8:40am", "no longer", "instead of", "the calendar still says ..."). Only what code can
    match: a clock time, a day of the month, or the old words themselves (four letters or more)."""
    global _CONTRAST
    import re
    if _CONTRAST is None:
        _CONTRAST = re.compile(r"\b(?:not|isn'?t|no longer|instead of|rather than|used to|corrected|correction|still (?:says|shows|lists)|old)\b", re.I)
    out = []
    sentences = [s for s in re.split(r'(?<=[.!?;])\s+|\n', str(text or '')) if s.strip()]
    for c in corrections if isinstance(corrections, list) else []:
        if not isinstance(c, dict):
            continue
        wc, wd, ww = _tokens(c.get('was'))
        vc, vd, vw = _tokens(c.get('value'))
        for s in sentences:
            if _CONTRAST.search(s):
                continue
            flat = ' '.join(s.lower().split())
            if wc is not None:
                same_clock = vc is not None and vc.pattern == wc.pattern
                hit = (wd.search(s) and not (vd is not None and vd.search(s))) if same_clock else (wc.search(s) and not (vc is not None and vc.search(s)))
            else:
                hit = len(ww) >= 4 and ww in flat and not (vw and vw in flat)
            if hit:
                out.append(f"{CM._fact_name(c)} is {CM._fact_value(c.get('value'))}, not {CM._fact_value(c.get('was'))}")
                break
    return out


def stale_line(con, key, opp: dict, packet: dict, now_dt: datetime, res: dict) -> bool:
    """worker._ready, first thing, for ANY result about to be readied. Two looks, both by code:
    1. a "done" nothing outside the job stands behind, on a chat ask or a deadline item, is reworded (unconfirmed_done);
    2. does it state a value the owner corrected on a job this item is linked to (states_superseded against the corrections
       carried at intake)? False = no (or no such link, or the switch is off): go on. True = handled here, not readied:
      the first time, for a job's result   the item goes back for its one further run (the corrections are in its prompt)
      otherwise                            that line is held: the item stays open, out of every note, like any item
                                           waiting for a receipt. Nothing else is held."""
    life = CM._home(con)
    if not on(life) or not isinstance(packet, dict):
        return False
    try:
        if unconfirmed_done(con, key, opp, packet, now_dt, res):      # a "done" nothing outside the job stands behind: reworded (and, when the owner was already told it for this version, not said again)
            return True
    except CM.Stale:
        raise
    except Exception as e:
        _hold(life, 'ready', key, f'the unconfirmed-result check could not be made ({type(e).__name__}: {str(e)[:80]})', res)
    if not opp.get('link_corrections'):
        return False
    try:
        d = packet.get('decision') if isinstance(packet.get('decision'), dict) else {}
        bad = states_superseded(' \n'.join(str(x) for x in (packet.get('headline'), packet.get('details'), d.get('ask'), d.get('ready'), d.get('default')) if x), opp['link_corrections'])
        if not bad:
            return False
        ex = opp.get('exec') if isinstance(opp.get('exec'), dict) else {}
        n = int(opp.get('stale_lines') or 0) + 1
        opp['stale_lines'] = n
        _event(con, key, 'stale_line', {'said': bad, 'count': n}, now_dt)
        res['stale_lines'] = res.get('stale_lines', 0) + 1
        if n == 1 and ex.get('job_id') and int(ex.get('tries') or 1) < 2:
            opp['exec'] = {'retry_used': True, 'created_at': ex.get('created_at')}
            con.execute("UPDATE opps SET state='pending', payload=?, updated=? WHERE key=? AND state='executing'", (json.dumps(opp, default=str), time.time(), key))
            return True
        import worker as W
        opp['packet'] = packet
        _park(con, key, opp, 'moot', 'its result states a value the owner corrected on a linked job (' + bad[0] + ')', now_dt, save=lambda: W._save(con, key, 'ready', opp))
        return True
    except CM.Stale:
        raise
    except Exception as e:
        _hold(life, 'ready', key, f'the corrected-value check could not be made ({type(e).__name__}: {str(e)[:80]})', res)
        return False


def as_told(life, row, payload: dict) -> bool:
    """tick.upsert, for the fold decision only: an item that is open ONLY because this file keeps it open counts as
    already told, as it would be without the switch. That is an item waiting for a receipt, and an item with nothing
    to ask the owner whose note may or may not have arrived. New mail about it is then not folded into it (nothing would work
    it again): it takes the engine's own road for news about something already handled and becomes an update item
    that is worked and told. The item itself is not changed. False without the switch."""
    try:
        if not on(life) or not isinstance(payload, dict):
            return False
        if row['state'] == 'ready':
            return _parked(payload) is not None
        d = (payload.get('packet') or {}).get('decision') or {}
        mine = bool((payload.get('told') or {}).get('uncertain')) or isinstance(payload.get('open_until_receipt'), dict)
        return row['state'] == 'surfaced' and mine and not (d.get('ask') or d.get('ready'))
    except Exception:
        return False


def own_mail(opp: dict, mail: dict) -> dict:
    """fresh._mail_ref: a "newest email" mark that points at another thread than the one the item came from is not the
    item's own mail (it got there by an old merge). It is not used: the item is compared with its own thread, or, when
    that cannot be worked out, the answer is "cannot tell". With the mark in its own thread, or no thread on record,
    the same dict comes back."""
    try:
        th = opp.get('thread') if isinstance(opp.get('thread'), dict) else {}
        if th.get('thread_id') and mail.get('thread_id') and mail['thread_id'] != th['thread_id'] and (_here() / SWITCH).exists():
            return {}
    except Exception:
        pass
    return mail


def named_exactly(key: str, said: str) -> bool:
    """lifecli: was the item named by its key (whole, or its beginning)? A match by loose words is only a suggestion."""
    try:
        if not (_here() / SWITCH).exists():
            return True                               # without the switch any match is taken, as before
    except OSError:
        pass
    t = str(said or '').strip().lower()
    return bool(t) and str(key).lower().startswith(t)


# ---- 3. the dispatch claim (worker.py) -------------------------------------------------------------------------------------

def tag(claimed) -> str:
    """The mark the booked job carries in its name, so the scheduler can be asked about this one attempt."""
    return f" [{claimed['attempt_id']}]" if isinstance(claimed, dict) and claimed.get('attempt_id') else ''


def _unstart(con, key) -> None:
    """A standing-OK step that one-send marked "being carried out" just before its job was to be booked, when no job
    was booked after all: the mark is taken back. Otherwise the next run would tell the owner it "may or may not have been
    done", which is false: nothing was asked of the scheduler."""
    try:
        import actions as A
        if not A.on_for(con):
            return
        rec = A.latest(con, key, 'granted_step')
        if rec and rec.get('state') == 'sending':
            A.move(con, rec['id'], 'did_not_run', 'no job was booked for it')
    except Exception:
        pass


def claim(con, life, key, opp: dict, run_at: datetime, tries: int, now_dt: datetime, base=None, **more) -> dict:
    """Before the scheduler is asked: the item is saved as being worked by ONE named attempt, for one version of its
    commitment, and that save is committed. -> the attempt (the item's `exec`), or None without the switch (nothing is
    written then). Raises commit.Stale, writing nothing, when the job was corrected since this run read the item."""
    if not on(life):
        return None
    try:
        idx = ensure(con, life, key, now=now_dt)
        if idx is None:
            raise CM.Stale(f'{key}: the item is gone')
        was = opp.get('exec') if isinstance(opp.get('exec'), dict) else None
        mine = opp.get('commitment') if isinstance(opp.get('commitment'), dict) else None
        if mine is None:
            _carry(con, key, opp)                     # the item got its commitment just now: the caller's copy carries it from here
        rev = int((mine or {}).get('revision') or idx['revision'])     # the version the caller's copy (and so its prompt) was read at
        ex = {k: v for k, v in (base or {}).items() if k not in ('job_id', 'done_at', 'attempt_id', 'booking', 'heard')}
        ex.update(attempt_id='a_' + uuid.uuid4().hex[:12], booking='claimed', claimed_at=_clock(), claimed_run=now_dt.timestamp(), created_at=time.time(),
                  run_at_ts=run_at.timestamp(), tries=2 if (was or {}).get('retry_used') else int(tries),
                  commitment_id=idx['commitment_id'], commitment_rev=rev, **more)
        if was and base is None:
            ex['was'] = was                           # what the item carried before (a used retry, a stale result): put back if this attempt is never booked
        saved = dict(opp, exec=ex)
        cur = con.execute("UPDATE opps SET state='executing', payload=?, updated=? WHERE key=? AND state IN ('pending','executing') "
                          'AND NOT EXISTS (SELECT 1 FROM commitments WHERE opp_key=opps.key AND revision<>?)',
                          (json.dumps(saved, default=str), time.time(), key, ex['commitment_rev']))
        if cur.rowcount != 1:
            raise CM.Stale(f'{key}: the job changed while its background run was being claimed; nothing was booked')
        _event(con, key, 'dispatch_claimed', {'attempt': {k: ex.get(k) for k in ('attempt_id', 'commitment_id', 'commitment_rev', 'tries', 'run_at_ts')}},
               now_dt, op_id='attempt:' + ex['attempt_id'])
        con.commit()
    except BaseException:
        _unstart(con, key)                            # nothing was booked: a step one-send marked as started is not left looking as if it may have gone
        raise
    opp['exec'] = ex
    return ex


def booked(con, key, claimed: dict, ex: dict, now_dt=None, how: str = 'the scheduler answered') -> dict:
    """The scheduler took the job: the attempt names it. The caller saves the item next."""
    if not isinstance(claimed, dict) or not claimed.get('attempt_id'):
        return ex                                     # no claim (the switch is off): the booked job's record is what it always was
    out = dict(ex)
    out.update({k: claimed[k] for k in ('attempt_id', 'claimed_at', 'commitment_id', 'commitment_rev', 'heard') if k in claimed}, booking='booked')   # `heard`: crossfence.py (switch cross-channel-fence-on); never there without it
    out.pop('was', None)
    try:
        _event(con, key, 'dispatch_booked', {'attempt_id': out.get('attempt_id'), 'job': out.get('job_id'), 'how': how}, now_dt)
    except Exception:
        pass
    return out


LIVE_WINDOW = 3600              # a run whose own time is within this of the real clock is a live run


def _claim_age(ex: dict, now_dt: datetime) -> float:
    """How long ago an attempt was claimed. In a live run: by the real clock, from the moment the claim was really made
    (never from the start of the run that made it: a run can spend minutes noticing before it books anything, and that
    must not eat the wait for a booking to land). In a replayed run, whose time is nowhere near the real clock (a test's
    fixed story time): by that run's own clock, from the time the claiming run was given."""
    now, real = now_dt.timestamp(), _clock()
    if abs(real - now) <= LIVE_WINDOW:
        return real - float(ex.get('claimed_at') or real)
    return now - float(ex.get('claimed_run') or now)


def scheduler_jobs():
    """The scheduler's own list of jobs, or None when it cannot be read. Not knowing is never "not there"."""
    try:
        import worker as W
        data = json.loads(Path(W.JOBS_FILE).read_text())
    except (OSError, ValueError):
        return None
    jobs = data.get('jobs', data) if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else None


def reconcile_claims(con, life, now_dt: datetime, res: dict, listed=None) -> set:
    """worker.run, first thing: every attempt that was claimed and has no job on record (the run died, or the booking
    gave no answer) is settled by reading the scheduler, before anything is harvested or booked.
      listed there      that job is this attempt's job; the normal harvest takes over
      not listed        the attempt is over: a first run goes back to be started again (as a NEW attempt); a second run
                        ends the way a failed second booking always did (the owner is told without the prep)
      too soon / unreadable   nothing is done; the item is neither harvested nor booked for on this run
    -> the keys that wait."""
    waiting = set()
    if not on(life):
        return waiting
    try:
        rows = con.execute("SELECT key,payload FROM opps WHERE state='executing' AND json_extract(payload,'$.exec.attempt_id') IS NOT NULL "
                           "AND json_extract(payload,'$.exec.job_id') IS NULL").fetchall()
    except Exception as e:
        res.setdefault('errors', []).append(f'claims: {type(e).__name__}: {str(e)[:80]}')
        return waiting
    if not rows:
        return waiting
    import worker as W
    jobs = (listed or scheduler_jobs)()
    for r in rows:
        key = r['key']
        try:
            opp = json.loads(r['payload'])
            ex = opp['exec']
            age = _claim_age(ex, now_dt)
            mine = [j for j in jobs or [] if tag(ex).strip() in str(j.get('name') or '') or tag(ex).strip() in str(j.get('prompt') or '')]
            if jobs is None or (not mine and age < BOOK_GRACE):
                waiting.add(key)
                why = 'the scheduler could not be read' if jobs is None else 'the booking may still be landing'
                _hold(life, 'book', key, f'{why}; nothing is booked until it can be read', res)
                if jobs is None and age > BOOK_ALARM_AFTER and not ex.get('booking_alarmed'):
                    W._alarm(life, key, f"Zeus cannot tell whether a background job was booked for: {opp.get('title')}", detail=f"attempt {ex.get('attempt_id')}: the scheduler's job list could not be read")
                    ex['booking_alarmed'] = True
                    con.execute("UPDATE opps SET payload=? WHERE key=? AND state='executing'", (json.dumps(opp, default=str), key))
                continue
            if mine:
                ex = booked(con, key, ex, dict(ex, job_id=str(mine[0].get('id'))), now_dt, how='read from the scheduler after the run that claimed it ended')
                if len(mine) > 1:                     # never expected: one attempt, one booking call. Written down, never used
                    ex['extra_jobs'] = [str(j.get('id')) for j in mine[1:]]
                    W._alarm(life, key, f"Zeus found more than one background job booked for one attempt on: {opp.get('title')}", detail=f"attempt {ex.get('attempt_id')}: jobs {[str(j.get('id')) for j in mine]}")
                c = opp.get('commitment') if isinstance(opp.get('commitment'), dict) else {}
                if ex.get('commitment_rev') is not None and int(c.get('revision') or ex['commitment_rev']) != int(ex['commitment_rev']):
                    opp['exec'] = ex                  # corrected since the claim: the job stays named for the version it was CLAIMED at (exec_for would stamp it with the new one), so the version fence meets its result at harvest
                else:
                    try:
                        opp['exec'] = CM.exec_for(con, life, key, opp, ex, now_dt)
                    except CM.Stale:
                        opp['exec'] = ex
                con.execute("UPDATE opps SET payload=?, updated=? WHERE key=? AND state='executing'", (json.dumps(opp, default=str), time.time(), key))
                res['claims_found'] = res.get('claims_found', 0) + 1
            else:
                _event(con, key, 'dispatch_not_booked', {'attempt_id': ex.get('attempt_id'), 'checked': 'the scheduler lists no job for this attempt'}, now_dt)
                opp['dispatch_last'] = {'attempt_id': ex.get('attempt_id'), 'outcome': 'not booked', 'checked_at': now_dt.timestamp()}
                res['claims_not_booked'] = res.get('claims_not_booked', 0) + 1
                if W.standing_ok(opp):
                    # "Not listed" is also what a job looks like that started and was cut short. A step that runs for
                    # real is never sent back to be started again on that: the owner is told plainly, and one-send's own
                    # record says it may have gone, so only reading where it went settles it.
                    try:
                        import actions as A
                        rec = A.latest(con, key, 'granted_step') if A.on_for(con) else None
                        if rec and rec.get('state') == 'sending':
                            A.move(con, rec['id'], 'unknown', "its job is not on the scheduler's list, so it may or may not have started")
                    except Exception:
                        pass
                    opp['exec'] = {'created_at': ex.get('created_at'), 'tries': ex.get('tries'), 'retry_used': True}
                    W._ready(con, key, opp, W.synth_packet(opp, 'not started again'), now_dt, res)
                elif int(ex.get('tries') or 1) >= 2 and 'was' not in ex:
                    opp['exec'] = {k: v for k, v in ex.items() if k in ('tries', 'last_error', 'created_at', 'prev_job', 'no_result')} | {'retry_used': True}
                    W._alarm(life, key, f"Zeus background work failed twice on: {opp.get('title')}", detail='the second run could not be booked')
                    W._ready(con, key, opp, W.synth_packet(opp, 'the background work failed twice'), now_dt, res)
                    __import__('recover').gave_up(con, life, key, 'job_interrupted', now_dt, res)
                    res['failed'] += 1
                else:
                    if isinstance(ex.get('was'), dict):
                        opp['exec'] = ex['was']
                    else:
                        opp.pop('exec', None)
                    con.execute("UPDATE opps SET state='pending', payload=?, updated=? WHERE key=? AND state='executing'", (json.dumps(opp, default=str), time.time(), key))
            con.commit()
        except Exception as e:
            if con.in_transaction:
                con.rollback()
            waiting.add(key)
            res.setdefault('errors', []).append(f'claim {str(key)[:40]}: {type(e).__name__}: {str(e)[:80]}')
    return waiting


# ---- 4. the point of action ------------------------------------------------------------------------------------------------

WANTS = {'start': ('pending',), 'result': ('executing',), 'job': ('executing',), 'send': ('ready',)}


def _may_work(life, key, p: dict):
    """Permission for a background run, read now: the mandate, the executor switch, and, for a step that runs under a
    standing OK, that the standing OK is still on file."""
    life = Path(life)
    if (life / 'executor-off').exists():
        return 'background work is switched off'
    try:
        if not json.loads((life / 'mandate.json').read_text()).get('active'):
            return 'there is no active mandate'
    except (OSError, ValueError):
        return 'the mandate could not be read'
    if any(isinstance(s, dict) and s.get('granted') for s in ((p.get('decision') or {}).get('steps') or [])):
        try:
            if not isinstance(json.loads((life / 'grants.json').read_text()), dict):
                return 'the standing OKs could not be read'
        except (OSError, ValueError):
            return 'the standing OKs could not be read'
        if __import__('authority').grant_for(life, p.get('dedupe_key') or key)[1] is None:
            return 'the standing OK this step ran under is no longer on file'
    return ''


def mandate_unreadable(life) -> bool:
    """worker.run: the mandate file is there and cannot be read. That is "cannot tell", not "no mandate": with the switch
    the background runs wait, for a short while only (MANDATE_WAIT). After that this answers False again, so the worker's
    old rule takes over (due items are told without their prep, which is safe work), and ONE repair ticket is raised.
    Only the booking is held: nothing else in the run waits on this."""
    if not on(life):
        return False
    path, mark = Path(life) / 'mandate.json', Path(life) / MANDATE_MARK
    try:
        json.loads(path.read_text())
        bad = False
    except FileNotFoundError:
        bad = False
    except (OSError, ValueError):
        bad = True
    if not bad:
        try:
            mark.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        seen = json.loads(mark.read_text())
    except (OSError, ValueError):
        seen = {'since': _clock()}
        try:
            mark.write_text(json.dumps(seen))
        except OSError:
            pass
    if _clock() - float(seen.get('since') or 0) < MANDATE_WAIT:
        return True
    if not seen.get('ticket'):
        try:
            __import__('worker')._alarm(life, 'mandate', 'Zeus cannot read its mandate file, so background work is off and items are told without their prep',
                                        detail='mandate.json is there and could not be read or parsed')
            mark.write_text(json.dumps(dict(seen, ticket=True)))
        except Exception:
            pass
    return False


def gate(con, life, key, action: str, revision=None, attempt=None) -> tuple:
    """Read again, at the moment of acting. -> (True, '', '') or (False, code, the plain reason). Codes: off (the owner's stop),
    state (the item was closed, stopped or moved on), stale (the job was corrected since, or this is not its current
    attempt), permission, unreadable (the check itself could not be made). Never raises."""
    try:
        life = Path(life)
        if (life / 'off').exists():
            return False, 'off', 'the off switch is set'
        row = con.execute('SELECT state,payload FROM opps WHERE key=?', (key,)).fetchone()
        if row is None:
            return False, 'state', 'the item is gone'
        state, p = tuple(row)[0], json.loads(tuple(row)[1])
        if state not in WANTS[action] or (p.get('closed') and action != 'send'):     # for a note the engine's own "ready" decides; an old close mark on a ready item must not hold every note
            return False, 'state', f'the item is {state} now' + (' (closed)' if p.get('closed') else '')
        if _parked(p) and action == 'send':
            return False, 'state', 'it is waiting for a receipt, not for a note'
        ex = p.get('exec') if isinstance(p.get('exec'), dict) else {}
        if attempt is not None and ex.get('attempt_id') != attempt:
            return False, 'stale', 'this is not the current attempt for the item'
        CM.ensure_schema(con)
        idx = CM._index(con, key)
        if idx is not None and idx['phase'] not in ('done', 'stopped'):
            rev = revision if revision is not None else (ex.get('commitment_rev') if action != 'start' else None)
            if rev is not None and int(rev) != int(idx['revision']):
                return False, 'stale', f"the job was corrected since (it is at version {idx['revision']}; this was for version {rev})"
        if action in ('start', 'job'):
            no = _may_work(life, key, p)
            if no:
                return False, 'permission', no
        if action == 'send' and not (life / 'notify-live').exists():
            return False, 'permission', 'sending is switched off'
        return True, '', ''
    except Exception as e:
        return False, 'unreadable', f'the check could not be made ({type(e).__name__}: {str(e)[:80]})'


def may_start(con, life, key, opp: dict, res: dict) -> bool:
    """worker.run, before a job is claimed and booked."""
    if not on(life):
        return True
    ok, code, why = gate(con, life, key, 'start', revision=(opp.get('commitment') or {}).get('revision') if isinstance(opp.get('commitment'), dict) else None)
    if not ok:
        _hold(life, 'start', key, why, res)
    return ok


_GRANTED_JOB = None             # a job that may carry a step out for real says so in its name (worker.job_name; the job guard's own GRANTED)


def granted_ran(con, life, key, opp: dict, ex: dict, packet: dict, now_dt=None) -> None:
    """worker._harvest_one, with the result of a job that ran a step for real under a standing OK, before that result is
    readied (without this, a later rework could book a SECOND standing-OK job and the approved step would run twice).
    The item says that the step ran, or may have (`granted_done`): the job itself reports it
    done, or the read-back of its tool calls shows it sent or changed something, or that read-back could not be made.
    From then on no second standing-OK job is booked for this item (worker.run) and the job gate refuses one whoever
    booked it (job_gate): one approved step takes effect at most once.
    When only the job's own word says "done" (the read-back saw no change, as with a step done in a browser), that
    word is kept as what it did, so one-send's own record of the step says "carried out" and not "nothing went".
    A job that says it did NOT do the step (it asks the owner, or reports nothing needed) with a clean read-back is not marked."""
    try:
        import worker as W
        if not on(life) or not isinstance(packet, dict) or not W.standing_ok(opp):
            return
        seen = ex.get('did_under_standing_ok') or ex.get('broke_a_limit') or ex.get('audit_skipped')
        if packet.get('status') != 'done' and not seen:
            return
        if not seen:
            ex['did_under_standing_ok'] = ['the job itself reported the step done (the read-back of its tool calls showed no change)']
        opp['granted_done'] = {'attempt_id': ex.get('attempt_id'), 'job': ex.get('job_id'), 'at': time.time(), 'status': packet.get('status')}
        _event(con, key, 'granted_step_ran', {'attempt_id': ex.get('attempt_id'), 'job': ex.get('job_id'), 'status': packet.get('status'), 'read_back': 'saw it' if seen else 'saw no change'}, now_dt)
    except Exception as e:
        _hold(life, 'result', key, f'whether the standing-OK step ran could not be written down ({type(e).__name__})')


def acted_before(opp: dict) -> bool:
    """worker.run, before a standing-OK job is booked: did a standing-OK job of this item already run its step (or may
    it have)? Then no second one is booked, whatever sent the item back (a rework, a retry, a restart)."""
    return isinstance(opp.get('granted_done'), dict)


def _second_act(name, p: dict, attempt) -> bool:
    """job_gate: is this a standing-OK job of an item whose standing-OK step already ran in ANOTHER attempt?"""
    global _GRANTED_JOB
    if not isinstance(p.get('granted_done'), dict) or p['granted_done'].get('attempt_id') == attempt:
        return False
    if _GRANTED_JOB is None:
        import re
        _GRANTED_JOB = re.compile(r'^[a-z][a-z-]*-granted( retry)?:')
    return bool(_GRANTED_JOB.match(str(name or '')))


def save_guard(con) -> str:
    """worker._save: with the switch, the worker's one UPDATE only lands on an item that is still being worked. An item
    the owner dropped or closed while its result was in hand stays as the owner left it (the UPDATE matches nothing, _save raises)."""
    return " AND state IN ('pending','executing')" if on_for(con) else ''


def still_worked(con, key) -> bool:
    """worker.add_followups: is the parent still being worked (not dropped or closed since its result was read)?"""
    try:
        if not on_for(con):
            return True
        row = con.execute('SELECT state,payload FROM opps WHERE key=?', (key,)).fetchone()
        return row is not None and tuple(row)[0] in ('pending', 'executing') and not json.loads(tuple(row)[1]).get('closed')
    except Exception:
        return False


def held_jobs(life) -> set:
    """Job ids the job gate refused at least one tool call of (its own lines in the holds file)."""
    out = set()
    try:
        for line in (Path(life) / HOLDS).read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('action') == 'job' and row.get('job'):
                out.add(str(row['job']))
    except OSError:
        pass
    return out


def refused_run(con, life, key, opp: dict, ex: dict, finished: bool, now_dt, res: dict, audit=None) -> bool:
    """worker._harvest_one: a run the job gate refused a tool call of. -> True = handled here: the run did NO work, so
    what it answered is not a result and it is not a failed run; the item goes back to be started again as a new attempt.
    That is only said when it can be shown: the run has ended and the audit of its session shows NO tool call at all.
    Anything else is False, and the normal harvest takes the run as it would any run:
      it made calls (one refusal in a run that otherwise worked), or its calls cannot be read
      the item has a step that runs for real under a standing OK (the harvest and one-send settle that; never restarted here)
      it has not ended yet
    An item is started again this way at most RESTART_CAP times; after that the owner is told without the prep.
    `audit`: stands in for jobaudit.audit (job id -> {'calls': n, ...})."""
    if not on(life) or not finished or not ex.get('job_id') or str(ex['job_id']) not in held_jobs(life):
        return False
    import worker as W
    if W.standing_ok(opp):
        return False
    try:
        did = (audit or __import__('jobaudit').audit)(ex['job_id'])
        calls = did.get('calls') if isinstance(did, dict) else None
    except Exception:
        calls = None
    if calls != 0:
        return False
    n = int(opp.get('refused_restarts') or 0) + 1
    _event(con, key, 'attempt_refused', {'attempt_id': ex.get('attempt_id'), 'job': ex.get('job_id'), 'restart': n,
                                         'why': 'the job gate held it and its session shows no tool call: it did no work'}, now_dt)
    opp['dispatch_last'] = {'attempt_id': ex.get('attempt_id'), 'outcome': 'refused by the job gate', 'checked_at': now_dt.timestamp()}
    opp['exec'] = {'created_at': ex.get('created_at')} | ({'retry_used': True} if int(ex.get('tries') or 1) >= 2 else {})     # kept, so the day's job count still sees it
    res['refused_runs'] = res.get('refused_runs', 0) + 1
    if n > RESTART_CAP:
        W._ready(con, key, opp, W.synth_packet(opp, 'the background work did not run'), now_dt, res)
        return True
    opp['refused_restarts'] = n
    con.execute("UPDATE opps SET state='pending', payload=?, updated=? WHERE key=? AND state='executing'", (json.dumps(opp, default=str), time.time(), key))
    return True


def may_rebook(con, life, key, opp: dict, ex: dict, rows, gone, audit, res: dict) -> bool:
    """worker._harvest_one, a run that looks failed, lost or cut short, before a second one is booked. With the switch a
    second run is booked only on what could be READ:
      the run records could not be read            -> wait (not knowing is not "never ran")
      no run on record and no word of a drop       -> the scheduler's list must be readable; if not, wait
      it started (failed, stalled, cut short)      -> what it did must be read back; unreadable -> wait
    True = go on as always (the old rules still decide whether a second run is allowed). False = held on this run."""
    if not on(life):
        return True
    why = ''
    if rows is None:
        why = 'the run records could not be read'
    elif not rows and not gone:
        if scheduler_jobs() is None:
            why = 'no run is on record and the scheduler could not be read'
    elif gone != 'never ran':
        try:
            did = audit(ex.get('job_id')) if audit else None
        except Exception:
            did = None
        if did is None:
            why = 'what the earlier run did could not be read back'
    if not why:
        return True
    _hold(life, 'rebook', key, why + '; no second run is booked until it can be', res)
    return False                                      # a hold is not a running job: it takes no place under the worker's cap


def result_held(con, life, key, ex: dict, res: dict) -> bool:
    """worker._harvest_one, with a job's result in hand: may it be used? A result for an older version is left to the
    version fence (commit.stale_result, which sends the item back for its current version) when that switch is on."""
    if not on(life):
        return False
    ok, code, why = gate(con, life, key, 'result', revision=ex.get('commitment_rev'))
    if ok or (code == 'stale' and CM.fence_on(life)):
        return False
    _hold(life, 'result', key, why, res)
    if code == 'stale':
        # The version fence is switched off, so nobody else sends the item back. The result is not used and the item
        # is started again for its current version (as the fence would do); it is not held on every run for ever.
        try:
            row = con.execute("SELECT payload FROM opps WHERE key=? AND state='executing'", (key,)).fetchone()
            if row is not None:
                p = json.loads(tuple(row)[0])
                p['exec'] = {'retry_used': True, 'created_at': ex.get('created_at'), 'stale': {'job_id': ex.get('job_id'), 'for_version': ex.get('commitment_rev')}}
                con.execute("UPDATE opps SET state='pending', payload=?, updated=? WHERE key=? AND state='executing'", (json.dumps(p, default=str), time.time(), key))
                _event(con, key, 'stale_result', {'job_id': ex.get('job_id'), 'for_version': ex.get('commitment_rev')})
                res['stale'] = res.get('stale', 0) + 1
        except Exception as e:
            res.setdefault('errors', []).append(f'stale {str(key)[:40]}: {type(e).__name__}: {str(e)[:80]}')
    return True                                       # a hold is not a running job: it takes no place under the worker's cap


def may_send(con, item_keys, text=None) -> bool:
    """notifier._send, before the note is claimed and sent: every item in it is read again. One item that fails holds
    this note (its words include that item); the next run writes a note without it.
    `text` (optional): the note's words. With them, a note that states a value the owner corrected on a job one of its items
    is linked to is held: the next run writes it again (the one rewrite), and if it says it a second time that item
    is taken out of the notes (it waits, open) so the others go."""
    life = CM._home(con)
    if not on(life):
        return True
    bad = []
    if text:
        for key in item_keys:
            try:
                row = con.execute("SELECT payload FROM opps WHERE key=? AND state='ready'", (key,)).fetchone()
                p = json.loads(tuple(row)[0]) if row else {}
                said = states_superseded(text, p.get('link_corrections')) if p.get('link_corrections') else []
                if not said:
                    continue
                p['stale_notes'] = int(p.get('stale_notes') or 0) + 1
                _hold(life, 'send', key, 'the note states a value the owner corrected: ' + said[0])
                if p['stale_notes'] >= 2:
                    from ownertime import PACIFIC
                    _park(con, key, p, 'moot', 'a note about it stated a value the owner corrected (' + said[0] + ')', datetime.now(PACIFIC))
                else:
                    con.execute("UPDATE opps SET payload=? WHERE key=? AND state='ready'", (json.dumps(p, default=str), key))
                con.commit()
                return False
            except Exception as e:
                _hold(life, 'send', key, f'the corrected-value check could not be made ({type(e).__name__})')
                return False
    for key in item_keys:
        ok, code, why = gate(con, life, key, 'send')
        if ok:
            continue
        bad.append((key, why))
        if code == 'stale':                           # a result for an older version never goes out: back to be worked for the current one
            row = con.execute("SELECT payload FROM opps WHERE key=? AND state='ready'", (key,)).fetchone()
            if row is not None:
                p = json.loads(tuple(row)[0])
                p['stale_packet'] = p.pop('packet', None)
                p['exec'] = {'retry_used': True, 'created_at': (p.get('exec') or {}).get('created_at')}
                con.execute("UPDATE opps SET state='pending', payload=?, updated=? WHERE key=? AND state='ready'", (json.dumps(p, default=str), time.time(), key))
                con.commit()
    for key, why in bad:
        _hold(life, 'send', key, why)
        try:    # still "ready" and still refused (it is waiting for a receipt): moved out of the way, so the next note is written without it
            row = con.execute("SELECT payload FROM opps WHERE key=? AND state='ready'", (key,)).fetchone()
            if row is not None and _parked(json.loads(tuple(row)[0])) is not None:
                from ownertime import PACIFIC
                con.execute("UPDATE opps SET surface_at=? WHERE key=? AND state='ready'", ((datetime.now(PACIFIC) + timedelta(seconds=PARK_FOR)).isoformat(), key))
                con.commit()
        except Exception:
            pass
    return not bad


def job_gate(life, job_id: str, name: str = '') -> dict:
    """The job guard's question, before EVERY tool call of a background job: is this job still the current attempt of
    an open item, at the version it was booked for, with the owner's stop not set and its permission still there?
    -> {'go': bool, 'why': plain words}. A job booked without an attempt mark (before this switch) is not judged here.
    Read-only. A check that cannot be made is a hold."""
    import re
    import sqlite3
    m = re.search(r'\[(a_[0-9a-f]{12})\]\s*$', str(name or ''))
    if not m or not on(life):
        return {'go': True, 'why': ''}
    attempt = m.group(1)
    try:
        con = sqlite3.connect(f"file:{Path(life) / 'ledger.sqlite3'}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT key,payload FROM opps WHERE json_extract(payload,'$.exec.attempt_id')=?", (attempt,)).fetchone()
            if row is None:
                return {'go': False, 'why': 'this job is not the current attempt of any item (the item was stopped, corrected or worked again)', 'code': 'stale'}
            key, p = tuple(row)[0], json.loads(tuple(row)[1])
            booked_job = (p.get('exec') or {}).get('job_id')
            if booked_job and str(booked_job) != str(job_id):
                return {'go': False, 'why': 'another job holds this attempt', 'code': 'stale'}
            ok, code, why = gate(con, life, key, 'job', attempt=attempt)
            if ok and _second_act(name, p, attempt):      # one approved step takes effect at most once, whatever path booked this job
                ok, code, why = False, 'stale', 'the step the owner gave a standing OK for already ran for this item; it is not carried out a second time'
            if ok and (Path(life) / 'cross-channel-fence-on').exists():   # crossfence.py: did the owner say something since this job was booked that bears on it? Without that switch file the module is not even loaded
                code, why = __import__('crossfence').job_hold(life, key, p, name)
                ok = not code
            out = {'go': ok, 'why': why, 'item': key, 'code': code}
        finally:
            con.close()
    except Exception as e:
        out = {'go': False, 'why': f'the check could not be made ({type(e).__name__})', 'code': 'unreadable'}
    if not out['go'] and out.get('code') in ('off', 'unreadable', 'permission'):
        _job_hold(life, job_id, out)                  # the run did no work for a reason that may pass: the harvest starts the item again (refused_run)
    return out


def _job_hold(life, job_id, out: dict) -> None:
    if str(job_id) in held_jobs(life):
        return
    try:
        with open(Path(life) / HOLDS, 'a') as fh:
            fh.write(json.dumps({'at': time.time(), 'action': 'job', 'job': str(job_id), 'key': out.get('item'), 'why': out.get('why')}, default=str) + '\n')
    except OSError:
        pass


# ---- 5. a receipt to close ---------------------------------------------------------------------------------------------------

def _event_over(key: str, p: dict, now_dt: datetime):
    """The one state of the source that stands as a receipt by itself: the event the item was about has passed. Only a
    calendar time counts, never a deadline (a deadline that passes is a thing missed, not a thing settled), and never
    for something the owner asked for in chat. -> that time, or None."""
    t = _moment(p.get('event_start'))
    if t is None or t > now_dt or p.get('deadline') or (p.get('packet') or {}).get('told_instead'):
        return None
    return None if (p.get('_source') == 'conversation' or str(key).startswith('chat:')) else t


_OWN_MEMORY = None


def named_sources(packet: dict) -> list:
    """The places a result says it read. Two things are never a place: the assistant's own memory of something, and the
    item's own text or notice (the thing being judged cannot be the proof that nothing is needed)."""
    global _OWN_MEMORY
    if _OWN_MEMORY is None:
        import re
        _OWN_MEMORY = re.compile(r"\b(?:my|own)\s+(?:memory|recollection|notes?|understanding)\b|^\s*(?:memory|i remember|i recall)\b|\bfrom memory\b"
                                 r"|\b(?:the|this)\s+item(?:'s|\u2019s|s)?\b|\bitem(?:'s|\u2019s)\s+(?:own\s+)?\w+"
                                 r"|\b(?:the|this)\s+(?:notice|alert|email|message|reminder)(?:'s|\u2019s)?\s+(?:own\s+)?(?:text|wording|words)\b"
                                 r"|\b(?:notice|alert)\s+text\b|\b(?:the|this)\s+(?:notice|alert|email|message)\s+itself\b", re.I)
    src = packet.get('sources') if isinstance(packet, dict) else None
    return [str(s)[:160] for s in src if str(s).strip() and not _OWN_MEMORY.search(str(s))][:8] if isinstance(src, list) else []


def moot_receipt(key: str, opp: dict, packet: dict, now_dt: datetime):
    """What, other than the model saying so, stands behind "nothing more is needed"? -> the receipt, or None.
      job_audit      the job ran to its end on the scheduler's own record, the read-back of what it did was made and is
                     clean, AND its result names where it read this (its own memory is not a place). A scheduled
                     follow-up CHECK needs only the first two: running the check is what it was for.
      source_state   the event the item was about has passed (never a deadline; see _event_over)."""
    ex = opp.get('exec') if isinstance(opp.get('exec'), dict) else {}
    audited = bool(ex.get('job_id') and ex.get('done_at') and not ex.get('audit_skipped') and not ex.get('broke_a_limit'))
    read = named_sources(packet)
    check = '#fu' in key or bool(opp.get('followup'))
    if audited and (read or (check and CHECK_CLOSES_ON_AUDIT)):
        return {'kind': 'job_audit', 'job': ex['job_id'], 'at': ex['done_at'], 'sources': read,
                'what': 'the job ran to the end, the read-back of what it did was clean' + (', and its result names where it read this' if read else '; it was a scheduled check')}
    t = _event_over(key, opp, now_dt)
    if t is not None:
        return {'kind': 'source_state', 'what': 'its own time has passed (event start)', 'when': t.isoformat(), 'at': now_dt.timestamp()}
    return None


def _of(p: dict) -> str:
    """What a "waiting for a receipt" mark was made for: the result, and the source as the item last saw it (its newest
    email, its stored version of the mail thread, calendar entry or watched page). New mail, or a changed page, makes
    the mark void."""
    f = p.get('fresh') if isinstance(p.get('fresh'), dict) else {}
    seen = [(f.get(k) or {}).get('v') if isinstance(f.get(k), dict) else None for k in ('seen', 'watch')]
    return hashlib.sha1(json.dumps([p.get('packet'), p.get('latest_email'), seen], sort_keys=True, default=str).encode()).hexdigest()[:12]


def _owed(key: str, opp: dict, now_dt: datetime) -> bool:
    """Is this something the owner would miss if it went quiet: a top-level item the owner asked for in chat, or one with a deadline
    still ahead? (A later check hanging off an item is never one: "nothing to tell" is what a check is for.)"""
    if '#fu' in key or opp.get('followup'):
        return False
    due = _moment(opp.get('deadline'))
    return opp.get('_source') == 'conversation' or str(key).startswith('chat:') or (due is not None and due > now_dt)


def _parked(p: dict):
    """The item's "waiting for a receipt" mark, only while it still belongs to the result the item carries now (a
    correction or a fresh run replaces the result, and the old mark then counts for nothing)."""
    a = p.get('awaiting_receipt')
    return a if isinstance(a, dict) and a.get('of') == _of(p) else None


def _park(con, key, opp: dict, would: str, why, now_dt: datetime, save=None) -> None:
    opp['awaiting_receipt'] = {'would_close': would, 'why': str(why or '')[:300], 'since': now_dt.timestamp(), 'of': _of(opp)}
    if save is not None:
        save()
    else:
        con.execute("UPDATE opps SET payload=?, updated=? WHERE key=? AND state='ready'", (json.dumps(opp, default=str), time.time(), key))
    row = con.execute('SELECT surface_at FROM opps WHERE key=?', (key,)).fetchone()
    opp['awaiting_receipt'].update(due_was=tuple(row)[0] if row else None, pushed_to=(now_dt + timedelta(seconds=PARK_FOR)).isoformat())
    con.execute("UPDATE opps SET payload=?, surface_at=? WHERE key=? AND state='ready'", (json.dumps(opp, default=str), opp['awaiting_receipt']['pushed_to'], key))
    _event(con, key, 'close_held', {'would_close': would, 'why': str(why or '')[:300], 'receipt': None}, now_dt)


def _next_note(now_dt: datetime, opp: dict) -> str:
    """When a line that only rides along goes: at the next daytime moment, as its own line (there is
    no morning note to ride), or now, at routine urgency, when its deadline comes before that."""
    brief = __import__('notifier').next_daytime(now_dt)
    due = _moment(opp.get('deadline'))
    return (now_dt if due is not None and due <= brief else brief).isoformat()


def _next_check(con, key):
    """When the item's next later check is booked for, in plain words ('' when it has none)."""
    row = con.execute("SELECT MIN(surface_at) FROM opps WHERE key LIKE ? AND state IN ('pending','executing','ready')", (str(key) + '#fu%',)).fetchone()
    when = tuple(row)[0] if row else None
    return CM._plain(when) if _moment(when) is not None else ''


def _revision(con, key) -> int:
    idx = CM._index(con, key)
    return int(idx['revision']) if idx else 1


UNCONFIRMED = 'unconfirmed'     # the status of a result whose claim nothing outside the job stands behind: not "done", not "moot", so no reader takes it for finished work


def _unconfirmed_line(con, key, opp: dict, packet: dict, was: str):
    """A claim nothing outside the job stands behind is never handed to the note writer, not as a fact and not as a
    quote. The line is built by code from the ITEM's own facts only: what could not be confirmed (the item's own title),
    its deadline if it has one, that it stays open, and when it is looked at again. The result's status becomes
    "unconfirmed" and its details are dropped. What the job claimed is kept for the record only (told_instead, and the
    history line the callers write). -> (the job's version, the job's own reason)."""
    rev, again = _revision(con, key), _next_check(con, key)
    why = str(packet.get('moot_reason') or packet.get('headline') or 'nothing more is needed').strip()
    what = str(opp.get('title') or key).strip().rstrip('.')[:200]
    line = f'I could not confirm that this is settled: {what}.'
    if _moment(opp.get('deadline')) is not None:
        line += f" Its deadline is {CM._plain(opp['deadline'])}."
    line += f' It stays open; I will check again {again}.' if again else ' It stays open.'
    kept = {'was': was, 'why': why[:300], 'claim': str(packet.get('headline') or '')[:400], 'claim_details': str(packet.get('details') or '')[:900], 'silenced': 0, 'rev': rev}
    packet.update(status=UNCONFIRMED, headline=line, details='', told_instead=kept)
    return rev, why


def unconfirmed_done(con, key, opp: dict, packet: dict, now_dt: datetime, res: dict) -> bool:
    """A result that says "done" about something the owner asked for in chat, or with a deadline still ahead, from a job that
    names no place outside itself (or whose audit is missing or not clean): its line is rebuilt the same way a
    "nothing needed" is (_unconfirmed_line: the item's own facts, never the job's claim), so the note cannot state an
    outcome nobody verified. The item is told at its usual time and stays open afterwards (told_state), as before. -> True only when the owner was already told this for this very
    version of the job: then it is not said again, the item simply stays open, and the caller stops."""
    if packet.get('status') != 'done' or packet.get('told_instead') or not _owed(key, opp, now_dt) or _proven(dict(opp, packet=packet)):
        return False
    was = opp.get('open_until_receipt') if isinstance(opp.get('open_until_receipt'), dict) else {}
    claim = str(packet.get('headline') or '')[:400]
    rev, why = _unconfirmed_line(con, key, opp, packet, 'done')
    again = was.get('rev') == rev and isinstance(opp.get('told'), dict)
    _event(con, key, 'close_held', {'would_close': 'done', 'why': why[:300], 'claim': claim, 'receipt': None, 'told_instead': True, 'already_told': again}, now_dt)
    res['unconfirmed_done'] = res.get('unconfirmed_done', 0) + 1
    if not again:
        return False
    opp['packet'] = packet
    __import__('worker')._save(con, key, 'surfaced', opp)
    res['still_open'] = res.get('still_open', 0) + 1
    return True


def unreceipted(con, key, opp: dict, packet: dict, now_dt: datetime, res: dict) -> bool:
    """worker._ready, a result that says "nothing needed" (moot / nothing_useful). False = close it as always (the
    switch is off, or there is a receipt, which is then written on the item). True = it is NOT closed here:
      something the owner asked for in chat, or with a deadline still ahead   the owner is told ONCE (once per version of the job),
          in one line of its own at the next daytime moment, at routine urgency, built from the item's own facts (never the
          job's claim): what could not be confirmed, its deadline, that it stays open, when it is looked at again. The item STAYS OPEN after that note (told_state): its commitment, deadline,
          steps and later checks are untouched. Being told is recorded and closes nothing.
      anything else   it stays open with its result, out of every note, waiting for a receipt."""
    life = CM._home(con)
    if not on(life):
        return False
    try:
        if ensure(con, life, key, now=now_dt) is not None and 'commitment' not in opp:
            _carry(con, key, opp)
        got, owed = moot_receipt(key, opp, packet, now_dt), _owed(key, opp, now_dt)
        if got and not (owed and OWED_ALWAYS_TOLD and got.get('kind') == 'job_audit'):
            opp['receipt'] = got
            return False
        import worker as W
        why = str(packet.get('moot_reason') or packet.get('headline') or 'nothing more is needed').strip()
        if owed:
            was = opp.get('open_until_receipt') if isinstance(opp.get('open_until_receipt'), dict) else {}
            claim = str(packet.get('headline') or '')[:400]
            rev, why = _unconfirmed_line(con, key, opp, packet, packet.get('status'))
            packet.update(decision=None, followups=[])
            opp['packet'] = packet
            _event(con, key, 'close_held', {'would_close': 'moot', 'why': why[:300], 'claim': claim, 'receipt': None, 'told_instead': True, 'already_told': was.get('rev') == rev}, now_dt)
            if was.get('rev') == rev and isinstance(opp.get('told'), dict):
                # the owner was told this for this very version of the job already: it is not said again; the item simply stays open
                W._save(con, key, 'surfaced', opp)
                res['still_open'] = res.get('still_open', 0) + 1
                return True
            opp['ready_at'] = time.time()
            W._save(con, key, 'ready', opp)
            con.execute("UPDATE opps SET surface_at=?, urgency=CASE WHEN urgency='now' THEN 'today' ELSE urgency END WHERE key=? AND state='ready'", (_next_note(now_dt, opp), key))
            res['ready'] = res.get('ready', 0) + 1
            res['told_instead'] = res.get('told_instead', 0) + 1
            return True
        _park(con, key, opp, 'moot', why, now_dt, save=lambda: W._save(con, key, 'ready', opp))
        res['awaiting_receipt'] = res.get('awaiting_receipt', 0) + 1
        return True
    except CM.Stale:
        raise
    except Exception as e:
        _hold(life, 'close', key, f'the receipt check could not be made ({type(e).__name__}: {str(e)[:80]})', res)
        return True


def silent_held(con, life, key, opp: dict, now_dt: datetime, out: dict) -> bool:
    """notifier.deliver, the note writer chose to say nothing about a finished item: its silence is not a receipt.
    The item waits, open. A line the owner is owed (unreceipted told the owner instead of closing) is offered to the next morning
    note again, twice at most; after that it waits like the others, and no passing deadline ever closes it."""
    if not on(life):
        return False
    try:
        row = con.execute('SELECT * FROM opps WHERE key=?', (key,)).fetchone()
        told = __import__('monitor').already_told_by_code(con, row) if row is not None else ''
    except Exception:
        told = ''
    if told and not opp.get('deadline') and not isinstance((opp.get('packet') or {}).get('told_instead'), dict):
        return False        # a change the monitor's own code text already told the owner. The writer's silence about it closes it; it is not parked and pushed a day at a time. Anything else waits as before
    try:
        ensure(con, life, key, now=now_dt)
        ti = (opp.get('packet') or {}).get('told_instead')
        if isinstance(ti, dict) and int(ti.get('silenced') or 0) < 2:
            ti['silenced'] = int(ti.get('silenced') or 0) + 1
            con.execute("UPDATE opps SET payload=?, surface_at=? WHERE key=? AND state='ready'",
                        (json.dumps(opp, default=str), __import__('notifier').next_daytime(now_dt).isoformat(), key))
            out['silent_again'] = out.get('silent_again', 0) + 1
            return True
        _park(con, key, opp, 'done', 'the note writer chose to say nothing about it', now_dt)
        out['awaiting_receipt'] = out.get('awaiting_receipt', 0) + 1
    except Exception as e:
        _hold(life, 'close', key, f'the receipt check could not be made ({type(e).__name__})')
    return True


def _proven(p: dict) -> bool:
    """Does something other than the telling stand behind this item's result? Yes when telling the owner IS the whole job (a
    reminder, a heads-up with no work behind it), and yes when a job ran to its end with a clean audit and its result
    names a place outside itself where it read this."""
    packet = p.get('packet') if isinstance(p.get('packet'), dict) else {}
    if packet.get('told_instead'):
        return False
    ex = p.get('exec') if isinstance(p.get('exec'), dict) else {}
    if not ex.get('job_id'):
        return True                                   # no job worked on it: the owner was to be told, and the owner was
    return bool(ex.get('done_at') and not ex.get('audit_skipped') and not ex.get('broke_a_limit') and named_sources(packet))


def _stays_open(con, p: dict, state: str) -> str:
    """The owner has provably been told. For most items that is the end ('done'). For something the owner asked for in chat, or with
    a deadline still ahead, whose result nothing outside the job stands behind, it is NOT: the telling is recorded and
    the item stays open (told, nothing asked of the owner), with its commitment, deadline, steps and later checks as they
    were. It closes on real evidence, on the owner's own word, or when its deadline has passed and its later checks have run
    (reconcile_parked)."""
    try:
        key = str(p.get('dedupe_key') or '')
        if state != 'done' or not key:
            return state
        unproven = __import__('outcome').told_unproven(con, CM._home(con), key, p)   # outcome.py (switch own-outcome-on): a send receipt closes nothing that work stood behind; only the job's own record with a place it read outside itself does. None without the switch
        if not unproven and (unproven is not None or not _owed(key, p, CM._now()) or _proven(p)):
            return state
        p['open_until_receipt'] = {'since': time.time(), 'rev': _revision(con, key), 'why': 'the owner was told; nothing outside the job confirms the outcome'}
        return 'surfaced'
    except Exception:
        return state


def told_state(con, p: dict, note_key: str, state: str) -> str:
    """notifier._mark_told: a note that provably went (its send answered "sent", or it was read back) is the receipt
    that the owner was TOLD, written on the item. A live note whose delivery is not known yet leaves the item OPEN and marked
    uncertain; the read-back on a later run settles it (confirmed), or reopens it. And being told does not close an
    obligation whose outcome nothing confirms (_stays_open)."""
    try:
        if not on_for(con):
            return state
        row = con.execute('SELECT mode,state FROM outbox WHERE key=?', (note_key,)).fetchone()
        if row is None or tuple(row)[0] != 'live':
            return state
        if tuple(row)[1] == 'sent':
            p['told']['receipt'] = {'kind': 'send_readback', 'note': note_key}
            p['told'].pop('uncertain', None)
            return _stays_open(con, p, state)
        p['told']['uncertain'] = True
        return 'surfaced'
    except Exception:
        return state


def confirmed(con, note_key: str) -> int:
    """notifier._settle, a note was read back as delivered: its items that were left open and uncertain get the receipt,
    and the ones with nothing to wait for from the owner, and nothing left unconfirmed, are done."""
    if not on_for(con):
        return 0
    n = 0
    for o in con.execute("SELECT key,payload FROM opps WHERE state='surfaced' AND json_extract(payload,'$.told.note')=? AND json_extract(payload,'$.told.uncertain')", (note_key,)).fetchall():
        p = json.loads(o['payload'])
        if tuple(con.execute('SELECT state FROM outbox WHERE key=?', (note_key,)).fetchone() or ('',))[0] != 'sent':
            continue                                  # not before the note itself is on record as having arrived
        p['told'].pop('uncertain', None)
        p['told']['receipt'] = {'kind': 'send_readback', 'note': note_key}
        state = 'surfaced' if (p.get('packet') or {}).get('decision') else _stays_open(con, p, 'done')
        con.execute("UPDATE opps SET state=?, payload=?, updated=? WHERE key=? AND state='surfaced'", (state, json.dumps(p, default=str), time.time(), o['key']))
        n += 1
    return n


def never_assumed(con, life, note_key: str) -> bool:
    """notifier._settle, a note whose delivery still cannot be told either way after the give-up time. With the switch:
    True = it is NOT written off as delivered. It stays uncertain, is read back on every run and is never sent again
    blind; the same repair ticket is raised, once."""
    if not on(life):
        return False
    try:
        CM.ensure_schema(con)
        mark = 'ci_unsettled:' + str(note_key)
        if con.execute('SELECT 1 FROM meta WHERE key=?', (mark,)).fetchone() is None:
            con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (mark, str(time.time())))
            __import__('worker')._alarm(life, note_key, "Zeus's note to the owner: delivery could not be confirmed after 30 minutes (it was not sent again, and it is not counted as delivered)", kind='life_note_unconfirmed')
    except Exception:
        pass
    return True


def reconcile_parked(con, life, now_dt: datetime) -> dict:
    """Every run: an item waiting for a receipt is looked at again.
      the event it was about has passed (a calendar time; never a deadline, and never something the owner asked for in chat)
          that is the state of the source: it closes with that receipt, the way the note's own silent close does. Its
          later checks are NOT closed with it: a check booked for after the event still runs.
      otherwise it stays open, and its place in the queue is kept ahead of now so that no note carries it and nothing
          works it again. The owner's own word in chat (done, drop, told) closes it at any time, as always.
    A mark whose result or source has moved on (the job was corrected, it went back to work, new mail or a changed page
    arrived) is taken off, and the item's own time is put back if it is still the one this file set."""
    out = {}
    marks = ','.join('?' * len(CM.OPEN))
    for r in con.execute(f"SELECT key,state,surface_at,payload FROM opps WHERE state IN ({marks}) AND json_extract(payload,'$.awaiting_receipt') IS NOT NULL", CM.OPEN).fetchall():
        p = json.loads(r['payload'])
        a = _parked(p) if r['state'] == 'ready' else None
        if a is None:
            old = p.pop('awaiting_receipt', None) or {}
            back = old.get('due_was') if r['surface_at'] == old.get('pushed_to') else None
            con.execute('UPDATE opps SET payload=?, surface_at=? WHERE key=? AND state=?', (json.dumps(p, default=str), back or r['surface_at'], r['key'], r['state']))
            out['unmarked'] = out.get('unmarked', 0) + 1
            continue
        t = _event_over(r['key'], p, now_dt)
        if t is not None:
            state = 'done' if a.get('would_close') == 'done' else 'moot'
            why = str(a.get('why') or 'nothing more was needed')
            p.pop('awaiting_receipt', None)
            p['closed'] = {'state': state, 'at': time.time(), 'by': 'integrity', 'why': why,
                           'receipt': {'kind': 'source_state', 'what': 'its own time has passed (event start)', 'when': t.isoformat()}}
            CM.on_close(con, r['key'], p, state, 'integrity', why, {'now': CM._now(now_dt)})     # the commitment ends in the same save
            con.execute("UPDATE opps SET state=?, payload=?, updated=? WHERE key=? AND state='ready'", (state, json.dumps(p, default=str), time.time(), r['key']))
            out['closed_with_receipt'] = out.get('closed_with_receipt', 0) + 1
            continue
        due = _moment(r['surface_at'])
        if due is None or (due - now_dt).total_seconds() < PARK_NEAR:
            a['pushed_to'] = (now_dt + timedelta(seconds=PARK_FOR)).isoformat()
            con.execute("UPDATE opps SET payload=?, surface_at=? WHERE key=? AND state='ready'", (json.dumps(p, default=str), a['pushed_to'], r['key']))
        out['waiting'] = out.get('waiting', 0) + 1
    # Items the owner was told about that stay open because nothing confirms their outcome (_stays_open): one of them ends here
    # only when its deadline has passed AND none of its later checks is still to run. Without a deadline it stays open
    # until real evidence or the owner's own word.
    for r in con.execute("SELECT key,payload FROM opps WHERE state='surfaced' AND json_extract(payload,'$.open_until_receipt') IS NOT NULL").fetchall():
        p = json.loads(r['payload'])
        d = (p.get('packet') or {}).get('decision') or {}
        due = _moment(p.get('deadline'))
        if d.get('ask') or d.get('ready') or due is None or due > now_dt:
            out['open_told'] = out.get('open_told', 0) + 1
            continue
        if con.execute("SELECT 1 FROM opps WHERE key LIKE ? AND state IN ('pending','executing','ready') LIMIT 1", (r['key'] + '#fu%',)).fetchone():
            out['open_told'] = out.get('open_told', 0) + 1
            continue                                  # a later check is still to run: not yet
        why = 'its deadline passed after the owner was told and after its later checks ran'
        p['closed'] = {'state': 'done', 'at': time.time(), 'by': 'integrity', 'why': why,
                       'receipt': {'kind': 'source_state', 'what': why, 'when': due.isoformat(), 'told': (p.get('told') or {}).get('note')}}
        CM.on_close(con, r['key'], p, 'done', 'integrity', why, {'now': CM._now(now_dt)})
        con.execute("UPDATE opps SET state='done', payload=?, updated=? WHERE key=? AND state='surfaced'", (json.dumps(p, default=str), time.time(), r['key']))
        out['closed_after_deadline'] = out.get('closed_after_deadline', 0) + 1
    con.commit()
    return out


def tend(con, life, now_dt: datetime) -> dict:
    """The tick's step (tick.run_phases, before any model call and before fresh.tend). Sends nothing, books nothing."""
    out = {}
    if not on(life):
        return out
    for name, fn in (('commitments', lambda: ensure_all(con, life, now_dt)), ('receipts', lambda: reconcile_parked(con, life, now_dt))):
        try:
            got = fn()
            if got:
                out[name] = got
        except Exception as e:
            if con.in_transaction:
                con.rollback()
            out.setdefault('errors', []).append(f'{name}: {type(e).__name__}: {str(e)[:120]}')
    return out


def main(argv=None) -> int:
    """`python3 -I integrity.py job-gate <life folder> <job id> <job name>`: the job guard's question, asked from outside
    this process. Prints ONE JSON object ({"go": true|false, "why": ...}). Exit 0 whenever the question was answered;
    anything else (and any output that is not that JSON) is to be read by the caller as a hold."""
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) >= 3 and a[0] == 'job-gate':
        print(json.dumps(job_gate(a[1], a[2], ' '.join(a[3:])), default=str))
        return 0
    print(json.dumps({'error': 'usage: integrity.py job-gate <life folder> <job id> <job name>'}))
    return 2


if __name__ == '__main__':
    sys.exit(main())

"""timing: when a merged update moves an item's due time.

Why it exists: when a calendar event behind an item changes (renamed, cancelled, moved) the noticer merges an update
into the existing item. Without this module tick.upsert would change the item's words and urgency but keep the old due
time (the `surface_at` column and, through KEEP_ON_MERGE, the old decision), and the worker, which only takes items due
within 40 minutes, would leave an urgent update waiting.

Three kinds of update, told apart by code:

  detail       new words or facts, nothing urgent, no moved event. The due time stays exactly as it was.
  urgent       the update is urgency "now", or asks for an earlier time than the item's, AND its content really
               differs from what the item last saw (a different version stamp, or the item had none). The due time
               moves EARLIER, to max(now, the asked time). Never later.
  reschedule   a verified explicit move. Only two things count:
                 - source: the same calendar occurrence the item came from now starts at another time. The due time
                   shifts by exactly that difference (earlier or later), so "an hour before" stays an hour before.
                 - chat: the owner moved it themselves (`life move <key> <time> --said "<their words>"`, lifecli.py).
                   Their words are required and kept.
               A reschedule may move the due time later. Nothing else may.

Every move is written on the item, `fresh.timing` (last 20): from, to, kind, why, at, and the source version.
A repeat of the same update finds nothing new (same version, due already moved) and writes nothing.

Concurrency: the merge writes with a compare-and-set on the row's `updated` value (tick.upsert). If another writer
changed the row in between (chat's `life add` / `life move` run in their own process), the merge reads the row again
and is redone, so neither change is lost.

Switch file in the life folder: `merge-timing-on` (always on: the switch is folded in).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

SWITCH = 'merge-timing-on'
LOG_MAX = 20


def on(life) -> bool:
    try:
        return life is not None   # always on: the merge-timing-on switch is folded in
    except Exception:
        return False


def _moment(v, tz=None):
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except ValueError:
        return None
    if t.tzinfo is None and tz is not None:
        t = t.replace(tzinfo=tz)
    return t


def _seen(payload: dict):
    f = payload.get('fresh') if isinstance(payload, dict) else None
    s = f.get('seen') if isinstance(f, dict) else None
    return s if isinstance(s, dict) else None


def _start_of(seen):
    facts = (seen or {}).get('facts') or {}
    if facts.get('all_day'):
        return None
    return facts.get('start')


def classify(old_due: str | None, old_payload: dict, new_seen: dict | None, opp: dict, decision: dict, now_dt: datetime) -> dict:
    """-> {'kind': 'detail'|'urgent'|'reschedule', 'to': iso or None, 'why': str}. 'to' is None when nothing moves.

    old_due      the row's surface_at as stored
    old_payload  the stored item
    new_seen     the version stamp of the incoming observation (fresh.stamp_of), or None when unknown
    opp          the incoming notice
    decision     the policy decision made for the incoming notice (its surface_at is the time it asks for)"""
    tz = now_dt.tzinfo
    due = _moment(old_due, tz)
    old_seen = _seen(old_payload)
    changed = new_seen is None or old_seen is None or str(new_seen.get('v')) != str(old_seen.get('v'))

    # reschedule from the source: the same calendar occurrence moved
    if (old_seen and new_seen and old_seen.get('kind') == 'calendar' and new_seen.get('kind') == 'calendar'
            and old_seen.get('ref') == new_seen.get('ref') and old_seen.get('day') == new_seen.get('day')):
        a, b = _moment(_start_of(old_seen), tz), _moment(_start_of(new_seen), tz)
        if a and b and a != b and due:
            to = due + (b - a)
            return {'kind': 'reschedule', 'to': to.isoformat(), 'why': f'the event itself moved from {a.isoformat()} to {b.isoformat()}'}

    if not changed:
        return {'kind': 'detail', 'to': None, 'why': 'same version as before'}

    asked = _moment((decision or {}).get('surface_at') or opp.get('surface_at'), tz)
    if opp.get('urgency') == 'now':
        target = max(now_dt, min(asked, now_dt) if asked else now_dt)
    elif asked and due and asked < due:
        target = max(now_dt, asked)
    else:
        return {'kind': 'detail', 'to': None, 'why': 'not urgent and no earlier time asked'}
    if due is not None and target >= due:
        return {'kind': 'urgent', 'to': None, 'why': 'already due at or before the urgent time'}
    return {'kind': 'urgent', 'to': target.isoformat(), 'why': 'urgent update with new content' if opp.get('urgency') == 'now' else 'new content asks for an earlier time'}


def log(payload: dict, old_due, move: dict, version=None, by='engine') -> None:
    """Write one timing change on the item (fresh.timing, last LOG_MAX)."""
    f = payload.get('fresh')
    if not isinstance(f, dict):
        f = payload['fresh'] = {}
    entry = {'from': old_due, 'to': move.get('to'), 'kind': move.get('kind'), 'why': move.get('why'), 'at': time.time(), 'by': by}
    if version is not None:
        entry['version'] = version
    if move.get('said'):
        entry['said'] = move['said']
    f['timing'] = ((f.get('timing') or []) + [entry])[-LOG_MAX:]


def apply_to_payload(payload: dict, to: str) -> None:
    """The stored decision follows the new due time, so readers of the payload see the same time as the column."""
    if isinstance(payload.get('decision'), dict):
        payload['decision']['surface_at'] = to


def move_cmd(a: list, db) -> int:
    """`life move <key> <time> --said "<their exact words>"`: the owner moves an item (earlier or later).
    Their words are required and written on the item with the old and new time. Compare-and-set, so a tick writing the
    same row at that moment is never overwritten."""
    import json as _json
    import re
    from contextlib import closing
    from ownertime import PACIFIC
    said, words, i = '', [], 0
    while i < len(a):
        if a[i] == '--said' and i + 1 < len(a):
            said = a[i + 1].strip()
            i += 2
        else:
            words.append(a[i])
            i += 1
    if len(words) != 2 or not said:
        print('Use: life move <key> <time> --said "<your exact words>". Your words are required. Nothing was moved.')
        return 2
    key, raw = words
    now = datetime.now(PACIFIC)
    m = re.fullmatch(r'\+(\d+)\s*([mhd])', raw)
    if m:
        to = now + timedelta(**{{'m': 'minutes', 'h': 'hours', 'd': 'days'}[m.group(2)]: int(m.group(1))})
    else:
        to = _moment(raw, PACIFIC)
        if to is None:
            print(f'I could not read the time "{raw}". Use an ISO time or +2h / +3d. Nothing was moved.')
            return 2
    with closing(db()) as con:
        rows = con.execute("SELECT key FROM opps WHERE (key=? OR key LIKE ?) AND state IN ('pending','executing','ready','surfaced','needs_attention')",
                           (key, key + '%')).fetchall()
        keys = [r['key'] for r in rows]
        if key in keys:
            keys = [key]
        if len(keys) != 1:
            print(('No open item matches ' if not keys else 'More than one open item matches ') + f'"{key}". Nothing was moved.')
            return 2
        for _ in range(3):
            r = con.execute('SELECT * FROM opps WHERE key=?', (keys[0],)).fetchone()
            p = _json.loads(r['payload'])
            move = {'kind': 'reschedule', 'to': to.isoformat(), 'why': 'the owner moved it in chat', 'said': said[:300]}
            log(p, r['surface_at'], move, by='owner')
            apply_to_payload(p, move['to'])
            cur = con.execute('UPDATE opps SET surface_at=?, payload=?, updated=? WHERE key=? AND updated IS ?',
                              (move['to'], _json.dumps(p, default=str), time.time(), r['key'], r['updated']))
            if cur.rowcount == 1:
                __import__('commit').on_move(con, r['key'], r['surface_at'], move['to'], said)   # commit.py (switch commit-on): the item's commitment is corrected to the new time in this same save
                con.commit()
                print(f"Moved to {to.strftime('%a %b %d at %I:%M%p').replace(' 0', ' ')}. Key: {r['key']}")
                return 0
        print('It kept changing while I moved it. Nothing was moved; try again.')
        return 1

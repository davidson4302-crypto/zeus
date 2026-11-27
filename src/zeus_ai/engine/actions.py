"""actions: one record for every prepared outside step, and the only place its state changes.

This answers "was the owner's yes carried out, and only once?" in one place. Everything is written down BEFORE the
effect, so a crash, a second "send" or a timeout never leaves stories that look the same.

An action is the frozen step: who it goes to, from which account, the exact words, a fingerprint of all of that, and
the version of the facts it was built from. It lives in its own table in the engine's ledger (`actions`, one row per
attempt, tied to its item by the item's key). A table and not a field of the item, because the 5-minute run rewrites
an item from a copy it read earlier, and a mark that says "this is being sent" must never be lost to that.

States, and the only moves between them (MOVES). Every move is one guarded write ("from this state only") with its
time and reason, saved BEFORE whatever it describes is attempted:

  prepared     the step is frozen, the owner has not been asked yet
  asked        it was put to them; waiting on their word
  approved     their plain yes is on record (or a rule of theirs that needs no yes), nothing has happened yet
  sending      the effect is being attempted right now. Written before the attempt.
  sent         it went, and the receipt is saved
  did_not_run  it is known that nothing went
  unknown      it may have gone. Never tried again; settled only by reading the destination.
  declined     the owner said no
  replaced     the owner changed it, or the thing itself changed: a fresh step takes its place
  expired      the thing's own deadline passed, or its item closed, before the owner answered

The fence: an identical step (same fingerprint) that was sent hands back its saved receipt, and one that is asked,
approved, being sent or unknown is refused, each with a plain reason. Start-up sweep: a "sending" whose process is
gone, or that is older than its own patience, becomes "unknown". Nothing here sends, reads mail or talks to anyone.

Switch file `one-send-on` in the life folder (on()). Without it nobody calls into this module and the table is not
even created.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

SWITCH = 'one-send-on'
STATES = ('prepared', 'asked', 'approved', 'sending', 'sent', 'did_not_run', 'unknown', 'declined', 'replaced', 'expired')
IN_PLAY = ('asked', 'approved', 'sending', 'unknown')          # an identical step in one of these is refused by the fence
WAITING = ('prepared', 'asked')                               # nothing has been decided yet
ENDED = ('sent', 'did_not_run', 'declined', 'replaced', 'expired')
MOVES = {'prepared': ('asked', 'approved', 'declined', 'replaced', 'expired'),
         'asked': ('approved', 'declined', 'replaced', 'expired'),
         'approved': ('sending', 'did_not_run', 'declined', 'replaced', 'expired'),
         'sending': ('sent', 'did_not_run', 'unknown'),
         'unknown': ('sent', 'did_not_run')}
TABLE = """
CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, item TEXT NOT NULL, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
  state TEXT NOT NULL, details TEXT NOT NULL, facts TEXT, receipt TEXT, history TEXT NOT NULL, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS actions_by_item ON actions(item);
CREATE INDEX IF NOT EXISTS actions_by_fingerprint ON actions(fingerprint);
"""
COLUMNS = 'id,item,kind,fingerprint,state,details,facts,receipt,history,created,updated'


def home_of(con) -> Path:
    """The life folder this ledger lives in. The switch is read from there, so a test ledger in a temp folder and
    the live ledger can never share a switch."""
    for row in con.execute('PRAGMA database_list'):
        if row[1] == 'main' and row[2]:
            return Path(row[2]).parent
    raise RuntimeError('this ledger has no file')


def on(life_home) -> bool:
    """Switch file `one-send-on`. Without it everything this build adds stays off."""
    try:
        return (Path(life_home) / SWITCH).exists()
    except (OSError, TypeError):
        return False


def on_for(con) -> bool:
    try:
        return on(home_of(con))
    except Exception:
        return False


def ready(con):
    con.executescript(TABLE)


def _row(r) -> dict:
    if r is None:
        return None
    a = dict(zip(COLUMNS.split(','), r))
    for k in ('details', 'receipt', 'history'):
        try:
            a[k] = json.loads(a[k]) if a[k] else ([] if k == 'history' else None)
        except ValueError:
            a[k] = [] if k == 'history' else None
    return a


def fingerprint(kind: str, item: str, details: dict) -> str:
    """The exact thing the owner's yes is bound to: who, which account, the words. An email reply is pinned by its thread and
    the message it answers, so the same reply stored under two items has ONE fingerprint and can only go once.
    Any other step is pinned to its own item: next month's refill is a new step, not a repeat of this month's."""
    if kind == 'email_reply':
        canon = {k: details.get(k) for k in ('mailbox', 'thread_id', 'reply_to', 'to', 'cc', 'body')}
    else:
        canon = {'item': str(item).split('#')[0], 'ask': details.get('ask'), 'words': details.get('words'), 'who': details.get('who')}
    return hashlib.sha256(json.dumps([kind, canon], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def new(con, item: str, kind: str, details: dict, facts: str = '', *, why: str = 'prepared', now=None) -> dict:
    """Freeze one step. -> the record, in state 'prepared'."""
    now = time.time() if now is None else now
    n = con.execute('SELECT COUNT(*) FROM actions WHERE item=?', (item,)).fetchone()[0] + 1
    rec_id = f'{item}@{n}'
    fp = fingerprint(kind, item, details)
    history = [{'state': 'prepared', 'at': now, 'why': why}]
    con.execute(f'INSERT INTO actions({COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (rec_id, item, kind, fp, 'prepared', json.dumps(details, ensure_ascii=False, default=str), facts or '', None,
                 json.dumps(history, default=str), now, now))
    con.commit()
    return get(con, rec_id)


def get(con, rec_id: str) -> dict:
    return _row(con.execute(f'SELECT {COLUMNS} FROM actions WHERE id=?', (rec_id,)).fetchone())


def latest(con, item: str, kind: str | None = None) -> dict:
    """The newest record of an item (records are numbered in the order they were frozen)."""
    rows = con.execute(f'SELECT {COLUMNS} FROM actions WHERE item=?' + (' AND kind=?' if kind else '') + ' ORDER BY created DESC, rowid DESC LIMIT 1',
                       (item, kind) if kind else (item,)).fetchall()
    return _row(rows[0]) if rows else None


def of_item(con, item: str) -> list:
    return [_row(r) for r in con.execute(f'SELECT {COLUMNS} FROM actions WHERE item=? ORDER BY created, rowid', (item,))]


def in_states(con, states) -> list:
    marks = ','.join('?' * len(states))
    return [_row(r) for r in con.execute(f'SELECT {COLUMNS} FROM actions WHERE state IN ({marks}) ORDER BY created, rowid', tuple(states))]


def move(con, rec_id: str, to: str, why: str = '', *, receipt=None, now=None, **more) -> bool:
    """One guarded move, saved at once: it only happens if the record is still in the state it was read in, so two
    runs can never both win. -> True when this call made the move. `more` is kept with the move (their words, the
    message they came from, which process is sending)."""
    now = time.time() if now is None else now
    rec = get(con, rec_id)
    if rec is None or to not in MOVES.get(rec['state'], ()):
        return False
    step = {'state': to, 'at': now, 'why': str(why)[:400]} | {k: v for k, v in more.items() if v is not None}
    history = rec['history'] + [step]
    cur = con.execute('UPDATE actions SET state=?, history=?, updated=?' + (', receipt=?' if receipt is not None else '') + ' WHERE id=? AND state=?',
                      (to, json.dumps(history, default=str), now) + ((json.dumps(receipt, ensure_ascii=False, default=str),) if receipt is not None else ())
                      + (rec_id, rec['state']))
    con.commit()
    if to == 'approved' and cur.rowcount == 1:
        __import__('authority').approved(con, rec_id)   # authority.py: where this yes comes from, written on this step and its job
    return cur.rowcount == 1


def note(con, rec_id: str, what: str, *, now=None, **more) -> bool:
    """Write something down on a record without changing its state (a readback, how their reply was read)."""
    now = time.time() if now is None else now
    rec = get(con, rec_id)
    if rec is None:
        return False
    history = rec['history'] + [{'note': str(what)[:400], 'at': now} | {k: v for k, v in more.items() if v is not None}]
    cur = con.execute('UPDATE actions SET history=?, updated=? WHERE id=? AND state=?', (json.dumps(history, default=str), now, rec_id, rec['state']))
    con.commit()
    return cur.rowcount == 1


def last(rec: dict, state: str) -> dict:
    """The newest move into `state` on this record ({} when it never was there)."""
    return next((h for h in reversed(rec.get('history') or []) if h.get('state') == state), {})


def notes(rec: dict, what: str) -> list:
    return [h for h in rec.get('history') or [] if h.get('note') == what]


def fence(con, rec: dict):
    """Is an identical step already on record somewhere else? -> ('clear', None) | ('sent', that record: hand back
    its receipt, do not do it again) | ('held', that record: asked, approved, being sent or unknown, so refuse)."""
    others = [_row(r) for r in con.execute(f'SELECT {COLUMNS} FROM actions WHERE fingerprint=? AND id!=? ORDER BY created, rowid',
                                           (rec['fingerprint'], rec['id']))]
    went = next((o for o in others if o['state'] == 'sent'), None)
    if went:
        return 'sent', went
    held = next((o for o in others if o['state'] in IN_PLAY), None)
    return ('held', held) if held else ('clear', None)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def sweep(con, now=None, alive=_alive) -> list:
    """Start-up sweep. A record still "sending" is only believed while the process that wrote it is alive and inside
    its own patience (how long that kind of attempt can honestly take). Otherwise nobody is left who knows how it
    ended: it becomes "unknown", which is never tried again. -> the ids that were moved."""
    now = time.time() if now is None else now
    moved = []
    for rec in in_states(con, ('sending',)):
        mark = last(rec, 'sending')
        patience = mark.get('patience')
        if patience is None:
            continue                                  # its owner settles it (a booked background job: the harvest step)
        age = now - float(mark.get('at') or now)
        gone = mark.get('pid') is not None and not alive(mark.get('pid'))
        if gone or age > float(patience):
            why = 'the process that was sending stopped before it could say how it ended' if gone else \
                'no word came back within %d minutes' % max(1, round(float(patience) / 60))
            if move(con, rec['id'], 'unknown', why, now=now):
                moved.append(rec['id'])
    return moved


def clock(ts) -> str:
    """'3:41pm' today, 'Tue Nov 10 at 3:41pm' otherwise. For sentences the owner may read."""
    try:
        t = datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return 'an unknown time'
    hour = t.strftime('%I:%M%p').lstrip('0').lower()
    return hour if t.date() == datetime.now().date() else t.strftime('%a %b %d at ').replace(' 0', ' ') + hour

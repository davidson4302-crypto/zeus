"""memsync: is Zeus's memory outbox moving?

Zeus's memory provider writes every turn into a small outbox (<profile>/unified-memory/outbox.sqlite3) and sends it
on to the memory service. Without a watch that outbox could sit full, or with entries that never go through, while
every tick still looked healthy.

This file only READS (the outbox is opened read-only, and its layout is discovered, not assumed):
  pending                     entries not yet acknowledged, and how old the oldest is
  acknowledged                entries the memory service took (its receipts)
  last_progress_at            when a run of this engine last SAW the outbox move: an entry acknowledged, or the
                              waiting list shorter. The outbox keeps no time for an acknowledgement, so this is
                              measured from one health note to the next and never guessed.
  failed attempts             the provider's own ledger of gaps (gaps.jsonl beside the outbox: a reason and a time per
                              line, no content): how many in the last hour, and why
  uncertain                   turns whose outcome the provider never learned. They are REPORTED only. Nothing here
                              replays anything: an uncertain write has to be reconciled before any replay, and that
                              is not this file's job.

The one rule that raises a repair ticket (kind zeus_memory_sync_stuck, through the engine's own alarm path):
  an actionable queue (something is pending) that has made no progress for 30 minutes AND whose oldest pending entry
  is over an hour old, counted from the first turn that could have sent it. The provider only sends from inside a
  turn, so entries queued after the last turn with no turn since are idle (`waiting_for_next_turn`), never stuck.
  An outbox with no turn times keeps the plain rule.
  Only a turn the provider took up (bound 1) counts: a turn recorded with bound 0 ran where the sender never loaded
  (a one-shot `--ignore-rules` probe) and could not send anything.
One more case is the same ticket, because it is the same outage seen from the other side: the provider's own ledger
shows it REFUSING new entries (its outbox is at its size limit) for the last 30 minutes while nothing new was taken in
for over an hour. Then nothing is pending, so the first rule alone would stay silent while memory takes in nothing.
(ALARM_WHEN_CAPTURE_BLOCKED turns that second case off.)
File size alone is never an alarm: a big outbox that is moving is healthy.

ONE ticket per outage: the note remembers that it was raised and stays quiet until the outbox moves again. Like the
other health tickets: daytime only (outside quiet hours), never with `off` present, only with the switch file health-alarms-on
(without it the note says the ticket is held), and a ticket the alarm handler did not take is tried again on the next run.

Switch file `claims-on` in the life folder (the memory switch). Without it health.json has no memory_sync
part. The health note also lists these two switches (claims-on, authority-on) under `switches` while their
files are there; with neither, the note is exactly what it was.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import settings

SWITCH = 'claims-on'
LANE_SWITCHES = ('claims-on', 'authority-on')      # the two switches listed in the health note while present
KIND = 'zeus_memory_sync_stuck'
CONDITION = 'memory_sync_stuck'
LIVE_HOME = Path(settings.get('paths.life_home', str(settings.PROFILE_DIR / 'workspace' / 'life-engine')))   # the live life folder (not the LIFE_HOME override)
PACIFIC = ZoneInfo(settings.OWNER_TZ)   # the owner's home zone (name kept for the code below)
NO_PROGRESS = 30 * 60           # the queue has not moved for this long
OLDEST_PENDING = 60 * 60        # and its oldest entry has waited this long
REFUSED_WINDOW = 30 * 60        # the provider refused new entries within this long
REFUSED_AT_LEAST = 3            # this many times (one refusal is a blip)
NOTHING_NEW = 60 * 60           # and nothing new was taken in for this long
DONE_STATES = ('acknowledged',)
REFUSALS = ('outbox_quota_reached',)       # reasons in the provider's ledger that mean "a new entry was turned away"
UNCERTAIN_TURNS = ('unknown', 'interrupted')
GAPS_TAIL = 512 * 1024          # the ledger is read from its end: the last hour is all that is counted
DAY_STARTS, DAY_ENDS = settings.QUIET_END, settings.QUIET_START    # the same hours lifehealth.py raises tickets in
ALARM_WHEN_CAPTURE_BLOCKED = True
CARRIED = ('acknowledged', 'pending', 'watching_since', 'last_progress_at', 'stuck_since', 'alarm_raised_at')   # kept across a run that could not read the outbox


def on(life) -> bool:
    try:
        return life is not None and (Path(life) / SWITCH).exists()
    except (OSError, TypeError):
        return False


def outbox_path(life) -> Path:
    """<profile>/unified-memory/outbox.sqlite3 (the life folder is <profile>/workspace/life-engine). A sandbox may name
    a pretend one with LIFE_MEMSYNC_DB; the live folder always reads the real one."""
    life = Path(life)
    try:
        live = life.resolve() == LIVE_HOME.resolve()
    except OSError:
        live = False
    named = os.environ.get('LIFE_MEMSYNC_DB')
    if named and not live:
        return Path(named)
    return life.parent.parent / 'unified-memory' / 'outbox.sqlite3'


def _iso(ts) -> str | None:
    try:
        return datetime.fromtimestamp(float(ts), PACIFIC).isoformat(timespec='seconds') if ts else None
    except (TypeError, ValueError, OSError):
        return None


def _ts(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=PACIFIC)).timestamp()


def _pick(cols: list, *names):
    return next((n for n in names if n in cols), None)


def gaps(path: Path, now: float) -> dict:
    """The provider's ledger of gaps, last hour only: how many, why, and how many of them were a new entry turned
    away within the last 30 minutes. No file is "no ledger", not "no failures"."""
    out = {'readable': False}
    try:
        size = path.stat().st_size
        with open(path, 'rb') as fh:
            fh.seek(max(0, size - GAPS_TAIL))
            tail = fh.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return out | {'why': 'there is no ledger of failed attempts beside the outbox'}
    if size > GAPS_TAIL:
        tail = tail[1:]                                  # the first line of a tail may be cut in half
    hour, refused, reasons, last_refused = 0, 0, {}, None
    for line in tail:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        at = _ts(row.get('observedAt')) if isinstance(row, dict) else None
        if at is None or now - at > 3600:
            continue
        reason = str(row.get('reason') or 'unknown')[:60]
        hour += 1
        reasons[reason] = reasons.get(reason, 0) + 1
        if reason in REFUSALS:
            last_refused = max(last_refused or 0, at)
            if now - at <= REFUSED_WINDOW:
                refused += 1
    return {'readable': True, 'last_hour': hour, 'reasons': dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:6]),
            'refused_last_30_minutes': refused, 'last_refused_at': _iso(last_refused)}


def measure(path, now: float | None = None) -> dict:
    """What the outbox says right now. Read-only. -> {'readable': False, 'why'} when it cannot be read: that is never
    reported as an empty queue."""
    path, now = Path(path), float(now or time.time())
    if not path.exists():
        return {'readable': False, 'why': 'there is no outbox file'}
    try:
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2)) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'events' not in tables:
                return {'readable': False, 'why': 'the outbox has no list of entries (its layout is not the one this was built for)'}
            cols = [r[1] for r in con.execute('PRAGMA table_info(events)')]
            state, made = _pick(cols, 'state', 'status'), _pick(cols, 'created', 'created_at', 'at')
            if not state or not made:
                return {'readable': False, 'why': 'the outbox entries carry no state or no time (its layout is not the one this was built for)'}
            by_state = {str(s): (int(n), lo, hi) for s, n, lo, hi in con.execute(f'SELECT {state}, COUNT(*), MIN({made}), MAX({made}) FROM events GROUP BY {state}')}
            turns, tmade, sender_saw = {}, None, ''
            if 'turns' in tables:
                tcols = [r[1] for r in con.execute('PRAGMA table_info(turns)')]
                tstate, tmade = _pick(tcols, 'status', 'state'), _pick(tcols, 'created', 'created_at', 'at')
                if tstate:
                    turns = {str(s): int(n) for s, n in con.execute(f'SELECT {tstate}, COUNT(*) FROM turns GROUP BY {tstate}')}
                # `bound` is set when the provider (the part that sends) took the turn up. A turn recorded with
                # bound 0 ran in a process where the sender never loaded (a one-shot `--ignore-rules` probe, for
                # one): only the observer wrote its rows, and nothing in that run could send anything.
                # Such probes are never counted as send chances.
                # An outbox without the column keeps counting every turn.
                sender_saw = ' AND bound = 1' if 'bound' in tcols else ''
            # The provider only sends from inside a turn it took up: the first such turn that started after the oldest
            # pending entry is the first chance it had to send it. None while no such turn exists (idle, not stuck).
            waiting_lo = [v[1] for s, v in by_state.items() if s not in DONE_STATES and _ts(v[1])]
            first_chance = None
            if tmade and waiting_lo:
                lo = min(waiting_lo, key=_ts)
                first_chance = con.execute(f'SELECT MIN({tmade}) FROM turns WHERE {tmade} > ?{sender_saw}', (lo,)).fetchone()[0]
            last_turn = con.execute(f'SELECT MAX({tmade}) FROM turns WHERE 1=1{sender_saw}').fetchone()[0] if tmade else None
            receipts = None
            if 'receipt' in cols:
                receipts = int(con.execute(f"SELECT COUNT(*) FROM events WHERE receipt IS NOT NULL AND receipt != ''").fetchone()[0])
    except sqlite3.Error as e:
        return {'readable': False, 'why': f'the outbox could not be read ({type(e).__name__}: {str(e)[:80]})'}
    waiting = {s: v for s, v in by_state.items() if s not in DONE_STATES}
    pending = sum(v[0] for v in waiting.values())
    oldest = min((_ts(v[1]) for v in waiting.values() if _ts(v[1])), default=None)
    newest = max((_ts(v[2]) for v in by_state.values() if _ts(v[2])), default=None)
    try:
        size = sum(p.stat().st_size for p in path.parent.glob(path.name + '*'))
    except OSError:
        size = None
    return {'readable': True, 'pending': pending, 'pending_by_state': {s: v[0] for s, v in waiting.items()},
            'oldest_pending_at': _iso(oldest), 'oldest_pending_age_seconds': round(now - oldest) if oldest else None,
            'acknowledged': sum(by_state.get(s, (0,))[0] for s in DONE_STATES), 'receipts': receipts,
            'last_captured_at': _iso(newest), 'last_captured_age_seconds': round(now - newest) if newest else None,
            'uncertain_turns': {s: turns[s] for s in UNCERTAIN_TURNS if turns.get(s)},
            'turns_known': bool(tmade), 'last_turn_at': _iso(_ts(last_turn)),
            'first_send_chance_at': _iso(_ts(first_chance)),
            'uncertain_note': 'reported only: an uncertain write is reconciled before any replay, and nothing here replays',
            'size_bytes': size, 'size_note': 'size alone is never an alarm',
            'failed_attempts': gaps(path.parent / 'gaps.jsonl', now)}


def judge(part: dict, before: dict | None, now: float) -> dict:
    """Add what only two notes in a row can tell: when the outbox last moved, and whether it is stuck. `before` is the
    memory_sync part of the previous health note (or None on the first run: then the clock starts now, so the first
    ticket can never come from a guess about what happened before this engine was watching)."""
    before = before if isinstance(before, dict) else {}
    before = before if before.get('readable') else before.get('carried') if isinstance(before.get('carried'), dict) else {}
    if not part.get('readable'):
        # Not readable is not "stuck" and not "fine": what was last known is carried to the next readable run, nothing is concluded.
        keep = {k: before[k] for k in CARRIED if before.get(k) is not None}
        return part | ({'carried': keep} if keep else {})
    moved = bool(before) and (part['acknowledged'] > int(before.get('acknowledged') or 0) or part['pending'] < int(before.get('pending') or 0))
    part['watching_since'] = before.get('watching_since') or _iso(now)
    part['last_progress_at'] = _iso(now) if moved else before.get('last_progress_at')
    since = _ts(part['last_progress_at']) or _ts(part['watching_since']) or now
    # The provider sends only from inside a turn. Entries queued after the last turn's send (its outcome and receipt)
    # wait for the next turn: with no turn since, nothing has tried to send them, so that is idle, not stuck
    # The no-progress clock
    # starts at the first turn that could have sent them. When the outbox keeps no turn times, this cannot be told
    # apart, and the old rule stands (unknown is never read as fine).
    idle = bool(part['pending']) and bool(part.get('turns_known')) and not part.get('first_send_chance_at')
    part['waiting_for_next_turn'] = idle
    if part['pending'] and part.get('turns_known') and part.get('first_send_chance_at'):
        since = max(since, _ts(part['first_send_chance_at']) or since)
    part['no_progress_seconds'] = round(now - since) if part['pending'] and not idle else None
    queue_stuck = bool(part['pending']) and not idle and now - since >= NO_PROGRESS and (part.get('oldest_pending_age_seconds') or 0) > OLDEST_PENDING
    failed = part.get('failed_attempts') or {}
    blocked = bool(failed.get('readable')) and int(failed.get('refused_last_30_minutes') or 0) >= REFUSED_AT_LEAST \
        and (part.get('last_captured_age_seconds') is None or part['last_captured_age_seconds'] > NOTHING_NEW)
    part['capture_blocked'] = blocked
    part['stuck'] = queue_stuck or (blocked and ALARM_WHEN_CAPTURE_BLOCKED)
    part['stuck_why'] = ('queue' if queue_stuck else 'capture refused') if part['stuck'] else None
    part['stuck_since'] = (before.get('stuck_since') or _iso(now)) if part['stuck'] else None
    if part['stuck'] and before.get('alarm_raised_at'):
        part['alarm_raised_at'] = before['alarm_raised_at']             # the same outage: its one ticket is already out
    return part


def ticket(part: dict) -> dict:
    """The one ticket for a stuck outbox. If the alarm handler has to ask the owner, it reads `title` word for word."""
    failed = part.get('failed_attempts') or {}
    if part.get('stuck_why') == 'queue':
        title = (f"Zeus's memory sync is stuck: {part['pending']} saved item{'s' if part['pending'] != 1 else ''} "
                 'waiting over an hour to sync, and none has gone through in 30 minutes')
    else:
        title = "Zeus's memory sync has stopped taking in anything new: its outbox is full and has turned away every new entry for over an hour"
    detail = [f"pending {part.get('pending')} (oldest since {part.get('oldest_pending_at')}); acknowledged {part.get('acknowledged')}; last progress seen {part.get('last_progress_at') or 'none since ' + str(part.get('watching_since'))}",
              f"last new entry taken in {part.get('last_captured_at')}; turned away in the last 30 minutes: {failed.get('refused_last_30_minutes')}; failed attempts in the last hour: {failed.get('last_hour')} {failed.get('reasons') or ''}",
              f"uncertain turns (not replayed): {part.get('uncertain_turns') or 'none'}; outbox size {part.get('size_bytes')} bytes (size is not the reason for this ticket)",
              'Do not replay the outbox: uncertain writes are reconciled first.']
    return {'condition': CONDITION, 'kind': KIND, 'title': title, 'detail': detail}


def health_part(life, note: dict, prev, now_dt, alarm=None, alarms_on: bool = False, path=None) -> None:
    """tick.health_note's call. Only with the switch claims-on: health.json gains a `memory_sync` part, and a stuck
    outbox raises its ONE ticket. Without the switch the note is exactly what it was. Never raises."""
    try:
        if not isinstance(note, dict):
            return
        for name in LANE_SWITCHES:          # listed only while its file is there (as lifehealth.LISTED_WHEN_ON does): a note with neither is what it was
            if (Path(life) / name).exists() and isinstance(note.get('switches'), dict):
                note['switches'][name] = True
        if not on(life):
            return
        now = now_dt.timestamp()
        before = (prev or {}).get('memory_sync') if isinstance(prev, dict) else None
        part = judge(measure(path or outbox_path(life), now), before, now)
        note['memory_sync'] = part
        if not part.get('readable') or not part.get('stuck') or part.get('alarm_raised_at'):
            return
        if note.get('off') or not (DAY_STARTS <= now_dt.astimezone(PACIFIC).hour < DAY_ENDS):
            part['alarm'] = 'waiting: tickets are raised outside quiet hours, and never while the off switch is set'
            return
        if not alarms_on:
            part['alarm'] = 'held: the switch file health-alarms-on is absent'
            held = note.setdefault('alarms_held', [])
            if CONDITION not in held:
                held.append(CONDITION)
            return
        t = ticket(part)
        try:
            took = alarm(life, 'health:' + t['condition'], t['title'], kind=t['kind'], detail=t['detail']) if alarm else None
        except Exception as e:
            took = False
            note.setdefault('alarm_errors', []).append(f'{CONDITION}: {type(e).__name__}')
        if took is False:
            note.setdefault('alarm_errors', []).append(f'{CONDITION}: the alarm handler did not take it')
            part['alarm'] = 'not taken: tried again on the next run'
        else:
            part['alarm_raised_at'] = _iso(now)
            part['alarm'] = 'raised'
    except Exception as e:
        try:
            note['memory_sync_error'] = f'{type(e).__name__}: {str(e)[:120]}'
        except Exception:
            pass

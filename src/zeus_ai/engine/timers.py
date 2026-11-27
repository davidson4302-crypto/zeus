"""timers: one list of every timer, and an honest "how late and why".

Why: Zeus's timers live in three stores (the profile's scheduled jobs, the open items of the life ledger with their
times, and the page watches). Looking in only one of them can wrongly show "no watch exists", and a reminder or a
check that reaches the owner late should say so.

Switch file `timer-truth-on` in the life folder. Without it nothing here is imported: no command, no line in the
status answer, no fact in any note, and every prompt is byte for byte what it was.

PART A, the one list (reading only; nothing here writes to the three stores)
  rows(life, jobs_file, now_dt)     every running timer as one row, in this shape:
        {'source': 'schedule'|'follow-up'|'watch'|'order', 'id', 'what' (plain words), 'next', 'last',
         'last_result', 'late_s' (0 when not late), 'late_why'}
  collect(...)                      the rows plus, per store, what happened to EVERY record in it:
        listed        it is a running timer and has a row
        folded        it is the same timer as another row (the one-time job that is working an item right now)
        not_running   it is not a live timer, with the reason (a finished one-time job, a paused job, a closed item,
                      an item the owner was already told about and that waits on their answer, a parked item, an ended watch)
        unrecognized  a record this code does not understand. That is never silent: the check fails.
  check(...)                        reads each store again by its own short path and compares, record by record and
                                    field by field. Missing, doubled, misfiled or changed = not ok.
  What counts as running:
        schedule   a job that is enabled, scheduled and not paused
        follow-up  an open item that is still owed: noticed, being worked, worked and not told yet, or failed
        watch      a page watch in state "watching"; and a monitor saved on an item to START later (commit.py)
        order      rows another module hands over (orderwatch.timer_rows), only while its own switch is on
  `life timers`, `life timers --json`, `life timers --check` (lifecli.py), and one line for the "is everything
  working?" answer (status_line).

PART B, the late line
  A timed thing is late when it reaches the owner, or runs, more than 15 minutes after its due time. Then the text that
  carries it says how late and why, in one short clause. Code works out the why from evidence:
        quiet_hours     the time fell in quiet hours (on the owner's clock) and it went at the first allowed time
        engine_down     no run of the life engine for 15 minutes or more between due and send (the `runs` table)
        busy            runs happened; the work, or the note, took long (the item's own work record)
        could_not_read  what it depends on could not be read (fresh.py's mark, a watch's failed reads)
        signin          a sign-in had lapsed (the item's sign-in hold, signins.json)
        unknown         none of the above can be shown. Said plainly.
  The reason is the first thing that held it up for 15 minutes or more, walking forward from the due time; what
  held it longest after that is named second ("then quiet hours held it until morning").
  A thing has a stated time when it is a follow-up check or an owed reminder (its own "when"), or something the owner asked
  for in chat for a later time. Routine items with no promised time have none: no clause, ever.
  late_fact(con, life, key_or_row, now_dt)  -> {'late_s', 'why', 'due', 'clause_hint', ...} or None
  composing(...)        notifier.deliver's writer for this note: each late item is handed one fact ("late") and the
                        writer one sentence of instruction; the writer says it, code checks that it did (one
                        targeted fix through the composer's own fix step). Nothing is appended, and no text is ever
                        sent only to say something was late.
  late_read(...)        watches.run: a page read that ran late and found a change keeps that with the change, so the
                        observation, and later the note, carry it.

Never sends. Own guard: every hook here swallows its own trouble; a crash in this file never stops a tick or a turn.
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import settings

SWITCH = 'timer-truth-on'
ORDER_MODULES = (('orderwatch', 'order-watch-on'),)      # other modules that feed rows: (module, its switch file)
ORDER_MODULES += (('refundwatch', 'refund-watch-on'),)   # refundwatch.timer_rows, only while its own switch file is there
LATE_AFTER = 15 * 60          # "late" = more than this after the due time
RUN_GAP = 15 * 60             # the engine runs every 5 minutes; no finished run for this long = it was not running
THEN_AFTER = 30 * 60          # a second hold-up is named only when it lasted at least this long
from ownertime import alert_open
HOME = ZoneInfo(settings.OWNER_TZ)
RUNNING_STATES = ('pending', 'executing', 'ready', 'needs_attention')
WAITING_STATES = ('surfaced',)
CLOSED_STATES = ('done', 'moot', 'declined', 'skipped')
HELD_STATE = 'held'
WHYS = ('signin', 'could_not_read', 'quiet_hours', 'engine_down', 'busy', 'unknown')
SOURCES = ('schedule', 'follow-up', 'watch', 'order')
ROW_KEYS = ('source', 'id', 'what', 'next', 'last', 'last_result', 'late_s', 'late_why')
STATE_WORDS = {'pending': 'not worked yet', 'executing': 'being worked right now', 'ready': 'worked, the owner has not been told yet',
               'needs_attention': 'the work on it failed'}
NOT_RUNNING_WORDS = {'done': 'finished', 'moot': 'finished: no longer needed', 'declined': 'finished: the owner passed on it', 'skipped': 'finished: skipped',
                     'surfaced': 'the owner was told; it waits on their answer', 'held': 'parked: nothing is working it'}

# Plain names for the jobs Zeus runs on its own (the same names the status answer uses), by the job's own name.
JOB_NAMES = (
    (re.compile(r'life pipes tick', re.I), "Reading the owner's email and calendar for things to handle"),
    (re.compile(r'purchase (?:settle|lock)', re.I), "Making sure nothing is bought without the owner's yes"),
    (re.compile(r'phone sign-?in links', re.I), 'Texting the owner a sign-in link when a store signs you out'),
    (re.compile(r'work maintenance', re.I), 'Tidying your own to-do list'),
    (re.compile(r'checkpoint\s+small', re.I), 'Saving a small backup of your own setup'),
    (re.compile(r'checkpoint\s+full', re.I), 'Saving a full backup of your own setup'),
    (re.compile(r'checkpoint\s+drill', re.I), 'Checking that a backup can be restored'),
)
ONE_TIME = re.compile(r'^life-(?:work|exec):\s*(?:follow-?up:\s*)?(.+)$', re.I)
MACHINE = re.compile(r'\b(?:tick|pipes?|cron|shadow|script|agent|hermes|sync|heartbeat|bridge|gateway|webhook|api|daemon|plugin|engine|worker|'
                     r'test|sandbox|ledger|payload|json|outbox|queue|zeus-\w+)\b|[_/{}\[\]`]|\w\.(?:jsonl?|py|md|sqlite3|ya?ml|db|sh)\b', re.I)
STORE_WORDS = {'walmart': 'Walmart', 'amazon': 'Amazon', 'samsclub': "Sam's Club", 'southwest': 'Southwest'}


def on(life) -> bool:
    try:
        return (Path(life) / SWITCH).exists()
    except Exception:
        return False


# ---- small helpers ---------------------------------------------------------------------------------------------------

def _dt(value, tz=HOME):
    """An ISO time or epoch seconds -> an aware datetime, or None."""
    if value in (None, '', 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=tz)


def _ts(value):
    t = _dt(value)
    return t.timestamp() if t is not None else None


def _iso(value):
    t = _dt(value)
    return t.isoformat(timespec='seconds') if t is not None else None


def _sibling(name: str):
    """Another module of this engine. Under the tick and the life tool this folder is on the import path already; the
    chat plugin loads this file by path, so the folder is put on the path for the import and taken off again."""
    if name in sys.modules:
        return sys.modules[name]
    here = str(Path(__file__).resolve().parent)
    added, keep = here not in sys.path, sys.dont_write_bytecode
    if added:
        sys.path.insert(0, here)
    sys.dont_write_bytecode = True
    try:
        return __import__(name)
    finally:
        sys.dont_write_bytecode = keep
        if added and here in sys.path:
            sys.path.remove(here)


def _zone(life, at=None):
    """The real zone the owner is in at that moment (ownertime.py: their home zone unless they said they are away)."""
    try:
        z = _sibling('ownertime').zone(at, life)
        return z if isinstance(z, ZoneInfo) else HOME
    except Exception:
        return HOME


def _clock(t: datetime) -> str:
    return t.strftime('%I:%M%p').lstrip('0').lower().replace(':00', '')


def at_words(t: datetime, now: datetime) -> str:
    t = t.astimezone(now.tzinfo) if now.tzinfo else t
    days = (t.date() - now.date()).days
    day = {0: 'today', 1: 'tomorrow', -1: 'yesterday'}.get(days) or t.strftime('%a %b %d').replace(' 0', ' ')
    return f'{day} at {_clock(t)}'


def how_long(seconds) -> str:
    s = max(0, int(seconds))
    m = int(s / 60 + 0.5)
    if m < 120:
        return f'about {m} minutes'                 # under two hours the minutes are said: 90 minutes is not "about 2 hours"
    if s < 36 * 3600:
        return f'about {int(s / 3600 + 0.5)} hours'
    return f'about {int(s / 86400 + 0.5)} days'


def _plain_words(text, fallback: str, limit: int = 140) -> str:
    """Words for the owner: one line, no machine names. Anything else becomes the fallback."""
    t = ' '.join(str(text or '').split())
    if not t or MACHINE.search(t):
        return fallback
    return t if len(t) <= limit else t[:limit - 1].rstrip() + '…'


def jobs_path(life) -> Path:
    """The profile's scheduled jobs: <profile>/cron/jobs.json, two folders up from the life folder."""
    env = os.environ.get('LIFE_JOBS_FILE')
    return Path(env) if env else Path(life).parent.parent / 'cron' / 'jobs.json'


# ---- the three stores, read only -------------------------------------------------------------------------------------

# The routine check ("life pipes tick") may be started by the always-on watcher (zeus_watch.py), not by a Hermes job,
# once that job is paused. The readers below find it by the JOB's name, so with the job paused the line "Reading the
# owner's email and calendar for things to handle" would vanish from the timer list, the status answer and research's job page
# while the check was running fine. The evidence that it IS running is the engine's own record of its last check
# (last-tick.json, written as each check's last act), with the same 15 minutes an engine-silence alarm would use.
# No new file.
ROUTINE_CHECK = re.compile(r'life pipes tick', re.I)       # = the first pattern of JOB_NAMES
ENGINE_SILENT = 15 * 60
CHECK_BEAT_MIN = 5                                         # zeus_watch.BEAT: a check at least this often


def last_check(life):
    """When the newest routine check began (epoch seconds), by the engine's own record. None when there is no readable record."""
    try:
        return _ts(json.loads((Path(life) / 'last-tick.json').read_text(encoding='utf-8')).get('at'))
    except (OSError, ValueError, AttributeError):
        return None


def watcher_job(jobs, life, now_ts=None):
    """-> one job record standing for the routine check, when no Hermes job runs it and checks are happening (the last
    one began under 15 minutes ago); else None. With the Hermes job on, or with no fresh check, nothing is added: a job
    that is on is listed as itself, and a check that is not happening is never listed as running."""
    if life is None or any(isinstance(j, dict) and ROUTINE_CHECK.search(str(j.get('name') or '')) and job_running(j) for j in jobs or []):
        return None
    at = last_check(life)
    now_ts = datetime.now(HOME).timestamp() if now_ts is None else float(now_ts)
    if at is None or not 0 <= now_ts - at <= ENGINE_SILENT:
        return None
    stamp = lambda t: datetime.fromtimestamp(t, HOME).isoformat()
    return {'id': 'zeus-watch', 'name': 'Zeus life pipes tick (always-on watcher)', 'enabled': True, 'state': 'scheduled', 'paused_at': None,
            'schedule': {'kind': 'interval', 'minutes': CHECK_BEAT_MIN, 'display': f'every {CHECK_BEAT_MIN}m, sooner when something is due'},
            'schedule_display': f'every {CHECK_BEAT_MIN}m, sooner when something is due', 'last_run_at': stamp(at), 'next_run_at': stamp(at + CHECK_BEAT_MIN * 60),
            'last_status': 'ok', 'last_error': None, 'no_agent': True}


def read_jobs(jobs_file, life=None, now_ts=None):
    """-> (the job records, '') | (None, why it could not be read). A file that is not there is an empty store.
    life: with it, the routine check the watcher runs is one more record (watcher_job); without it, exactly as before."""
    p = Path(jobs_file) if jobs_file else None
    if p is None or not p.exists():
        rows = []
    else:
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            return None, f'{type(e).__name__}'
        rows = d.get('jobs') if isinstance(d, dict) else d
        if not isinstance(rows, list):
            return None, 'not a list of jobs'
    extra = watcher_job(rows, life, now_ts) if life is not None else None
    return (rows + [extra] if extra else rows), ''


def read_watches(life):
    p = Path(life) / 'watches.json'
    if not p.exists():
        return [], ''
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        return None, f'{type(e).__name__}'
    rows = d.get('watches') if isinstance(d, dict) else None
    if not isinstance(rows, list):
        return None, 'not a list of watches'
    return rows, ''


def open_ledger(life):
    """The life ledger, opened so that nothing can be written through this connection. None when there is none."""
    p = Path(life) / 'ledger.sqlite3'
    if not p.exists():
        return None
    con = sqlite3.connect('file:' + urllib.parse.quote(str(p)) + '?mode=ro', uri=True, timeout=5)
    con.execute('PRAGMA query_only=1')
    con.row_factory = sqlite3.Row
    return con


def read_items(con):
    """-> (every item of the ledger as a dict with its payload read, '') | (None, why)."""
    if con is None:
        return [], ''
    try:
        got = con.execute('SELECT key,state,surface_at,urgency,payload,sources,first_seen,updated FROM opps').fetchall()
    except sqlite3.Error as e:
        return None, f'{type(e).__name__}'
    out = []
    for r in got:
        r = dict(zip(('key', 'state', 'surface_at', 'urgency', 'payload', 'sources', 'first_seen', 'updated'), tuple(r)))
        try:
            r['p'] = json.loads(r['payload']) if r['payload'] else {}
        except ValueError:
            r['p'] = None                       # an unreadable payload: the item is still counted, as unrecognized
        out.append(r)
    return out, ''


class _Ledger:
    """`with _Ledger(life, con) as c`: the caller's connection as it is, or a read-only one of our own (closed again)."""

    def __init__(self, life, con=None):
        self.life, self.con, self.own = life, con, None

    def __enter__(self):
        if self.con is None:
            self.own = self.con = open_ledger(self.life)
        return self.con

    def __exit__(self, *exc):
        if self.own is not None:
            self.own.close()
        return False


# ---- rows: scheduled jobs --------------------------------------------------------------------------------------------

def _job_id(j, i) -> str:
    return str(j.get('id') or f'#{i}') if isinstance(j, dict) else f'#{i}'


def job_running(j) -> bool:
    return bool(j.get('enabled')) and j.get('state') not in ('paused', 'disabled', 'completed') and not j.get('paused_at')


def _every(j) -> str:
    sch = j.get('schedule') if isinstance(j.get('schedule'), dict) else {}
    m = sch.get('minutes')
    if isinstance(m, (int, float)) and m > 0:
        return 'every minute' if m == 1 else f'every {int(m)} min' if m < 60 else f'every {round(m / 60)} hours'
    if sch.get('kind') == 'once':
        return 'one time'
    return 'on a set schedule'


def job_what(j) -> str:
    name = str(j.get('name') or '')
    for pat, words in JOB_NAMES:
        if pat.search(name):
            return f'{words} ({_every(j)})'
    m = ONE_TIME.match(name)
    rest = m.group(1).strip() if m else re.sub(r'\(.*?\)', '', re.sub(r'^Zeus\s+', '', name, flags=re.I)).strip()
    if rest and not MACHINE.search(rest):
        rest = rest[:100]
        return f'A one-time check: {rest}' if (m or _every(j) == 'one time') else f'{rest[0].upper()}{rest[1:]} ({_every(j)})'
    return f'A small job of your own with no plain name ({_every(j)})'


def _job_result(j) -> str:
    if not j.get('last_run_at'):
        return 'has not run yet'
    bad = j.get('last_status') not in (None, 'ok') or bool(j.get('last_error'))
    text = 'its last run hit a problem' if bad else 'ran fine'
    d = j.get('last_dispatch') if isinstance(j.get('last_dispatch'), dict) else {}
    try:
        behind = float(d.get('lateness_seconds') or 0)
    except (TypeError, ValueError):
        behind = 0
    if behind > LATE_AFTER:
        text += f', and it started {how_long(behind)} late'
    if j.get('last_delivery_error'):
        text += ', and its last text to the owner did not go through'
    return text


def _job_row(j, i, now_ts) -> dict:
    nxt = _ts(j.get('next_run_at'))
    late = int(now_ts - nxt) if nxt is not None and now_ts - nxt > LATE_AFTER else 0
    return {'source': 'schedule', 'id': 'job:' + _job_id(j, i), 'what': job_what(j), 'next': _iso(j.get('next_run_at')),
            'last': _iso(j.get('last_run_at')), 'last_result': _job_result(j), 'late_s': late, 'late_why': 'unknown' if late else ''}


def _job_not_running(j) -> str:
    if j.get('paused_at') or j.get('state') == 'paused':
        return 'paused'
    if j.get('state') == 'completed':
        return 'finished'
    return 'switched off'


# ---- rows: ledger items ----------------------------------------------------------------------------------------------

def _item_last(p: dict):
    ex = p.get('exec') if isinstance(p.get('exec'), dict) else {}
    return _iso(ex.get('done_at') or p.get('ready_at') or ex.get('created_at'))


def _legs(p: dict) -> list:
    """The pages a commitment has saved to START watching later (commit.save_arm_step): [(n, leg, next_action)]."""
    c = p.get('commitment') if isinstance(p.get('commitment'), dict) else {}
    na = c.get('next_action') if isinstance(c.get('next_action'), dict) else {}
    if na.get('do') != 'arm_monitor':
        return []
    legs = (na.get('monitor') or {}).get('legs') if isinstance(na.get('monitor'), dict) else None
    return [(n, l, na) for n, l in enumerate(legs or [], 1) if isinstance(l, dict)]


def _item_row(r: dict, now_ts, why) -> dict:
    """`why(record, due)` -> (reason, detail); it is asked only for a row that is late right now."""
    p = r['p']
    nxt = _ts(r['surface_at'])
    late = int(now_ts - nxt) if nxt is not None and now_ts - nxt > LATE_AFTER else 0
    about = _plain_words(p.get('why') if p.get('followup') else '', '', 100)       # a later check carries its parent's title as its "why"
    fallback = ('A later check on: ' + about) if about else 'Something you are following up for the owner'
    return {'source': 'follow-up', 'id': 'item:' + r['key'], 'what': _plain_words(p.get('title'), fallback),
            'next': _iso(r['surface_at']), 'last': _item_last(p), 'last_result': STATE_WORDS[r['state']],
            'late_s': late, 'late_why': why(r, nxt)[0] if late else ''}


# ---- rows: watches -----------------------------------------------------------------------------------------------------

def _watch_result(w: dict) -> str:
    if int(w.get('fails') or 0) > 0:
        n = int(w['fails'])
        return f"could not be read the last {n} time{'s' if n != 1 else ''}"
    if w.get('pending'):
        return 'it changed; the owner has not been told yet'
    return 'read fine, no change' if int(w.get('quiet_checks') or 0) > 0 else 'read fine'


def _watch_row(w: dict, i, now_ts, why) -> dict:
    nxt = watch_due(w)      # not always `next_check`: failed reads move that forward (watch_due)
    late = int(now_ts - nxt) if nxt is not None and now_ts - nxt > LATE_AFTER else 0
    return {'source': 'watch', 'id': 'watch:' + str(w.get('id') or f'#{i}'), 'what': _plain_words(w.get('for'), 'A page you are watching for the owner'),
            'next': _iso(w.get('next_check')), 'last': _iso(w.get('checked_at')), 'last_result': _watch_result(w),
            'late_s': late, 'late_why': why(w, nxt)[0] if late else ''}


# ---- the one list ------------------------------------------------------------------------------------------------------

def _empty(name, where) -> dict:
    return {'store': name, 'where': str(where), 'readable': True, 'error': '', 'count': 0, 'listed': [], 'folded': [], 'not_running': [],
            'unrecognized': []}


def _order_rows(life, now_dt, acc: dict) -> list:
    """Rows other lanes hand over, each lane behind its own switch. A lane that breaks is one line in the accounting."""
    out = []
    for mod, switch in ORDER_MODULES:
        try:
            if not (Path(life) / switch).exists():
                continue
            m = _sibling(mod)
            got = m.timer_rows(life, now_dt)
        except Exception as e:
            acc['error'] = f'{mod}: {type(e).__name__}: {str(e)[:120]}'
            acc['readable'] = False
            continue
        for i, r in enumerate(got if isinstance(got, (list, tuple)) else []):
            acc['count'] += 1
            if not isinstance(r, dict) or not str(r.get('id') or '').strip():
                acc['unrecognized'].append(f'{mod}#{i}')
                continue
            try:
                late = max(0, int(r.get('late_s') or 0))
            except (TypeError, ValueError):
                late = 0
            row = {'source': 'order', 'id': str(r['id']), 'what': _plain_words(r.get('what'), 'A store order you are watching'),
                   'next': _iso(r.get('next')), 'last': _iso(r.get('last')), 'last_result': ' '.join(str(r.get('last_result') or '').split())[:160],
                   'late_s': late, 'late_why': (str(r.get('late_why') or '') if str(r.get('late_why') or '') in WHYS else 'unknown') if late else ''}
            out.append(row)
            acc['listed'].append(row['id'])
    return out


def collect(life, jobs_file=None, now_dt=None, con=None) -> dict:
    """-> {'now', 'rows', 'stores': {name: accounting}}. Reading only. Each store that cannot be read says so."""
    life = Path(life)
    now_dt = now_dt or datetime.now(HOME)
    now_ts = now_dt.timestamp()
    jobs_file = Path(jobs_file) if jobs_file else jobs_path(life)
    rows, hints = [], {}                            # hints: for a row that is late right now, the words for how late and why

    def said(row_id, check=False):
        def ask(fn):
            def why(rec, due):
                got = fn(rec, due)
                if due is not None:
                    hints[row_id] = clause(int(now_ts - due), got[0], got[1], due, life, check)
                return got
            return why
        return ask

    stores = {'schedules': _empty('schedules', jobs_file), 'items': _empty('items', life / 'ledger.sqlite3'),
              'starts': _empty('starts', 'monitors saved on items, to start later'), 'watches': _empty('watches', life / 'watches.json')}

    with _Ledger(life, con) as c:
        items, err = read_items(c)
        runs = _Runs(c)
        lapsed = _lapsed(life)

        # the ledger's items
        acc = stores['items']
        working = {}                                # job id -> the key of the open item that job is working right now
        if items is None:
            acc.update(readable=False, error=err)
        for r in items or []:
            acc['count'] += 1
            p, state = r['p'], r['state']
            if not isinstance(p, dict):
                acc['unrecognized'].append(r['key'])
            elif state in RUNNING_STATES:
                rows.append(_item_row(r, now_ts, said('item:' + r['key'])(lambda rr, due: _why(runs, life, due, now_ts, rr['p'], quiet=True))))
                acc['listed'].append(r['key'])
                job = (p.get('exec') or {}).get('job_id') if isinstance(p.get('exec'), dict) else None
                if state == 'executing' and job:
                    working[str(job)] = r['key']
            elif state in WAITING_STATES or state in CLOSED_STATES or state == HELD_STATE:
                acc['not_running'].append((r['key'], NOT_RUNNING_WORDS[state]))
            else:
                acc['unrecognized'].append(r['key'])
            # monitors saved on this item to start later
            if isinstance(p, dict):
                sacc = stores['starts']
                for n, leg, na in _legs(p):
                    sid = f"{r['key']}#{n}"
                    sacc['count'] += 1
                    if leg.get('watch'):
                        sacc['not_running'].append((sid, f"already started (it is watch {leg['watch']})"))
                    elif leg.get('stopped'):
                        sacc['not_running'].append((sid, 'stopped'))
                    elif state not in RUNNING_STATES:
                        sacc['not_running'].append((sid, 'its item is no longer open'))
                    elif na.get('gave_up'):
                        sacc['not_running'].append((sid, 'it could not be started; its item says so'))
                    else:
                        due = _ts(leg.get('starts')) or _ts(na.get('due'))
                        late = int(now_ts - due) if due is not None and now_ts - due > LATE_AFTER else 0
                        got_why = said('start:' + sid, True)(lambda _p, d: _why(runs, life, d, now_ts, _p, quiet=False))(p, due) if late else ('', {})
                        rows.append({'source': 'watch', 'id': 'start:' + sid,
                                     'what': 'Start watching: ' + _plain_words(leg.get('for'), 'a page for the owner'),
                                     'next': _iso(leg.get('starts') or na.get('due')), 'last': None, 'last_result': 'not started yet',
                                     'late_s': late, 'late_why': got_why[0]})
                        sacc['listed'].append(sid)

        # the profile's scheduled jobs
        acc = stores['schedules']
        jobs, err = read_jobs(jobs_file, life, now_ts)   # plus the routine check when the watcher, not a Hermes job, runs it
        if jobs is None:
            acc.update(readable=False, error=err)
        job_rows = []
        for i, j in enumerate(jobs or []):
            acc['count'] += 1
            jid = _job_id(j, i)
            if not isinstance(j, dict):
                acc['unrecognized'].append(jid)
            elif not job_running(j):
                acc['not_running'].append((jid, _job_not_running(j)))
            elif jid in working:
                acc['folded'].append((jid, 'item:' + working[jid]))      # the same timer as the item it is working
            else:
                job_rows.append(_job_row(j, i, now_ts))
                acc['listed'].append(jid)
        repeating = [x for x in job_rows if x['next']]
        if repeating and all(x['late_s'] for x in repeating):
            for x in repeating:                     # every job is behind: the scheduler itself is not running
                x['late_why'] = 'engine_down'
        rows += job_rows

        # the page watches
        acc = stores['watches']
        watches, err = read_watches(life)
        if watches is None:
            acc.update(readable=False, error=err)
        for i, w in enumerate(watches or []):
            acc['count'] += 1
            wid = str(w.get('id') or f'#{i}') if isinstance(w, dict) else f'#{i}'
            if not isinstance(w, dict):
                acc['unrecognized'].append(wid)
            elif w.get('state') == 'watching':
                rows.append(_watch_row(w, i, now_ts, said('watch:' + wid, True)(lambda ww, due: _read_why(runs, life, ww, due, now_ts, lapsed))))
                acc['listed'].append(wid)
            elif w.get('state') in ('ended', 'stopped'):
                why = ' '.join(str((w.get('closed') or {}).get('why') or '').split())[:80] if isinstance(w.get('closed'), dict) else ''
                acc['not_running'].append((wid, f"{w['state']}" + (f': {why}' if why else '')))
            else:
                acc['unrecognized'].append(wid)

    if any((life / switch).exists() for _, switch in ORDER_MODULES):
        stores['orders'] = _empty('orders', 'rows handed over by the order watch')
        rows += _order_rows(life, now_dt, stores['orders'])

    seen, once = set(), []
    for r in rows:                                  # every timer once
        if r['id'] not in seen:
            seen.add(r['id'])
            once.append(r)
    far = float('inf')
    once.sort(key=lambda r: (_ts(r['next']) if r['next'] else far, r['source'], r['id']))
    for r in once:
        if r['late_s'] and r['id'] not in hints:    # a schedule, or another lane's row: its own reason, in the same words
            hints[r['id']] = clause(r['late_s'], r['late_why'] or 'unknown', {}, now_ts - r['late_s'], life)
    return {'now': now_dt.isoformat(timespec='seconds'), 'rows': once, 'stores': stores, 'hints': hints}


def rows(life, jobs_file=None, now_dt=None) -> list:
    """Every running timer, soonest first (the contract between the lanes)."""
    return collect(life, jobs_file, now_dt)['rows']


# ---- the check: each store read again, record by record --------------------------------------------------------------

def _same_time(a, b) -> bool:
    ta, tb = _ts(a), _ts(b)
    if ta is None or tb is None:
        return ta is None and tb is None
    return abs(ta - tb) < 1


def check(life, jobs_file=None, now_dt=None, got=None, con=None) -> dict:
    """Does the list match each store, record by record? `got` (a collect() result) is what is being checked; the
    stores are read again here by their own short path. -> {'ok', 'rows', 'stores': {name: {...}}, 'stray_rows'}.
    Not ok: a record that is neither listed, folded nor counted as not running (missing); one that appears twice
    (doubled) or that the stores do not have (extra); one filed on the wrong side of running / not running
    (misfiled); a listed record with no row, or with a next or last time that differs from its store (differs); a
    record the code does not understand (unrecognized); a store that could not be read."""
    life = Path(life)
    now_dt = now_dt or datetime.now(HOME)
    jobs_file = Path(jobs_file) if jobs_file else jobs_path(life)
    got = got if got is not None else collect(life, jobs_file, now_dt, con)
    by_id = {}
    for r in got['rows']:
        by_id.setdefault(r['id'], []).append(r)
    raw = {}                                        # store -> {record id: (should it be running?, row id, next, last or ...)}
    unreadable = {}

    jobs, err = read_jobs(jobs_file, life, now_dt.timestamp())   # the same list collect() read
    if jobs is None:
        unreadable['schedules'] = err
    raw['schedules'] = {_job_id(j, i): (bool(isinstance(j, dict) and j.get('enabled') and j.get('state') == 'scheduled' and not j.get('paused_at')),
                                        'job:' + _job_id(j, i), j.get('next_run_at') if isinstance(j, dict) else None,
                                        j.get('last_run_at') if isinstance(j, dict) else None) for i, j in enumerate(jobs or [])}
    raw['items'], raw['starts'] = {}, {}
    with _Ledger(life, con) as c:
        if c is not None:
            try:
                for key, state, due, payload in c.execute('SELECT key,state,surface_at,payload FROM opps').fetchall():
                    raw['items'][key] = (state in RUNNING_STATES, 'item:' + key, due, ...)
                    try:
                        p = json.loads(payload) if payload else {}
                    except ValueError:
                        p = {}
                    for n, leg, na in _legs(p if isinstance(p, dict) else {}):
                        live = state in RUNNING_STATES and not leg.get('watch') and not leg.get('stopped') and not na.get('gave_up')
                        raw['starts'][f'{key}#{n}'] = (live, f'start:{key}#{n}', leg.get('starts') or na.get('due'), ...)
            except sqlite3.Error as e:
                unreadable['items'] = type(e).__name__
    watches, err = read_watches(life)
    if watches is None:
        unreadable['watches'] = err
    raw['watches'] = {str(w.get('id') or f'#{i}') if isinstance(w, dict) else f'#{i}':
                      (bool(isinstance(w, dict) and w.get('state') == 'watching'), 'watch:' + (str(w.get('id') or f'#{i}') if isinstance(w, dict) else f'#{i}'),
                       w.get('next_check') if isinstance(w, dict) else None, w.get('checked_at') if isinstance(w, dict) else None)
                      for i, w in enumerate(watches or [])}

    out, ok, claimed = {}, True, set()
    for name, records in raw.items():
        acc = (got.get('stores') or {}).get(name) or _empty(name, '')
        listed, folded = list(acc.get('listed') or []), [x[0] for x in acc.get('folded') or []]
        resting = [x[0] for x in acc.get('not_running') or []]
        filed = listed + folded + resting
        res = {'count': len(records), 'listed': len(listed), 'folded': len(folded), 'not_running': len(resting), 'not_running_why': {},
               'missing': sorted(set(records) - set(filed) - set(acc.get('unrecognized') or [])),
               'extra': sorted(set(filed) - set(records)), 'doubled': sorted({x for x in filed if filed.count(x) > 1}),
               'unrecognized': sorted(acc.get('unrecognized') or []), 'misfiled': [], 'differs': []}
        for _, why in acc.get('not_running') or []:
            why = re.sub(r' \(it is watch [^)]*\)|: .*$', '', str(why))
            res['not_running_why'][why] = res['not_running_why'].get(why, 0) + 1
        for rid, (live, row_id, nxt, last) in records.items():
            if live and rid in resting:
                res['misfiled'].append(f'{rid}: running in its store, counted as not running')
            if not live and (rid in listed or rid in folded):
                res['misfiled'].append(f'{rid}: not running in its store, listed as running')
            if rid in listed:
                mine = by_id.get(row_id) or []
                claimed.add(row_id)
                if len(mine) != 1:
                    res['differs'].append(f'{rid}: {len(mine)} rows in the list (must be 1)')
                    continue
                if not _same_time(mine[0].get('next'), nxt):
                    res['differs'].append(f"{rid}: next is {mine[0].get('next')} in the list, {nxt} in its store")
                if last is not ... and not _same_time(mine[0].get('last'), last):
                    res['differs'].append(f"{rid}: last is {mine[0].get('last')} in the list, {last} in its store")
                if any(k not in mine[0] for k in ROW_KEYS) or mine[0].get('source') not in SOURCES or not str(mine[0].get('what') or '').strip():
                    res['differs'].append(f'{rid}: its row is not in the agreed shape')
        if name in unreadable or not acc.get('readable', True):
            res['unreadable'] = unreadable.get(name) or acc.get('error') or 'could not be read'
        if acc.get('count') != len(records):
            res['differs'].append(f"the list counted {acc.get('count')} records, the store has {len(records)}")
        res['ok'] = not any(res[k] for k in ('missing', 'extra', 'doubled', 'unrecognized', 'misfiled', 'differs')) and 'unreadable' not in res
        ok = ok and res['ok']
        out[name] = res
    o = (got.get('stores') or {}).get('orders')
    if o is not None:                               # another lane's rows: counted, never compared with a store of ours
        claimed.update(o.get('listed') or [])
        out['orders'] = {'count': o.get('count'), 'listed': len(o.get('listed') or []), 'unrecognized': sorted(o.get('unrecognized') or []),
                         'ok': bool(o.get('readable', True)) and not o.get('unrecognized')}
        if not o.get('readable', True):
            out['orders']['unreadable'] = o.get('error')
        ok = ok and out['orders']['ok']
    stray = sorted(set(by_id) - claimed)
    doubled_rows = sorted(k for k, v in by_id.items() if len(v) > 1)
    return {'ok': ok and not stray and not doubled_rows, 'now': got.get('now'), 'rows': len(got['rows']), 'stores': out,
            'stray_rows': stray, 'doubled_rows': doubled_rows}


# ---- why late: evidence ------------------------------------------------------------------------------------------------

class _Runs:
    """When the life engine finished a run (the ledger's `runs` table: one row per run). No table = no evidence."""

    def __init__(self, con):
        self.con = con

    def between(self, a: float, b: float):
        """-> (the last run at or before a, [runs after a and up to b]) or (None, None) when the table cannot be read."""
        if self.con is None:
            return None, None
        try:
            prev = self.con.execute('SELECT MAX(at) FROM runs WHERE at<=?', (a,)).fetchone()[0]
            got = [float(r[0]) for r in self.con.execute('SELECT at FROM runs WHERE at>? AND at<=? ORDER BY at', (a, b)).fetchall()]
        except sqlite3.Error:
            return None, None
        return (float(prev) if prev is not None else None), got


def _gaps(runs: _Runs, due: float, sent: float):
    """Stretches between due and send with no finished run for RUN_GAP or more -> [(from, to)], or None = cannot tell."""
    prev, got = runs.between(due, sent)
    if got is None:
        return None
    marks = [prev if prev is not None else due] + got + [sent]
    return [(max(a, due), min(b, sent)) for a, b in zip(marks, marks[1:]) if b - a >= RUN_GAP and min(b, sent) > max(a, due)]


def _lapsed(life) -> dict:
    """The sign-in lapses signedin.py has written down (signins.json), with the hosts each site covers (the sign-in
    table: a copy in the life folder wins, as in signedin.sites). -> {site: {'hosts': [...], 'windows': [(from, to)]}}.
    Each site keeps the lapse that is open now ("lapse": since, no end) and the last one that closed ("last_lapse":
    since, closed_at). The closed one matters: a page of a lapsed site is only read again AFTER the owner signed back in, and
    by then the file no longer says "signed out"."""
    life = Path(life)
    try:
        d = json.loads((life / 'signins.json').read_text(encoding='utf-8'))
        sites = d.get('sites') if isinstance(d, dict) else None
    except (OSError, ValueError):
        sites = None
    if not isinstance(sites, dict):
        return {}
    table = {}
    for path in (life / 'signin-sites.json', Path(__file__).resolve().parent / 'signin-sites.json'):
        try:
            t = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(t, dict) and isinstance(t.get('sites'), dict):
            table = t['sites']
            break
    out = {}
    for site, v in sites.items():
        if not isinstance(v, dict):
            continue
        wins = []
        for name in ('lapse', 'last_lapse'):
            x = v.get(name)
            if isinstance(x, dict) and _ts(x.get('since')) is not None:
                wins.append((_ts(x['since']), _ts(x.get('closed_at')) if name == 'last_lapse' else None))
        if wins:
            hosts = (table.get(site) or {}).get('hosts') if isinstance(table.get(site), dict) else None
            hosts = [hosts] if isinstance(hosts, str) else [str(h).lower() for h in hosts or [] if h]
            out[str(site)] = {'hosts': hosts, 'windows': wins}
    return out


def _site_lapses(w: dict, lapses: dict) -> tuple:
    """The lapses of the signed-in site this watch reads -> ([(from, to or None)], site) or ([], '')."""
    read = w.get('read') if isinstance(w.get('read'), dict) else {}
    if read.get('kind') != 'chrome':
        return [], ''                               # a public page is read without the owner's sign-in
    try:
        host = (urllib.parse.urlsplit(str(read.get('url') or '')).hostname or '').lower()
    except ValueError:
        return [], ''
    for site, v in (lapses or {}).items():
        hosts = v.get('hosts') or []
        if any(host == h or host.endswith('.' + h) for h in hosts) or (not hosts and site and site.lower() in host):
            return list(v.get('windows') or []), site
    return [], ''


def watch_due(w: dict):
    """When this watch's read was really due -> epoch seconds or None. watches.apply moves `next_check` forward on
    every failed read (30 minutes, then longer), and keeps `unknown_since` = the first read of this run of failures.
    So while `fails` is above zero the read has been owed since `unknown_since`, whatever `next_check` says now."""
    times = [_ts(w.get('next_check'))]
    try:
        failing = int(w.get('fails') or 0) > 0
    except (TypeError, ValueError):
        failing = False
    if failing:
        times.append(_ts(w.get('unknown_since')))
    times = [t for t in times if t is not None]
    return min(times) if times else None


def _signin_window(p: dict, sent: float):
    """The stretch an item was held for a lapsed sign-in (signedin.hold) -> (from, to, site) or None."""
    mark = p.get('held_for_signin') if isinstance(p.get('held_for_signin'), dict) else None
    if mark:
        return _ts(mark.get('since')) or 0.0, sent, str(mark.get('site') or '')
    for name in ('signin_released', 'signin_not_renewed'):
        m = p.get(name) if isinstance(p.get(name), dict) else None
        if m and _ts(m.get('at')):
            return _ts(m.get('held_since') or m.get('since')) or 0.0, _ts(m['at']), str(m.get('site') or '')      # signedin.hold writes `held_since`
    return None


def _walk(due: float, sent: float, label) -> tuple:
    """Walk from due to send a minute at a time. -> (seconds per label, [(label, from, to)] in order)."""
    step = max(60.0, (sent - due) / 20000)
    totals, segs, t = {}, [], due
    while t < sent:
        k = label(t)
        d = min(step, sent - t)
        totals[k] = totals.get(k, 0) + d
        if segs and segs[-1][0] == k:
            segs[-1][2] = t + d
        else:
            segs.append([k, t, t + d])
        t += step
    return totals, segs


def _in_quiet(t, tz, life=None):
    at = datetime.fromtimestamp(t, tz)
    return alert_open(at, life=life) > at


def _why(runs: _Runs, life, due, sent, p: dict, quiet: bool = True, gone: bool = False) -> tuple:
    """Why a thing due at `due` had not gone by `sent` (both epoch seconds). -> (why, detail). The first thing that
    held it for LATE_AFTER or more, walking forward from due, is the reason; detail has the seconds per reason, the
    stretch that decided it, and the longest other hold-up ('then').
    gone: `sent` is the moment it really goes out (not "it is still waiting now"). A thing that goes out DURING quiet
    hours was not held by them (an urgent item at 10:40pm), so quiet hours are then never given as a reason."""
    p = p if isinstance(p, dict) else {}
    if due is None or sent <= due:
        return 'unknown', {}
    gaps = _gaps(runs, due, sent)
    hold = _signin_window(p, sent)
    tz = _zone(life, due)
    if gone and _in_quiet(sent, tz, life):
        quiet = False

    def label(t):
        if quiet and _in_quiet(t, tz, life):
            return 'quiet_hours'
        if gaps and any(a <= t < b for a, b in gaps):
            return 'engine_down'
        if hold and hold[0] <= t < hold[1]:
            return 'signin'
        return 'rest'

    totals, segs = _walk(due, sent, label)
    ex = p.get('exec') if isinstance(p.get('exec'), dict) else {}
    unread = isinstance(p.get('fresh'), dict) and bool(p['fresh'].get('unread'))
    worked = any(v is not None and v > due - 300 for v in (_ts(ex.get('created_at')), _ts(ex.get('done_at')), _ts(p.get('ready_at')))) \
        or bool(p.get('asked_later')) or bool(p.get('resend')) or int(ex.get('tries') or 0) > 1
    rest = 'could_not_read' if unread else 'busy' if worked else 'unknown'
    if gaps is None and rest == 'busy':
        rest = 'unknown'      # the run record cannot be read: code cannot know whether it was running, so it does not claim the work was slow
    totals = {(rest if k == 'rest' else k): v for k, v in totals.items()}
    segs = [((rest if k == 'rest' else k), a, b) for k, a, b in segs]
    first = next((s for s in segs if s[2] - s[1] >= LATE_AFTER), None)
    if first is None:                               # nothing held it 15 minutes in one stretch: the largest share
        k = max(totals, key=lambda x: (totals[x], -WHYS.index(x)))
        first = (k, due, sent)
    others = {k: v for k, v in totals.items() if k != first[0] and v >= THEN_AFTER}
    then = max(others, key=lambda x: others[x]) if others else ''
    detail = {'seconds': {k: int(v) for k, v in totals.items()}, 'from': first[1], 'to': first[2], 'then': then,
              'site': hold[2] if hold else '', 'paced': bool(p.get('asked_later')) and first[0] == 'busy', 'runs_known': gaps is not None}
    return first[0], detail


def _read_why(runs: _Runs, life, w: dict, due, now_ts, lapsed: dict, still: bool = True) -> tuple:
    """Why a page read that was due at `due` (watch_due) had not worked by now. Reads are not held by quiet hours.
    Minute by minute: no engine run = engine_down; inside a lapse of this page's sign-in (open now, or the last one
    that closed) = signin; anything else = could_not_read when its reads were failing, busy when more pages were due
    than one run reads, else unknown. Without a readable run record nothing is said about the engine: never busy.
    still: the reads are failing right now (the list) rather than having just worked again (late_read)."""
    if due is None or now_ts <= due:
        return 'unknown', {}
    gaps = _gaps(runs, due, now_ts)
    wins, site = _site_lapses(w, lapsed)
    try:
        failing = int(w.get('fails') or 0) > 0
    except (TypeError, ValueError):
        failing = False

    def label(t):
        if gaps and any(a <= t < b for a, b in gaps):
            return 'engine_down'
        if any(a <= t and (b is None or t < b) for a, b in wins):
            return 'signin'
        return 'rest'

    totals, segs = _walk(due, now_ts, label)
    rest = 'could_not_read' if failing else 'busy' if (w.get('coverage_reduced') and gaps is not None) else 'unknown'
    totals = {(rest if k == 'rest' else k): v for k, v in totals.items()}
    segs = [((rest if k == 'rest' else k), a, b) for k, a, b in segs]
    first = next((x for x in segs if x[2] - x[1] >= LATE_AFTER), None)
    if first is None:
        k = max(totals, key=lambda x: (totals[x], -WHYS.index(x)))
        first = (k, due, now_ts)
    return first[0], {'seconds': {k: int(v) for k, v in totals.items()}, 'from': first[1], 'to': first[2], 'then': '', 'site': site,
                      'paced': False, 'runs_known': gaps is not None, 'still_failing': bool(still and failing)}


# ---- why late: words ---------------------------------------------------------------------------------------------------

def why_words(why: str, detail: dict, due_ts: float, life=None, check: bool = False) -> str:
    tz = _zone(life, due_ts)
    a, b = detail.get('from'), detail.get('to')
    span = (f"from {_clock(datetime.fromtimestamp(a, tz))} to {_clock(datetime.fromtimestamp(b, tz))}" if a and b and b - a < 20 * 3600 else 'for a long stretch')
    if why == 'quiet_hours':
        return ('it came due during quiet hours, so it waited for the first allowed time' if _in_quiet(due_ts, tz, life)
                else 'quiet hours began before it could go, so it waited for morning')
    if why == 'engine_down':
        return f'I was not running {span}'
    if why == 'busy':
        if detail.get('paced'):
            return 'it was held back from an earlier message that already had enough questions'
        return 'more pages were due than I read in one pass' if check else 'the work on it took longer than planned'
    if why == 'could_not_read':
        if detail.get('still_failing'):
            return 'the page cannot be read right now'
        return 'the page could not be read at first' if check else 'what it depends on could not be read at first'   # a page check says page
    if why == 'signin':
        site = STORE_WORDS.get(str(detail.get('site') or '').split('.')[0].lower(), '')
        return f'the {site} sign-in had lapsed' if site else 'a sign-in had lapsed'
    return 'the reason is not known'


THEN_WORDS = {'quiet_hours': 'then quiet hours held it until morning', 'engine_down': 'and for part of that I was not running',
              'busy': 'and the work on it also took long', 'signin': 'and a sign-in had lapsed as well',
              'could_not_read': 'and what it depends on could not be read at first'}


def clause(late_s: int, why: str, detail: dict, due_ts: float, life=None, check: bool = False) -> str:
    """The one short clause, as a hint for the writer: how late and why."""
    how = how_long(late_s)
    if why == 'unknown':
        return f'{how} late, and the reason is not known'
    then = THEN_WORDS.get(detail.get('then') or '', '')
    if detail.get('then') == 'signin':              # the second hold-up names the store too, when the item's mark gives it
        then = 'and ' + why_words('signin', detail, due_ts, life, check) + ' as well'
    return f'{how} late: {why_words(why, detail, due_ts, life, check)}' + (f', {then}' if then else '')


# ---- late_fact ---------------------------------------------------------------------------------------------------------

def _row_of(con, key_or_row):
    """-> the item as a dict (key, state, surface_at, urgency, p, sources, first_seen) or None."""
    if isinstance(key_or_row, str):
        key = key_or_row[len('item:'):] if key_or_row.startswith('item:') else key_or_row
    else:
        try:
            key = key_or_row['key']
        except (KeyError, IndexError, TypeError):
            return None
    if con is None:
        return None
    r = con.execute('SELECT key,state,surface_at,urgency,payload,sources,first_seen FROM opps WHERE key=?', (key,)).fetchone()
    if r is None:
        return None
    r = dict(zip(('key', 'state', 'surface_at', 'urgency', 'payload', 'sources', 'first_seen'), tuple(r)))
    try:
        r['p'] = json.loads(r['payload']) if r['payload'] else {}
    except ValueError:
        return None
    return r if isinstance(r['p'], dict) else None


def stated_due(r: dict):
    """The time this item was stated to be due -> (epoch seconds, kind) or (None, ''). Kinds: 'reminder' (a reminder
    the owner is owed), 'follow-up' (a later check with its own time), 'asked' (the owner asked in chat for a later time).
    Everything else is routine: it rides the next note and has no stated time."""
    p = r['p']
    fu = p.get('followup') if isinstance(p.get('followup'), dict) else None
    if fu and _ts(fu.get('when')):
        return _ts(fu['when']), 'reminder' if fu.get('reminder') else 'follow-up'
    dec = p.get('decision') if isinstance(p.get('decision'), dict) else {}
    if not (dec.get('reason') == 'the owner asked in chat' or p.get('_source') == 'conversation' or p.get('from_chat')):
        return None, ''
    c = p.get('commitment') if isinstance(p.get('commitment'), dict) else {}
    times = [_ts(r.get('surface_at')), _ts(dec.get('surface_at'))]
    try:
        corrected = int(c.get('revision') or 1) > 1
    except (TypeError, ValueError):
        corrected = False
    if not corrected:                                # the time it had before it was held or put back (a correction makes those stale)
        times += [_ts((p.get('held_overnight') or {}).get('was_due')) if isinstance(p.get('held_overnight'), dict) else None,
                  _ts((p.get('asked_later') or {}).get('from')) if isinstance(p.get('asked_later'), dict) else None,
                  _ts((p.get('held_for_signin') or {}).get('was_due')) if isinstance(p.get('held_for_signin'), dict) else None]
    times = [t for t in times if t is not None]
    if not times:
        return None, ''
    due = min(times)
    born = r.get('first_seen') or p.get('_first_seen')
    try:
        born = float(born)
    except (TypeError, ValueError):
        return None, ''
    if due - born <= 5 * 60:
        return None, ''                              # asked for now, not for a time
    return due, 'asked'


def _watch_read_fact(life, sources) -> dict | None:
    """An item raised from a page change whose read ran late (late_read wrote it down with the change)."""
    try:
        srcs = json.loads(sources) if isinstance(sources, str) else list(sources or [])
    except ValueError:
        return None
    wanted = [str(s).split(':') for s in srcs if str(s).startswith('watch:')]
    wanted = [(x[1], x[2]) for x in wanted if len(x) == 3]
    if not wanted:
        return None
    watches, _ = read_watches(life)
    for w in watches or []:
        if not isinstance(w, dict):
            continue
        for wid, n in wanted:
            f = (w.get('late_reads') or {}).get(n) if w.get('id') == wid and isinstance(w.get('late_reads'), dict) else None
            if isinstance(f, dict) and int(f.get('late_s') or 0) > LATE_AFTER:
                return dict(f)
    return None


def late_fact(con, life, key_or_row, now_dt) -> dict | None:
    """Is this timed thing late at `now_dt` (the moment it is being told, or the moment asked about)?
    -> {'late_s', 'why', 'due', 'clause_hint', 'kind', 'then', 'seconds'} or None (not late, or it has no stated time).
    key_or_row: an item's key, an item row, or a row of the timer list. Never raises."""
    try:
        return _late_fact(con, Path(life), key_or_row, now_dt)
    except Exception:
        return None


def _late_fact(con, life: Path, key_or_row, now_dt):
    now_ts = now_dt.timestamp()
    if isinstance(key_or_row, dict) and 'source' in key_or_row and 'id' in key_or_row and 'key' not in key_or_row:
        row = key_or_row
        rid = str(row['id'])
        if not rid.startswith('item:'):              # a schedule, a watch, a start, an order: late right now, as the list says
            due = _ts(row.get('next'))
            w = None
            if rid.startswith('watch:'):
                w = next((x for x in (read_watches(life)[0] or []) if isinstance(x, dict) and 'watch:' + str(x.get('id')) == rid), None)
                if w is not None:
                    due = watch_due(w)              # failed reads moved its next try forward; it has been owed since the first failure
            if due is None or now_ts - due <= LATE_AFTER:
                return None
            why, detail = str(row.get('late_why') or 'unknown'), {}
            if w is not None:
                with _Ledger(life, con) as c:
                    why, detail = _read_why(_Runs(c), life, w, due, now_ts, _lapsed(life))
            late = int(now_ts - due)
            return {'late_s': late, 'why': why, 'due': _iso(due), 'kind': 'check' if row.get('source') == 'watch' else str(row.get('source')),
                    'then': detail.get('then', ''), 'seconds': detail.get('seconds', {}),
                    'clause_hint': clause(late, why, detail, due, life, check=row.get('source') == 'watch')}
        key_or_row = rid
    with _Ledger(life, con) as c:
        r = _row_of(c, key_or_row)
        if r is None:
            return None
        due, kind = stated_due(r)
        if due is None or now_ts - due <= LATE_AFTER:
            return _watch_read_fact(life, r.get('sources'))      # not late itself; the check that found it may have been
        why, detail = _why(_Runs(c), life, due, now_ts, r['p'], quiet=True, gone=True)      # now_dt is the moment it goes out
    late = int(now_ts - due)
    return {'late_s': late, 'why': why, 'due': _iso(due), 'kind': kind, 'then': detail.get('then', ''), 'seconds': detail.get('seconds', {}),
            'clause_hint': clause(late, why, detail, due, life)}


# ---- the note: one fact per late item, one sentence for the writer ---------------------------------------------------

LATE_RULE = ('\n- An item may carry "late": it reaches the owner after its own due time. Say that once, inside that item\'s own sentence, in one '
             'short clause of your own: how late and why, exactly as "late" gives them ("about 40 minutes late, I wasn\'t running"). No '
             'apology and nothing more about it. Never call an item without "late" late.')
LATE_MISSING = ('the results mark this one late, and the text never says so: {late} Say it once, in one short clause of your own inside '
                'that item\'s sentence: how late and why.')
SAYS_LATE = re.compile(r"\b(?:late|later than|overdue|behind|delayed|should have|was due|were due|meant to|supposed to|slipped|held up)\b", re.I)


def fact_text(f: dict, now_dt) -> str:
    """The fact as the writer gets it: true, from code, in plain words."""
    due = _dt(f.get('due'))
    when = at_words(due, now_dt) if due is not None else 'earlier'
    if f.get('kind') == 'check':
        return f"The check that found this was due {when} and ran {f['clause_hint']}."
    return f"This was due {when}. It is reaching the owner {f['clause_hint']}."


def _takes(fn, name: str) -> bool:
    import inspect
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in ps or any(p.kind == p.VAR_KEYWORD for p in ps.values())


def composing(con, life, now_dt, compose, keep: list, out: dict):
    """notifier.deliver: the writer for this note. No late item in it: the writer it was given, untouched (so the
    prompt is byte for byte what it was). Otherwise a wrapper that hands each late item its fact as "late", the
    writer one sentence (LATE_RULE) and, for a writer that takes it, the check for its one targeted fix. What the
    writer still leaves out is counted in the run's report (late_left_out); nothing is appended. Never raises."""
    try:
        if not on(life):
            return compose
        facts = {}
        for r, _o in keep:
            f = late_fact(con, life, r, now_dt)
            if f:
                facts[r['key']] = f
        if not facts:
            return compose
        titles = {str(o.get('title')): facts[r['key']] for r, o in keep if r['key'] in facts}
        order = [facts.get(r['key']) for r, _o in keep]
        out['late'] = [{'item': k, 'late_s': f['late_s'], 'why': f['why'], 'due': f['due']} for k, f in facts.items()]
    except Exception as e:
        try:
            out.setdefault('late_errors', []).append(f'{type(e).__name__}: {str(e)[:120]}')
        except Exception:
            pass
        return compose

    def wrapped(items, when, recent, **kw):
        try:
            shown = []
            for i, it in enumerate(items):
                f = None
                if isinstance(it, dict):
                    f = order[i] if len(items) == len(order) and order[i] and titles.get(str(it.get('item'))) is order[i] else titles.get(str(it.get('item')))
                shown.append({**it, 'late': fact_text(f, now_dt)} if f else it)
            lates = [it for it in shown if isinstance(it, dict) and it.get('late')]
            if lates and _takes(compose, 'rules'):
                kw['rules'] = str(kw.get('rules') or '') + LATE_RULE
            if lates and _takes(compose, 'must_say'):
                earlier = kw.get('must_say')
                kw['must_say'] = lambda text: (list(earlier(text)) if earlier else []) + (
                    [] if SAYS_LATE.search(str(text or '')) else [{'quote': str(it.get('item'))[:120], 'why': LATE_MISSING.format(late=it['late'])} for it in lates])
        except Exception:
            return compose(items, when, recent, **kw)
        result = compose(shown, when, recent, **kw)
        try:
            text = result.get('text') if isinstance(result, dict) else None
            if lates and text and not SAYS_LATE.search(text):
                out['late_left_out'] = len(lates)
        except Exception:
            pass
        return result
    return wrapped


# ---- watches: a read that ran late ---------------------------------------------------------------------------------------

OUTAGE_KINDS = ('outage', 'unclear', 'ended')     # monitor.py's notices that say a page could NOT be read: not a read that worked


def late_read(con, store, life, w: dict, now_dt) -> None:
    """watches.run, right after a due read found a change or could not be read: if that read came more than 15 minutes
    after it was due, the change it found keeps that fact (pending['late'], and late_reads by change number, the last
    five). `w` is the record as it was BEFORE the read; the store has it as it is AFTER.
    Due is `next_check` as it stood before the read. But when the reads had been failing (`fails` above zero before,
    and this read worked), the real engine has already moved `next_check` forward on each failure, so due is the
    first failed read (`unknown_since`): the check was owed from then, and it is late for could_not_read (or signin,
    when that site's sign-in had lapsed in between).
    Only a change this very read wrote down gets the fact: an older change still waiting to be told was found by an
    earlier read. Never raises."""
    try:
        now_ts = now_dt.timestamp()
        data = store.load() or {}
        rec = next((x for x in data.get('watches') or [] if isinstance(x, dict) and x.get('id') == w.get('id')), None)
        p = rec.get('pending') if rec is not None else None
        if not isinstance(p, dict) or not p.get('n'):
            return
        at = _ts(p.get('at'))
        if at is None or abs(at - now_ts) > 90:
            return                                  # that change was found by an earlier read
        try:
            failed_before = int(w.get('fails') or 0) > 0
        except (TypeError, ValueError):
            failed_before = False
        recovered = failed_before and p.get('kind') not in OUTAGE_KINDS      # this read wrote down a real change, so it worked (fails is back to 0)
        due = watch_due(w) if recovered else _ts(w.get('next_check'))
        if due is None or now_ts - due <= LATE_AFTER:
            return
        before = w if recovered else {k: v for k, v in w.items() if k != 'fails'}      # a late try that failed again is late for its own reason, not for the failures
        why, detail = _read_why(_Runs(con), life, before, due, now_ts, _lapsed(life), still=False)
        late = int(now_ts - due)
        fact = {'late_s': late, 'why': why, 'due': _iso(due), 'kind': 'check', 'then': '', 'seconds': detail.get('seconds', {}),
                'clause_hint': clause(late, why, detail, due, life, check=True)}
        n = p['n']

        def keep(d):
            r = next((x for x in d.get('watches') or [] if isinstance(x, dict) and x.get('id') == w.get('id')), None)
            q = r.get('pending') if r is not None else None
            if not isinstance(q, dict) or q.get('n') != n:
                return None
            q['late'] = fact
            reads = r.get('late_reads') if isinstance(r.get('late_reads'), dict) else {}
            reads[str(n)] = fact
            r['late_reads'] = {k: reads[k] for k in sorted(reads, key=lambda x: int(x) if str(x).isdigit() else 0)[-5:]}
            return True
        store.change(keep)
    except Exception:
        return


def observation_late(rec: dict, obs: dict) -> dict:
    """watches.observation: what the noticer is told when the read that found this change ran late. Never raises."""
    try:
        f = (rec.get('pending') or {}).get('late')
        if isinstance(f, dict) and int(f.get('late_s') or 0) > LATE_AFTER:
            obs['read_late'] = (f"Your own check of this page ran {f['clause_hint']}. If you tell the owner about this change, say that in one short "
                                'clause: how late the check was and why.')
    except Exception:
        pass
    return obs


def verbatim_late(con, life, rows_told: list, text: str, now_dt) -> str:
    """notifier.deliver_verbatim: a code-worded text about a page change whose read ran late says so, in code's own
    words, at the end of that text. One paragraph per item, in order; anything else: the text as it was."""
    try:
        parts = text.split('\n\n')
        if len(parts) != len(rows_told):
            return text
        for i, r in enumerate(rows_told):
            key = str(r['key'])
            bits = key.split(':')
            if len(bits) != 3 or bits[0] != 'monitor':
                continue
            f = _watch_read_fact(life, [f'watch:{bits[1]}:{bits[2]}'])
            if f:
                parts[i] = parts[i].rstrip() + f" (I read that page {f['clause_hint']}.)"
        return '\n\n'.join(parts)
    except Exception:
        return text


# ---- the status answer's one line ----------------------------------------------------------------------------------------

def _count_words(n: int, one: str, many: str) -> str:
    return f'{n} {one if n == 1 else many}'


def summary(got: dict) -> dict:
    by = {s: [r for r in got['rows'] if r['source'] == s] for s in SOURCES}
    late = sorted((r for r in got['rows'] if r['late_s']), key=lambda r: -r['late_s'])
    return {'by': by, 'late': late}


def status_line(life, jobs_file=None, now_dt=None, plain=None, got=None) -> str:
    """One plain line for the "is everything working?" answer: how many timers run, by kind; what is next; anything late
    right now with how late and why. `plain` is the status answer's own check of a line; a part that fails it is left
    out. '' when nothing can be said. Never raises."""
    try:
        life = Path(life)
        now_dt = now_dt or datetime.now(HOME)
        got = got or collect(life, jobs_file, now_dt)
        s = summary(got)
        ok = plain or (lambda line: True)
        counts = [_count_words(len(s['by']['schedule']), 'job on a schedule', 'jobs on a schedule'),
                  _count_words(len(s['by']['follow-up']), 'follow-up', 'follow-ups'),
                  _count_words(len(s['by']['watch']), 'page watch', 'page watches')]
        if s['by']['order']:
            counts.append(_count_words(len(s['by']['order']), 'store order', 'store orders'))
        line = 'Timers you have running: ' + ', '.join(counts[:-1]) + ' and ' + counts[-1] + '.'
        unread = [n for n, a in got['stores'].items() if not a.get('readable', True)]
        if unread:
            line += ' One of your timer lists could not be read just now, so this count is not complete.'
        now_ts = now_dt.timestamp()
        ahead = [r for r in got['rows'] if r['next'] and _ts(r['next']) >= now_ts and not r['late_s']
                 and not (r['source'] == 'schedule' and '(every ' in r['what'])]        # not the every-few-minutes jobs: those are always next
        if ahead:
            r = ahead[0]
            part = f" Next up: {r['what'][:90].rstrip('.')}, {at_words(_dt(r['next']), now_dt)}."
            if ok(part):
                line += part
        if s['late']:
            said = []
            for r in s['late'][:2]:
                hint = (got.get('hints') or {}).get(r['id']) or f"{how_long(r['late_s'])} late, and the reason is not known"
                what = re.sub(r' \((?:every |on a set schedule|one time)[^)]*\)$', '', r['what'])
                part = f"{what[:90].rstrip('.')} ({hint})"
                said.append(part if ok(part) else f"one {'job' if r['source'] == 'schedule' else 'follow-up' if r['source'] == 'follow-up' else 'check'} ({how_long(r['late_s'])} late)")
            more = len(s['late']) - len(said)
            line += ' Running late right now: ' + '; '.join(said) + (f', and {more} more' if more > 0 else '') + '.'
        else:
            line += ' Nothing is running late.'
        return line if ok(line) else ''
    except Exception:
        return ''


# ---- life timers ---------------------------------------------------------------------------------------------------------

TITLES = (('schedule', 'ON A SCHEDULE'), ('follow-up', 'FOLLOW-UPS'), ('watch', 'WATCHES'), ('order', 'STORE ORDERS'))


def as_text(got: dict, life, now_dt) -> str:
    s = summary(got)
    lines = [f"Timers running as of {at_words(now_dt, now_dt)}: {len(s['by']['schedule'])} on a schedule, {len(s['by']['follow-up'])} follow-ups, "
             f"{len(s['by']['watch'])} watches" + (f", {len(s['by']['order'])} store orders" if s['by']['order'] else '') + '. '
             + (f"{len(s['late'])} running late right now." if s['late'] else 'Nothing is running late.')]
    for source, title in TITLES:
        mine = s['by'][source]
        if not mine:
            continue
        lines.append('')
        lines.append(title)
        for r in mine:
            nxt = at_words(_dt(r['next']), now_dt) if r['next'] else 'no next time set'
            last = f"; last {at_words(_dt(r['last']), now_dt)}" if r['last'] else ''
            late = ''
            if r['late_s']:
                late = '  LATE: ' + ((got.get('hints') or {}).get(r['id']) or f"{how_long(r['late_s'])} late, and the reason is not known")
            lines.append(f"- next {nxt}{last} ({r['last_result']}): {r['what']}{late}")
    rest = []
    for name, acc in got['stores'].items():
        why = {}
        for _, w in acc.get('not_running') or []:
            w = re.sub(r' \(it is watch [^)]*\)|: .*$', '', str(w))
            why[w] = why.get(w, 0) + 1
        if acc.get('folded'):
            why['counted with the item they are working'] = len(acc['folded'])
        if why:
            rest.append(f"{name}: " + ', '.join(f'{n} {w}' for w, n in sorted(why.items())))
        if not acc.get('readable', True):
            rest.append(f"{name}: COULD NOT BE READ ({acc.get('error')})")
        if acc.get('unrecognized'):
            rest.append(f"{name}: {len(acc['unrecognized'])} record(s) this list does not understand")
    if rest:
        lines += ['', 'Not running, so not listed above (counted so nothing is hidden):'] + ['- ' + x for x in rest]
    return '\n'.join(lines)


def check_text(res: dict) -> str:
    lines = [f"Timer list against its stores, as of {res.get('now')}: " + ('MATCHES, record by record.' if res['ok'] else 'DOES NOT MATCH.'),
             f"rows in the list: {res['rows']}"]
    for name, r in res['stores'].items():
        why = ', '.join(f'{n} {w}' for w, n in sorted((r.get('not_running_why') or {}).items()))
        lines.append(f"{name}: {r.get('count')} in the store = {r.get('listed')} listed + {r.get('folded', 0)} folded into another row + "
                     f"{r.get('not_running', 0)} not running" + (f' ({why})' if why else '')
                     + f"; missing {len(r.get('missing') or [])}, extra {len(r.get('extra') or [])}, doubled {len(r.get('doubled') or [])}, "
                     f"misfiled {len(r.get('misfiled') or [])}, differs {len(r.get('differs') or [])}, not understood {len(r.get('unrecognized') or [])}"
                     + (f"; COULD NOT BE READ: {r['unreadable']}" if r.get('unreadable') else '') + (' -> ok' if r.get('ok') else ' -> NOT OK'))
        for k in ('missing', 'extra', 'doubled', 'misfiled', 'differs', 'unrecognized'):
            for x in (r.get(k) or [])[:20]:
                lines.append(f'    {k}: {x}')
    for k in ('stray_rows', 'doubled_rows'):
        for x in res.get(k) or []:
            lines.append(f'{k}: {x}')
    return '\n'.join(lines)


def cli(a: list, life) -> int:
    """life timers [--json] [--check] [--jobs <file>]. Reads only. 0 = fine; with --check, 1 = the list does not match."""
    life = Path(life)
    a = list(a)
    jobs_file = None
    if '--jobs' in a and a.index('--jobs') + 1 < len(a):
        jobs_file = a[a.index('--jobs') + 1]
    now_dt = datetime.now(_zone(life))
    got = collect(life, jobs_file, now_dt)
    if '--check' in a:
        res = check(life, jobs_file, now_dt, got)
        print(json.dumps(res, indent=1, default=str) if '--json' in a else check_text(res))
        return 0 if res['ok'] else 1
    if '--json' in a:
        acc = {n: {'count': s['count'], 'listed': len(s['listed']), 'folded': len(s['folded']), 'not_running': len(s['not_running']),
                   'unrecognized': len(s['unrecognized']), 'readable': s['readable']} for n, s in got['stores'].items()}
        print(json.dumps({'now': got['now'], 'rows': got['rows'], 'stores': acc}, indent=1, ensure_ascii=False, default=str))
        return 0
    print(as_text(got, life, now_dt))
    return 0


if __name__ == '__main__':
    _life = Path(os.environ.get('LIFE_HOME', str(settings.LIFE_HOME)))
    sys.exit(cli(sys.argv[1:], _life))

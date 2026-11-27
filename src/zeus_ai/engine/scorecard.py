"""scorecard: the week in counts, each one traceable to its rows. It never sends.

Prepared once a week, on the first run on a Monday at or after quiet hours end on the owner's clock; the week is saved in the ledger's
`meta` table (scorecard_last_week), so it is prepared once. It covers the past 7 days:

  useful results with proof          jobs closed as done with evidence on record (commit.py), prepared steps that went
                                     with a receipt (actions.py)
  finished without proof             jobs closed as done with no evidence on record (never counted as verified)
  jobs still open                    open commitments
  recoveries                         background work that failed once: worked on the second run / failed both
  repeats caught                     relevance.py's dropped repeats, and news code had already told the owner
  out-of-date or unrelated caught    items closed because the owner had already answered, results sent back because their
                                     source changed, chat replies trimmed to what the owner asked (turn-scope log)
  promises nothing carried           the chat's promise check (promise-checks log)
  source failures                    runs where mail, a calendar, a page or the model could not be read
  unresolved deliveries              notes whose delivery is still not settled (relevance.unresolved)
  backups and the restore drill      the checkpoint tool's own status file, when there is one
  storage                            the life folder's size, and its growth against the size the nightly closure wrote down

Every count carries the ids of the records behind it (`ids`), and a count that could not be made says so (n = None with
the reason): a log that is not there is "could not be counted", never 0. A missed-signal rate needs an independent
list of what should have been caught (<life>/expected-signals.jsonl, written by something OTHER than this engine);
without one the card says coverage is unmeasured.

It does not reach the owner (a weekly card texted to them would be a digest, and Zeus sends no digests). The card is
kept as a record in <life>/scorecards/<monday>.json; anyone (the owner, Zeus in chat, a developer) can read it there.
This module never sends and puts nothing on the owner's list.

Switch file `own-outcome-on` in the life folder. Without it nobody calls in here. No new table (it uses `meta`).
Standard library only. No model is asked.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import settings

SWITCH = 'own-outcome-on'
META = 'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)'      # the same table commit.py makes
K_WEEK = 'scorecard_last_week'
PREPARE_AT = settings.QUIET_END     # the owner's clock, Monday
WEEK = 7 * 86400
DIR = 'scorecards'
EXPECTED = 'expected-signals.jsonl'
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
IDS_KEPT = 200
def on(life) -> bool:
    try:
        return (Path(life) / SWITCH).exists()
    except (OSError, TypeError):
        return False


def _local(now) -> datetime:
    from ownertime import PACIFIC          # the clock the owner is on right now
    if isinstance(now, datetime):
        return now.astimezone(PACIFIC) if now.tzinfo else now.replace(tzinfo=PACIFIC)
    return datetime.fromtimestamp(float(now if now is not None else time.time()), PACIFIC)


def _has(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _json(text, default=None):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _ts_of(value):
    import closure
    return closure._ts_of(value)


def _count(ids=None, why=None) -> dict:
    """One count with the records behind it. ids=None: it could not be counted, and `why` says so."""
    if ids is None:
        return {'n': None, 'ids': [], 'why': why or 'could not be read'}
    ids = [str(i) for i in ids]
    return {'n': len(ids), 'ids': ids[:IDS_KEPT]} | ({'ids_cut': True} if len(ids) > IDS_KEPT else {})


def _log_lines(path: Path, since: float, until: float):
    """Rows of one of the chat's logs inside the week -> [(line number, row)], or None when the log is not there or
    cannot be read."""
    try:
        raw = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    out = []
    for n, line in enumerate(raw, 1):
        row = _json(line)
        at = _ts_of(row.get('at')) if isinstance(row, dict) else None
        if at is not None and since < at <= until:
            out.append((n, row))
    return out


# ---- the counts -------------------------------------------------------------------------------------------------------------

def build(con, life, now=None, *, checkpoint=None) -> dict:
    """The card for the 7 days before `now`. Read only. checkpoint: the status file to read (tests)."""
    import relevance as RL
    life = Path(life)
    now_dt = _local(now)
    until = now_dt.timestamp()
    since = until - WEEK
    c = {}

    # jobs
    if _has(con, 'commit_events') and _has(con, 'commitments'):
        proof, bare = [], []
        for cid, at, after in con.execute("SELECT commitment_id,at,after FROM commit_events WHERE kind='resolved' ORDER BY seq").fetchall():
            a = _json(after, {}) or {}
            t = _ts_of(at)
            if t is None or not (since < t <= until) or a.get('state') != 'done' or cid in proof or cid in bare:
                continue
            (proof if ((a.get('commitment') or {}).get('evidence_refs')) else bare).append(cid)
        marks = ','.join('?' * len(OPEN))
        still = [r[0] for r in con.execute(f'SELECT c.commitment_id FROM commitments c JOIN opps o ON o.key=c.opp_key WHERE o.state IN ({marks}) ORDER BY c.created', OPEN)]
        c['useful_verified_outcomes'], c['finished_without_proof'], c['unfinished_commitments'] = _count(proof), _count(bare), _count(still)
    else:
        why = 'no jobs are kept in this ledger yet'
        c['useful_verified_outcomes'], c['finished_without_proof'], c['unfinished_commitments'] = _count(None, why), _count(None, why), _count(None, why)
    if _has(con, 'actions'):
        went = [r[0] for r in con.execute("SELECT id FROM actions WHERE state='sent' AND receipt IS NOT NULL AND updated>? AND updated<=? ORDER BY updated", (since, until))]
        if c['useful_verified_outcomes']['n'] is None:
            c['useful_verified_outcomes'] = _count(went)
        else:
            c['useful_verified_outcomes'] = _count(c['useful_verified_outcomes']['ids'] + went)

    # recoveries: the worker's first run failed and a second one was started (exec.tries == 2)
    worked, failed = [], []
    for key, state, payload in con.execute('SELECT key,state,payload FROM opps WHERE updated>? AND updated<=?', (since, until)).fetchall():
        p = _json(payload, {}) or {}
        ex = p.get('exec') if isinstance(p.get('exec'), dict) else {}
        try:
            second = int(ex.get('tries') or 1) >= 2
        except (TypeError, ValueError):
            second = False
        if not second or state in ('executing', 'pending'):
            continue
        pk = p.get('packet') if isinstance(p.get('packet'), dict) else None
        (failed if (state == 'needs_attention' or pk is None or pk.get('prep')) else worked).append(key)
    c['recoveries_worked'], c['recoveries_failed'] = _count(worked), _count(failed)

    # what the runs themselves reported
    dup, stale, src_fail, runs = [], [], [], 0
    for at, report in con.execute('SELECT at,report FROM runs WHERE at>? AND at<=? ORDER BY at', (since, until)).fetchall():
        r = _json(report, {}) or {}
        runs += 1
        d = r.get('delivery') if isinstance(r.get('delivery'), dict) else {}
        if d.get('already_told_by_code'):
            dup.append(f'run:{at}')
        if d.get('moot') or d.get('fresh_redo') or (isinstance(r.get('fresh'), dict) and r['fresh']):
            stale.append(f'run:{at}')
        cal = r.get('calendar') if isinstance(r.get('calendar'), dict) else {}
        if any(str(e).startswith(('read ', 'model ', 'calendar')) for e in r.get('errors') or []) or r.get('model_failed') or (cal and not cal.get('ok')):
            src_fail.append(f'run:{at}')
    if _has(con, 'relevance_log'):
        dup += [f'relevance_log:{r[0]}' for r in con.execute("SELECT id FROM relevance_log WHERE verdict='duplicate' AND at>? AND at<=? ORDER BY id", (since, until))]
    scope = _log_lines(life / 'turn-scope.log.jsonl', since, until)
    if scope is not None:
        stale += [f'turn-scope.log.jsonl:{n}' for n, row in scope
                  if (row.get('step') == 'turn' and row.get('left_out')) or (row.get('step') == 'reply' and row.get('before') != row.get('after'))]
    c['duplicates_caught'], c['stale_or_unrelated_caught'] = _count(dup), _count(stale)
    c['source_failures'] = _count(src_fail) | {'runs': runs}
    readings = _log_lines(life / 'turn-readings.log.jsonl', since, until)
    c['chat_readings_without_the_model'] = _count(None, 'there is no chat reading log') if readings is None else \
        _count([f'turn-readings.log.jsonl:{n}' for n, row in readings if row.get('source') == 'fallback'])
    promises = _log_lines(life / 'promise-checks.log.jsonl', since, until)
    c['promises_nothing_carried'] = _count(None, 'there is no promise-check log') if promises is None else \
        _count([f'promise-checks.log.jsonl:{n}' for n, row in promises if any(isinstance(f, dict) and f.get('ok') is False for f in row.get('findings') or [])])

    # deliveries
    late = [u['note'] for u in RL.unresolved(con, until)]
    late += [r[0] for r in con.execute("SELECT key FROM outbox WHERE mode='live' AND state='failed' AND at>? AND at<=?", (since, until)) if r[0] not in late]
    c['unresolved_deliveries'] = _count(late)

    card = {'week_of': now_dt.date().isoformat(), 'from': _local(since).isoformat(), 'until': now_dt.isoformat(), 'counts': c,
            'backup': backup_status(checkpoint), 'storage': storage(life, since), 'coverage': coverage(life, since, until)}
    card['facts'] = facts(card)
    card = __import__('outcome').on_card(con, life, card, since, until, now_dt)   # outcome build (switch own-outcome-on): jobs due, finished on time with proof, times the owner asked again, the prepare and surface logs, open jobs without proof. The same card without the switch
    return card


def backup_status(path=None) -> dict:
    """The checkpoint tool's own status file (zeus_checkpoint.py writes <store>/status.json: last_small, last_full,
    total_bytes, count, offsite, drill, unfinished). -> {'readable': True, 'file', 'status': {...}} or
    {'readable': False, 'why'}. Not found or not readable is "could not be read", never "no backups"."""
    if path:
        f = Path(path)
    elif os.environ.get('ZEUS_CHECKPOINT_STATUS'):
        f = Path(os.environ['ZEUS_CHECKPOINT_STATUS'])
    else:
        f = Path(os.environ.get('ZEUS_CHECKPOINT_STORE') or settings.get('backup.checkpoint_store', str(settings.HERMES_HOME / 'checkpoints' / settings.PROFILE))) / 'status.json'
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        d = None
    if not isinstance(d, dict):
        return {'readable': False, 'why': 'no checkpoint status file could be read'}
    keep = {k: d.get(k) for k in ('updated', 'count', 'last_small', 'last_full', 'total_bytes', 'unfinished', 'drill') if k in d}
    if 'offsite' in d:
        off = d['offsite']
        keep['offsite'] = ({k: v for k, v in off.items() if k != 'not_copied_yet'} | {'not_copied_yet': len(off.get('not_copied_yet') or [])}) if isinstance(off, dict) else off
    return {'readable': True, 'file': str(f), 'status': keep}


def _time_words(value) -> str:
    """A stored time in everyday words; a value that is not a time is shown as it is."""
    t = _ts_of(value)
    if t is None or isinstance(value, bool):
        return str(value)
    t = _local(t)
    return t.strftime('%a %b ') + str(t.day) + ' at ' + t.strftime('%I:%M%p').lstrip('0').lower()


def backup_words(st: dict) -> list:
    """The checkpoint status as plain sentences. Only what the file says; a part it does not have is said to be missing."""
    out = []
    for key, name in (('last_small', 'small'), ('last_full', 'full')):
        b = st.get(key)
        if isinstance(b, dict) and (b.get('ended_at') or b.get('id')):
            flaw = '' if b.get('complete', True) and not b.get('problems') else ' (it is not complete)'
            out.append(f"The last {name} backup was made {_time_words(b.get('ended_at') or b.get('id'))}{flaw}.")
        elif key in st:
            out.append(f'No {name} backup is on record.')
    off = st.get('offsite')
    if isinstance(off, dict):
        if off.get('ok') is False:
            out.append(f"The offsite copy is failing since {_time_words(off.get('failing_since'))}: {' '.join(str(off.get('problem') or 'no reason recorded').split())[:160]}.")
        elif off.get('at'):
            out.append(f"The last offsite copy was made {_time_words(off.get('at'))}.")
        if off.get('not_copied_yet'):
            out.append(f"Backups not copied offsite yet: {off['not_copied_yet']}.")
    elif 'offsite' in st:
        out.append('No offsite copy is on record.')
    drill = st.get('drill')
    if isinstance(drill, dict) and drill.get('at'):
        out.append(f"The last restore drill, {_time_words(drill.get('at'))}, {'passed' if drill.get('ok') else 'FAILED'}.")
    elif 'drill' in st:
        out.append('No restore drill is on record.')
    if st.get('unfinished'):
        out.append(f"Backups that were started and never finished: {len(st['unfinished'])}.")
    if isinstance(st.get('total_bytes'), (int, float)) and not isinstance(st.get('total_bytes'), bool):
        out.append(f"All backups together take {_mb(st['total_bytes'])}.")
    return out


def storage(life, since: float) -> dict:
    """The life folder's size now, and its growth against the oldest size the nightly closure wrote down in the last
    8 days (metrics.jsonl). No earlier size on record: growth is not measured."""
    import closure
    now_bytes = closure.storage_bytes(life)
    earlier = None
    try:
        for raw in (Path(life) / closure.METRICS).read_text(encoding='utf-8').splitlines():
            row = _json(raw)
            at = _ts_of(row.get('at')) if isinstance(row, dict) else None
            if at is not None and at >= since - 86400 and isinstance(row.get('storage_bytes'), (int, float)):
                earlier = {'bytes': int(row['storage_bytes']), 'day': row.get('day')}
                break
    except OSError:
        pass
    return {'bytes': now_bytes, 'earlier': earlier, 'growth_bytes': (now_bytes - earlier['bytes']) if earlier else None}


def coverage(life, since: float, until: float) -> dict:
    """A missed-signal rate needs a list, kept by something other than this engine, of what should have been caught.
    Without one: unmeasured."""
    rows = _log_lines(Path(life) / EXPECTED, since, until)
    if not rows and (Path(life) / 'miss-review-on').exists():   # no outside list, so the nightly review's own feed is counted (missreview.coverage), worded as Zeus's own review
        own = __import__('missreview').coverage(life, since, until)
        if own:
            return own
    if not rows:
        return {'measured': False, 'why': 'there is no independent list of what should have been caught' if rows is None
                else 'the independent list has nothing for this week'}
    missed = [f'{EXPECTED}:{n}' for n, row in rows if not row.get('caught_by')]
    return {'measured': True, 'expected': len(rows), 'missed': len(missed), 'ids': missed[:IDS_KEPT]}


# ---- plain facts for the note writer ----------------------------------------------------------------------------------------

def _mb(n) -> str:
    return f'{n / 1_000_000:.1f} MB'


def facts(card: dict) -> list:
    """The card as plain sentences. No field names. A count that could not be made says so in words."""
    c = card['counts']

    def say(name, words):
        x = c[name]
        return f'{words}: {x["n"]}.' if x['n'] is not None else f'{words}: could not be counted ({x.get("why")}).'

    out = [say('useful_verified_outcomes', 'Useful results with proof on record'),
           say('finished_without_proof', 'Jobs marked finished with no proof on record'),
           say('unfinished_commitments', 'Jobs still open'),
           f"Background work that failed once and then worked on the second try: {c['recoveries_worked']['n']}. Failed both tries: {c['recoveries_failed']['n']}.",
           say('duplicates_caught', 'Repeats caught before they reached the owner'),
           say('stale_or_unrelated_caught', 'Out-of-date or unrelated messages caught before they reached the owner'),
           say('promises_nothing_carried', 'Promises in chat that nothing was set up to carry'),
           f"Runs where a source could not be read: {c['source_failures']['n']} of {c['source_failures']['runs']}.",
           say('unresolved_deliveries', 'Messages whose delivery is still not settled')]
    b = card['backup']
    if not b.get('readable'):
        out.append('Backup and restore-drill status could not be read (no checkpoint status file was found).')
    else:
        out += backup_words(b['status']) or ['The checkpoint status file was read, but it names no backup, copy or restore drill.']
    s = card['storage']
    if s['growth_bytes'] is None:
        out.append(f"The life folder is {_mb(s['bytes'])}. Its growth cannot be measured yet: no earlier size is on record.")
    else:
        more = s['growth_bytes']
        out.append(f"The life folder is {_mb(s['bytes'])}, {_mb(abs(more))} {'more' if more >= 0 else 'less'} than on {s['earlier'].get('day')}.")
    cov = card['coverage']
    if cov.get('words'):   # the nightly review's own sentence (only ever set by missreview.coverage), in place of the two below
        return out + [cov['words']]
    if cov.get('measured'):
        out.append(f"Of {cov['expected']} things an independent list says should have been caught, {cov['missed']} were missed.")
    else:
        out.append(f"How many things were missed is unmeasured: {cov['why']}.")
    return out


# ---- once a week --------------------------------------------------------------------------------------------------------------

def _write_whole(path: Path, text: str) -> None:
    import closure
    closure._write_whole(path, text)


def tend(con, life, now=None, *, checkpoint=None):
    """Call from every tick, before the notifier. On the first run on a Monday at or after quiet hours end (the owner's
    clock) the week's card is built and written to <life>/scorecards/ (a stored record; nothing goes on the owner's list).
    Sends nothing. -> {'status': 'prepared', ...} the one time it works, None the rest of the time."""
    life = Path(life)
    if not on(life):
        return None
    now_dt = _local(now)
    if now_dt.weekday() != 0 or now_dt.hour < PREPARE_AT:
        return None
    con.execute(META)
    week = now_dt.date().isoformat()
    row = con.execute('SELECT value FROM meta WHERE key=?', (K_WEEK,)).fetchone()
    if row is not None and str(tuple(row)[0]) >= week:
        return None
    card = build(con, life, now_dt, checkpoint=checkpoint)
    _write_whole(life / DIR / f'{week}.json', json.dumps(card, indent=2, ensure_ascii=False, default=str))
    if con.in_transaction:
        con.commit()
    con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (K_WEEK, week))   # no item for the owner: a weekly card sent to them would be a digest
    con.commit()
    return {'status': 'prepared', 'week': week, 'file': f'{DIR}/{week}.json'}

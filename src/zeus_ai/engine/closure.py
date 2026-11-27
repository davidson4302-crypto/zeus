"""closure: the nightly closing of the books. It SENDS NOTHING.

Once per day on the owner's own clock, on the first run at or after 8:30pm (after the nightly "how we work together" page and
the conflicts pass in tick.run_phases), Zeus closes the day:

  1. reconcile     execution, verification and delivery are compared for everything touched since the last closing,
                   and every disagreement is written down with the ids behind it (a job still "running" hours after it
                   started, a thing due that never started, a job marked done with no proof on record, an item marked
                   told whose note failed, a delivery still unresolved). Nothing is repaired by guessing.
  2. next steps    every open job (commit.py) must have a next step or a named thing it is waiting on. One that has
                   neither is given the one its own record supports (waiting on their answer, on the background job, on a
                   repair; or its own plain step at its own time) through commit.set_next_action, and is counted.
  3. decisions    the owner's approvals, declines, corrections and stops, as plain dated lines with their sources, in
                   <life>/daily/YYYY-MM-DD.md. Only what is on record: their recorded yes, a close made on their word, a
                   correction in the job history, a muted kind, a standing OK. A source that could not be read is
                   named as such in the file; it is never "nothing happened".
  4. sources       each source's freshness (fresh / stale / could not be read), from the health note and this run.
  5. counts        one line appended to <life>/metrics.jsonl.

Once and only once: the last completed day is saved in the ledger's `meta` table (closure_last_day) in the same
breath as the cut-off time, AFTER the files are written, so a restart cannot repeat a finished closing; the page is
written whole and a metrics line for a day that already has one is not added again, so a restart in the middle
repeats nothing either. A night that was missed is closed once at the next run (it covers everything since the last
closing, each line with its own date); a backlog of nights is never replayed one by one.

Always on for a real life folder. No new table (it uses `meta`, made here
on first use with the same shape commit.py gives it). Standard library only. No model is asked.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

SWITCH = 'closure-on'
CLOSE_AT = (20, 30)                 # their clock
META = 'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)'      # the same table commit.py makes
K_DAY, K_UNTIL = 'closure_last_day', 'closure_last_until'
LOOK_BACK_CAP = 7 * 86400           # a catch-up never reaches further back than this, and says so
RUNNING_TOO_LONG = 2 * 3600         # the worker's own limit is 40 minutes and one recovery run
DUE_NOT_STARTED = 3600
HEALTH_OLD = 15 * 60
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
DAILY, METRICS = 'daily', 'metrics.jsonl'


def on(life) -> bool:
    try:
        return life is not None   # always on for a real life folder
    except (OSError, TypeError):
        return False


# ---- time -----------------------------------------------------------------------------------------------------------------

def _local(now) -> datetime:
    from ownertime import PACIFIC          # the clock the owner is on right now: their home time zone unless they are away
    if isinstance(now, datetime):
        return now.astimezone(PACIFIC) if now.tzinfo else now.replace(tzinfo=PACIFIC)
    return datetime.fromtimestamp(float(now if now is not None else time.time()), PACIFIC)


def closed_day(now_dt: datetime) -> date:
    """The latest day, on their clock, whose 8:30pm has passed."""
    today = now_dt.date()
    return today if (now_dt.hour, now_dt.minute) >= CLOSE_AT else today - timedelta(days=1)


def _meta(con, key):
    row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return tuple(row)[0] if row else None


def due(con, now_dt: datetime):
    """-> (the day to close, None) or (None, 'early' | 'already'). The first closing ever waits for tonight's 8:30pm."""
    con.execute(META)
    day = closed_day(now_dt)
    last = _meta(con, K_DAY)
    tonight_passed = day == now_dt.date()
    if last and str(last) >= day.isoformat():
        return None, ('already' if tonight_passed else 'early')
    if not last and not tonight_passed:
        return None, 'early'
    return day, None


def _ts_of(value):
    """Epoch seconds from a number or an ISO time, or None."""
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        try:
            t = datetime.strptime(str(value), '%Y-%m-%dT%H:%M:%S%z')
        except ValueError:
            return None
    if t.tzinfo is None:
        from ownertime import PACIFIC
        t = t.replace(tzinfo=PACIFIC)
    return t.timestamp()


def _stamp(ts) -> str:
    t = _local(float(ts))
    return t.strftime('%Y-%m-%d ') + t.strftime('%I:%M%p').lstrip('0').lower()


def _has(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _one_line(text, cap: int = 240) -> str:
    return ' '.join(str(text or '').split())[:cap]


def _json(text, default=None):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


# ---- 3. their decisions -----------------------------------------------------------------------------------------------------

def decisions(con, life, since: float, until: float) -> tuple:
    """-> (lines, unreadable). Each line: {'at', 'kind': 'approval' | 'decline' | 'correction' | 'stop', 'text', 'source'}.
    Only what is on record. `unreadable` names every place that could not be read, so the page can say so."""
    life = Path(life)
    out, unreadable, seen = [], [], set()

    def add(at, kind, text, source, mark=None):
        at = _ts_of(at)
        if at is None or not (since < at <= until) or (mark and mark in seen):
            return
        if mark:
            seen.add(mark)
        out.append({'at': at, 'kind': kind, 'text': _one_line(text, 400), 'source': _one_line(source, 200)})

    for key, state, payload in con.execute('SELECT key,state,payload FROM opps WHERE updated>=?', (since - 60,)).fetchall():
        p = _json(payload)
        if not isinstance(p, dict):
            unreadable.append(f'item {key}')
            continue
        title = p.get('title') or key
        ap = p.get('approved') if isinstance(p.get('approved'), dict) else None
        if ap:
            words = f' Their words: "{_one_line(ap.get("his_words"), 200)}".' if ap.get('his_words') else ''
            add(ap.get('at'), 'approval', f'They said yes to: {_one_line(ap.get("ask") or title, 200)}.{words}', f'item {key}, recorded by {ap.get("by") or "the chat"}',
                mark=('yes', key))
        cl = p.get('closed') if isinstance(p.get('closed'), dict) else None
        by = str((cl or {}).get('by') or '')
        if cl and (by == 'owner' or by.startswith('zeus chat')):
            why = _one_line(cl.get('why'), 200)
            if cl.get('state') == 'declined':
                add(cl.get('at'), 'decline', f'They said no to, or dropped: {_one_line(title, 160)}. What was recorded: {why or "no reason"}.', f'item {key}, closed by {by}',
                    mark=('close', key))
            elif cl.get('state') == 'done' and (by.startswith('zeus chat, ') and 'answer to an offer' in by):
                add(cl.get('at'), 'approval', f'They answered: {_one_line(title, 160)}. What was recorded: {why}.', f'item {key}, closed by {by}', mark=('close', key))

    if _has(con, 'commit_events'):
        for seq, cid, rev, at, kind, actor, ref, after in con.execute(
                "SELECT seq,commitment_id,revision,at,kind,actor,source_ref,after FROM commit_events WHERE kind IN ('corrected','resolved') AND actor IN ('owner','chat') ORDER BY seq"):
            a = _json(after, {}) or {}
            what = _one_line(((a.get('commitment') or {}).get('subject') or {}).get('what') or cid, 200)
            src = f'job {cid}, history row {seq}' + (f', {ref}' if ref else '')
            if kind == 'corrected':
                add(at, 'correction', f'They corrected a job: {what}. It is now version {rev}.', src, mark=('fix', cid, rev))
            elif a.get('state') == 'declined' or ((a.get('commitment') or {}).get('phase') == 'stopped'):
                opp_key = (con.execute('SELECT opp_key FROM commitments WHERE commitment_id=?', (cid,)).fetchone() or [None])[0] if _has(con, 'commitments') else None
                add(at, 'stop', f'They stopped a job: {what}.', src, mark=('close', opp_key or cid))
    try:
        prefs = json.loads((life / 'preferences.json').read_text()) if (life / 'preferences.json').exists() else {}
        for m in prefs.get('muted') or []:
            note = f' Their note: "{_one_line(m.get("note"), 160)}".' if m.get('note') else ''
            add(m.get('at'), 'stop', f'They said to stop raising this kind of thing: {_one_line(m.get("title") or m.get("family"), 160)}.{note}',
                f'preferences, kind {m.get("family")}')
    except (OSError, ValueError, AttributeError):
        unreadable.append('their preferences file')
    try:
        grants = json.loads((life / 'grants.json').read_text()) if (life / 'grants.json').exists() else {}
        for k, g in (grants.items() if isinstance(grants, dict) else []):
            if isinstance(g, dict):
                words = g.get('his_words') or g.get('note')
                add(g.get('said_at') or g.get('at'), 'approval', f'They gave a standing OK: {_one_line(g.get("title") or k, 160)}.' + (f' Their words: "{_one_line(words, 200)}".' if words else ''),
                    f'standing OKs, {k}')
    except (OSError, ValueError, AttributeError):
        unreadable.append('their standing OKs file')
    log = life / 'turn-readings.log.jsonl'      # the chat's own reading of each message of theirs: a correction it saw
    if log.exists():
        try:
            for n, raw in enumerate(log.read_text(encoding='utf-8').splitlines(), 1):
                row = _json(raw)
                if isinstance(row, dict) and row.get('intent') == 'correction':
                    add(row.get('at'), 'correction', f'They corrected Zeus in chat: "{_one_line(row.get("corrected") or row.get("asked"), 240)}".',
                        f'chat reading log line {n}, turn {row.get("turn")}', mark=('turn', row.get('turn')))
        except OSError:
            unreadable.append("the chat's reading log")
    try:
        off = life / 'off'
        if off.exists():
            add(off.stat().st_mtime, 'stop', 'The life pipes were stopped (the stop switch was set).', 'the stop switch in the life folder')
    except OSError:
        unreadable.append('the stop switch')
    return sorted(out, key=lambda x: x['at']), unreadable


# ---- 2. every open job has a next step or a named dependency -----------------------------------------------------------------

def _derive(state, surface_at, p: dict, obj: dict):
    """The next step an open job's own record supports, or None when the record says nothing to go on."""
    what = _one_line((obj.get('subject') or {}).get('what') or p.get('proposed_action') or p.get('title'), 500)
    if state == 'surfaced':
        return {'do': 'Wait for their answer, then carry it out', 'depends_on': 'their answer to the question that was put to them'}
    if state == 'executing':
        return {'do': 'Read what the background job found', 'depends_on': 'the background job that is working on it'}
    if state == 'needs_attention':
        why = _one_line((p.get('attention') or {}).get('why') if isinstance(p.get('attention'), dict) else '', 140)
        return {'do': 'Sort out what stopped it', 'depends_on': ('a repair: ' + why) if why else 'a repair of what stopped it'}
    if state == 'ready':
        return {'do': 'Tell them the result in the next note', 'due': surface_at}
    return {'do': what, 'due': surface_at} if what else None


def next_steps(con, life, now_dt: datetime, day: str) -> dict:
    """-> {'readable', 'open', 'had_none': [ids], 'given': [ids], 'still_none': [ids]}. Jobs are only written to with
    the switch commit-on; without it they are counted and left alone."""
    if not _has(con, 'commitments'):
        return {'readable': False, 'why': 'no jobs are kept in this ledger yet'}
    import commit as CM
    marks = ','.join('?' * len(OPEN))
    rows = con.execute(f'SELECT c.commitment_id,c.revision,o.state,o.surface_at,o.payload FROM commitments c JOIN opps o ON o.key=c.opp_key WHERE o.state IN ({marks}) '
                       'ORDER BY c.created, c.commitment_id', OPEN).fetchall()
    out = {'readable': True, 'open': len(rows), 'had_none': [], 'given': [], 'still_none': []}
    may_write = CM.on(life)
    for cid, rev, state, surface_at, payload in [tuple(r) for r in rows]:
        p = _json(payload, {}) or {}
        obj = CM.get(con, cid) or {}                   # the job as its history last saved it (that copy counts, not the item's)
        na = obj.get('next_action') if isinstance(obj.get('next_action'), dict) else {}
        if str(na.get('do') or '').strip() or str(na.get('depends_on') or '').strip():
            continue
        out['had_none'].append(cid)
        step = _derive(state, surface_at, p, obj) if may_write else None
        done = False
        for attempt in ((step, {k: v for k, v in (step or {}).items() if k != 'due'}) if step else ()):
            try:
                CM.set_next_action(con, cid, attempt, expected_revision=rev, op_id=f'closure:{day}:{cid}:r{rev}', actor='tick', now=now_dt)
                done = True
                break
            except CM.Refused:
                continue                               # a time the job history cannot take: the same step without it
            except (CM.Conflict, sqlite3.Error):
                break
        out['given' if done else 'still_none'].append(cid)
    return out


# ---- 1. reconcile -----------------------------------------------------------------------------------------------------------

def reconcile(con, life, since: float, until: float) -> dict:
    """Execution, verification and delivery side by side. Read only. Every count carries its ids."""
    import relevance as RL
    ex = {'running_too_long': [], 'due_not_started': []}
    ver = {'done_with_proof': [], 'done_without_proof': []}
    by_state, told_but_failed = {}, []
    for key, state, surface_at, payload, updated in con.execute('SELECT key,state,surface_at,payload,updated FROM opps').fetchall():
        p = _json(payload, {}) or {}
        if state == 'executing':
            started = _ts_of((p.get('exec') or {}).get('run_at_ts') if isinstance(p.get('exec'), dict) else None)
            if started and until - started > RUNNING_TOO_LONG:
                ex['running_too_long'].append(key)
        elif state == 'pending':
            at = _ts_of(surface_at)
            if at and until - at > DUE_NOT_STARTED and not p.get('held_overnight'):
                ex['due_not_started'].append(key)
        told_at = _ts_of((p.get('told') or {}).get('at') if isinstance(p.get('told'), dict) else None)
        closed_at = _ts_of((p.get('closed') or {}).get('at') if isinstance(p.get('closed'), dict) else None)
        if any(t and since < t <= until for t in (told_at, closed_at)):
            ds = RL.delivery_state(con, key)
            by_state.setdefault(ds['state'] or 'unknown', []).append(key)
            if ds['state'] == RL.FAILED and state in ('done', 'surfaced'):
                told_but_failed.append(key)
    if _has(con, 'commit_events'):
        for cid, at, after in con.execute("SELECT commitment_id,at,after FROM commit_events WHERE kind='resolved' ORDER BY seq").fetchall():
            a = _json(after, {}) or {}
            c = a.get('commitment') if isinstance(a.get('commitment'), dict) else {}
            at = _ts_of(at)
            if not (at and since < at <= until) or a.get('state') != 'done' or cid in ver['done_with_proof'] + ver['done_without_proof']:
                continue
            ver['done_with_proof' if c.get('evidence_refs') else 'done_without_proof'].append(cid)
    late = RL.unresolved(con, until)
    return {'execution': ex, 'verification': ver,
            'delivery': {'by_state': by_state, 'marked_told_but_note_failed': told_but_failed, 'unresolved_over_30_min': [u['note'] for u in late]}}


# ---- 4. sources -------------------------------------------------------------------------------------------------------------

def sources(life, now_dt: datetime, report=None) -> dict:
    """Each source -> 'fresh' | 'stale' | 'could not be read'. Never fresh by default: what is not known to be fresh is
    one of the other two."""
    life, out = Path(life), {}
    try:
        note = json.loads((life / 'health.json').read_text())
    except (OSError, ValueError):
        note = None
    if not isinstance(note, dict):
        return {'the health note': 'could not be read', 'calendars': 'could not be read', 'mail feed': 'could not be read'}
    old = now_dt.timestamp() - float(note.get('when_ts') or 0) > HEALTH_OLD

    def put(name, state):
        out[name] = 'stale' if (old and state == 'fresh') else state

    out['the health note'] = 'stale' if old else 'fresh'
    cal = note.get('calendar') if isinstance(note.get('calendar'), dict) else None
    if cal is None:
        out['calendars'] = 'could not be read'
    else:
        put('calendars', 'stale' if cal.get('stale') else 'fresh' if cal.get('ok') else 'could not be read')
        for u in cal.get('unreadable') or []:
            out['calendar: ' + str(u.get('name') or u.get('scope') or 'one calendar')] = 'could not be read'
    box = note.get('inbox_feed') if isinstance(note.get('inbox_feed'), dict) else None
    if box is None or not box.get('readable'):
        out['mail feed'] = 'could not be read'
    else:
        put('mail feed', 'stale' if box.get('stale') else 'fresh')
    n = note.get('noticer') if isinstance(note.get('noticer'), dict) else {}
    if int(n.get('all_failed_runs') or 0) > 0:
        out['the model that reads new mail and calendar changes'] = 'could not be read'
    w = note.get('watches') if isinstance(note.get('watches'), dict) else None
    if w is not None:
        if not w.get('readable'):
            out['watched pages'] = 'could not be read'
        else:
            put('watched pages', 'fresh')
            for u in w.get('unreadable') or []:
                out['watched page: ' + _one_line(u.get('for') or 'one page', 80)] = 'could not be read'
    learn = (report or {}).get('learn') if isinstance(report, dict) else None
    if isinstance(learn, dict):
        out['owner chat'] = 'could not be read' if learn.get('errors') else 'fresh'
    elif isinstance(report, dict):
        out['owner chat'] = 'stale'              # not read on this run
    return out


# ---- the page, the counts line -----------------------------------------------------------------------------------------------

def _write_whole(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def page(day: date, now_dt: datetime, since: float, lines: list, unreadable: list, counts: dict, capped: bool) -> str:
    out = [f'# {day.strftime("%a %b")} {day.day}, {day.year}', '',
           f'Closed at {_stamp(now_dt.timestamp())}. Covers {_stamp(since)} to {_stamp(now_dt.timestamp())}. Nothing here was sent to them.']
    if capped:
        out.append('Earlier nights were missed. This closing reaches back seven days and no further.')
    out += ['', '## What they decided']
    words = {'approval': 'yes', 'decline': 'no', 'correction': 'correction', 'stop': 'stop'}
    out += [f"- {_stamp(l['at'])} ({words[l['kind']]}): {l['text']} Source: {l['source']}." for l in lines] or \
           ['- No approval, decline, correction or stop is on record for this stretch.']
    if unreadable:
        out.append('- Could not be read, so this list may be missing something: ' + ', '.join(sorted(set(unreadable))) + '.')
    out += ['', '## Sources']
    out += [f'- {name}: {state}' for name, state in counts['sources'].items()]
    jobs = counts['commitments']
    out += ['', '## Open jobs']
    if not jobs.get('readable'):
        out.append('- Could not be counted: ' + str(jobs.get('why') or 'the job list could not be read') + '.')
    else:
        out.append(f"- {jobs['open']} open. {len(jobs['had_none'])} had no next step and nothing named that they wait on; {len(jobs['given'])} of those were given one "
                   f"from their own record, {len(jobs['still_none'])} still have none.")
    d = counts['reconcile']['delivery']
    out += ['', '## Deliveries']
    out += [f'- {state}: {len(keys)}' for state, keys in sorted(d['by_state'].items())] or ['- Nothing was told or closed in this stretch.']
    if d['unresolved_over_30_min']:
        out.append(f"- Still unresolved after 30 minutes: {len(d['unresolved_over_30_min'])} ({', '.join(d['unresolved_over_30_min'][:5])}).")
    return '\n'.join(out) + '\n'


def storage_bytes(life) -> int:
    total = 0
    for root, _dirs, files in os.walk(Path(life)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _metrics_has(path: Path, day: str) -> bool:
    try:
        return any((_json(raw) or {}).get('day') == day for raw in path.read_text(encoding='utf-8').splitlines())
    except OSError:
        return False


# ---- the pass ----------------------------------------------------------------------------------------------------------------

def run(con, life, now=None, report=None) -> dict:
    """Call from every tick, after the other once-a-day passes. Works at most once per day on their clock. Sends nothing.
    -> {'status': 'off' | 'early' | 'already' | 'done', ...counts}"""
    life = Path(life)
    if not on(life):
        return {'status': 'off'}
    now_dt = _local(now)
    day, why_not = due(con, now_dt)
    if day is None:
        return {'status': why_not}
    until = now_dt.timestamp()
    last_until = _ts_of(_meta(con, K_UNTIL))
    since = last_until if last_until else until - 86400
    capped = until - since > LOOK_BACK_CAP
    if capped:
        since = until - LOOK_BACK_CAP
    catch_up = day != now_dt.date() or bool(_meta(con, K_DAY) and (day - date.fromisoformat(str(_meta(con, K_DAY)))).days > 1)

    jobs = next_steps(con, life, now_dt, day.isoformat())
    lines, unreadable = decisions(con, life, since, until)
    rec = reconcile(con, life, since, until)
    src = sources(life, now_dt, report)
    kinds = {k: sum(1 for l in lines if l['kind'] == k) for k in ('approval', 'decline', 'correction', 'stop')}
    counts = {'day': day.isoformat(), 'at': now_dt.isoformat(), 'from': _local(since).isoformat(), 'until': now_dt.isoformat(), 'catch_up': catch_up,
              'look_back_capped': capped, 'decisions': kinds, 'decisions_unreadable': sorted(set(unreadable)), 'commitments': jobs, 'reconcile': rec,
              'sources': src, 'storage_bytes': storage_bytes(life),
              'items': {'open': con.execute(f"SELECT COUNT(*) FROM opps WHERE state IN ({','.join('?' * len(OPEN))})", OPEN).fetchone()[0]}}
    _write_whole(life / DAILY / f'{day.isoformat()}.md', page(day, now_dt, since, lines, unreadable, counts, capped))
    if not _metrics_has(life / METRICS, day.isoformat()):
        with open(life / METRICS, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(counts, ensure_ascii=False, default=str) + '\n')
    con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (K_DAY, day.isoformat()))
    con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (K_UNTIL, repr(until)))
    con.commit()
    return {'status': 'done', 'day': day.isoformat(), 'catch_up': catch_up, 'decisions': kinds, 'jobs_given_a_next_step': len(jobs.get('given') or []),
            'jobs_still_without': len(jobs.get('still_none') or []), 'unresolved_deliveries': len(rec['delivery']['unresolved_over_30_min']),
            'sources_not_fresh': sorted(k for k, v in src.items() if v != 'fresh')}

"""Zeus life tick: notice -> decide -> WORK quietly (real Zeus) -> one NOTE (real Zeus) -> watch.

The free work is done first and the owner is told afterwards, in one message written by the Zeus they chat with,
instead of an offer per item the moment it comes due.

One scheduled job runs this every 5 minutes.
  sources.py   read-only observations (the owner's mailboxes, the configured calendar) + "did the owner already
               answer this thread?"
  noticer.py   model step: what would a great personal assistant do with this?
  policy.py    the owner's autonomy rules, per step; code has the last word on money, outside people, calendar, deletes
  worker.py    the real Zeus does the free work in a quiet background job and hands back a result packet
  notifier.py  everything ready at once -> one note, written by composer.py (the real Zeus); sent once via Zeus's line
  prefs.py     what the owner said to stop or handles themselves
  lifecli.py   how the Zeus in the owner's chat sees and closes its own items (yes, handled, stop)

Switches in <LIFE_HOME>: off (everything stops), notify-live (send for real; without it items wait and nothing is
used up), executor-off (no background jobs: items are told without prep).
Every run, also with `off` present, leaves <LIFE_HOME>/health.json (lifehealth.py): what could and could not be read,
whether the model answered, which switches are set. With `off` the run also reads back a note that was already on its
way; it sends nothing, starts no job and raises no ticket.
health-alarms-on: only with this switch file are the health note's own repair tickets raised (model down, mail feed
quiet, calendar copy old, a calendar unreadable). Without it the note is still written and lists them as held.
Usage: python3 -I tick.py [--max-calls N] [--email-days N] [--only email|calendar] [--no-notice] [--dry]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import policy as P  # noqa: E402
import prefs as F  # noqa: E402
import worker as W  # noqa: E402
import notifier  # noqa: E402
import lifehealth as H  # noqa: E402
import fresh as FR  # noqa: E402
import commit as CM  # noqa: E402
import integrity as CI  # noqa: E402

import settings  # noqa: E402

LIFE = Path(os.environ.get('LIFE_HOME') or settings.LIFE_HOME)
OPEN_STATES = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
CLOSED_STATES = ('done', 'moot', 'declined')
KEEP_ON_MERGE = ('decision', 'exec', 'packet', 'told', 'closed', 'followup', '_source', 'thread', 'latest_email', 'update_of', 'earlier', 'asked_later')
SCHEMA = """
CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, fp TEXT, at REAL, opp_count INTEGER, cost REAL);
CREATE TABLE IF NOT EXISTS opps(key TEXT PRIMARY KEY, state TEXT NOT NULL, autonomy TEXT, surface_at TEXT, urgency TEXT,
  action_class TEXT, payload TEXT NOT NULL, sources TEXT NOT NULL, first_seen REAL, updated REAL);
CREATE TABLE IF NOT EXISTS outbox(key TEXT PRIMARY KEY, text TEXT, mode TEXT, at REAL, state TEXT);
CREATE TABLE IF NOT EXISTS runs(at REAL PRIMARY KEY, report TEXT);
CREATE TABLE IF NOT EXISTS note_items(note TEXT NOT NULL, opp TEXT NOT NULL, PRIMARY KEY(note, opp));
"""


def db(life=None):
    life = Path(life or LIFE)
    life.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(life / 'ledger.sqlite3', timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# An offer of a standing way of working (offers.py, switch file reflect-on) is a question about the future, not a
# thing in the world: it is never shown to the noticer as open or handled, and new mail is never merged into it.
# No row matches this unless the reflection step has put one there.
NOT_AN_OFFER = "AND json_extract(payload,'$.offer.rule_key') IS NULL"


def open_opps(con):
    return [{'key': r['key'], 'title': json.loads(r['payload']).get('title'), 'state': r['state']}
            for r in con.execute("SELECT key,payload,state FROM opps WHERE state NOT IN ('done','moot','declined','skipped') " + NOT_AN_OFFER + " ORDER BY updated DESC LIMIT 60")]


def recently_handled(con, days=7):
    """What Zeus already dealt with or told the owner this week, so the noticer does not raise it again without news."""
    out = []
    for r in con.execute("SELECT key,state,payload FROM opps WHERE state IN ('done','moot','declined') AND updated>=? AND key NOT LIKE '%#fu%' " + NOT_AN_OFFER +
                         " ORDER BY updated DESC LIMIT 40", (time.time() - days * 86400,)):
        p = json.loads(r['payload'])
        out.append({'title': p.get('title'), 'how_it_ended': r['state'],
                    'what_he_was_told': (p.get('packet') or {}).get('headline') or (p.get('closed') or {}).get('why')})
    return out


def _yearish(w: str) -> bool:
    """A word that starts with a year near now (2026, 2027, 20261120) is a date, never a record id."""
    import time as _t
    y = _t.localtime().tm_year
    return w[:4].isdigit() and y - 1 <= int(w[:4]) <= y + 2


def _words(t):
    stop = {'the', 'and', 'for', 'with', 'your', 'owner', settings.OWNER_FIRST.lower(), 'check', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'about'}
    return {w for w in re.findall(r'[a-z0-9]{3,}', str(t).lower()) if w not in stop}


def same_thing(con, opp):
    """The same real-world thing already in the ledger under another key (two calendar copies, a resent email).
    -> (key, state) or (None, None). An open item wins over a closed one. Follow-up checks are never matched: a new
    email about a flight must not disappear into Saturday's scheduled check."""
    a = _words(opp.get('title')) | _words(opp.get('dedupe_key'))
    closed = (None, None)
    for r in con.execute("SELECT key,state,payload FROM opps WHERE state!='skipped' AND key NOT LIKE '%#fu%' AND updated>=? " + NOT_AN_OFFER + " ORDER BY updated DESC",
                         (time.time() - 21 * 86400,)):
        p = json.loads(r['payload'])
        b = _words(p.get('title')) | _words(r['key'])
        ids = {w for w in a & b if any(ch.isdigit() for ch in w) and len(w) >= 4 and not _yearish(w)}
        if (a and b and len(a & b) / len(a | b) >= 0.45) or ids:   # mostly the same words, or the same flight / confirmation / record id
            if r['state'] in OPEN_STATES:
                return r['key'], r['state']
            if closed[0] is None:
                closed = (r['key'], r['state'])
    return closed


def _note_email(payload: dict, obs: dict):
    """Remember the newest email seen for this item: the "did they already answer?" check must use that one."""
    if obs.get('source') != 'email':
        return
    cand = {'mailbox': obs.get('mailbox'), 'message_id': str(obs.get('id', '')).split(':', 2)[-1], 'thread_id': obs.get('thread_id'),
            'when': str(obs.get('when') or '')}
    old = payload.get('latest_email') or {}
    if not old or cand['when'] >= str(old.get('when') or ''):
        payload['latest_email'] = cand


def learned_in_prompts() -> bool:
    """Switch file `learn-in-prompts` in the life folder. Without it the learning steps only write their own files
    (what they said, people pages, the nightly page) and every prompt stays exactly as it was. With it, what was
    learned is shown to the noticer and the worker."""
    return True   # always on: the learn-in-prompts switch is folded in


def prefs_for_prompts() -> str:
    """What the owner told Zeus (prefs.py). With the switch on: their standing rules first, then that, then the short
    "how we work together" page. Any trouble with the new parts means the old text, unchanged."""
    text = F.as_text(LIFE)
    if not learned_in_prompts():
        return text
    try:
        rules = F.rules_text(LIFE)
        page = __import__('together').as_text(LIFE)
    except Exception:
        return text
    settled = ''
    if (LIFE / 'conflicts-on').exists():     # conflicts.py switch file; without it the text is exactly as before
        try:
            settled = __import__('conflicts').prompt_text(LIFE)
        except Exception:
            settled = ''
    return '\n'.join(x for x in (rules, text, page, settled) if x)


def _copy_of(con, row, obs, opp):
    """tick.upsert with the switch commit-integrity-on, before the intake: is this mail the very words an item already
    holds (the same notice in a second mailbox, a resend)? -> that item's row, or None. The item is looked for the way it
    always was (its key, else same_thing); the test itself is exact: the content key fresh.py works out in code is
    already on that item (so it needs fresh-on, as the drop always did). Only fresh.arrival's own same-content drop acts
    on the answer; it changes no title, plan, time or newest-email mark. Any trouble here means None: the intake decides."""
    try:
        if obs.get('source') != 'email' or not FR.on(LIFE):
            return None
        if row is None:
            twin, _ = same_thing(con, opp)
            row = con.execute('SELECT * FROM opps WHERE key=?', (twin,)).fetchone() if twin else None
        if row is None or row['state'] == 'skipped':
            return None
        held = (json.loads(row['payload']).get('fresh') or {}).get('contents') or []
        return row if FR.content_keys(obs)[0] in held else None
    except Exception:
        return None


def upsert(con, obs, opp, decision):
    now = time.time()
    row = con.execute('SELECT * FROM opps WHERE key=?', (opp['dedupe_key'],)).fetchone()
    link, ci = None, CI.on(LIFE)   # integrity.py (switch commit-integrity-on): only the very same source is folded into an item; similar words or a reused key only suggest a link. Without the switch the lines below are exactly what they were
    if ci:
        copy = _copy_of(con, row, obs, opp)   # the very same words delivered again (another mailbox, a resend) are dropped first, as they always were: that drop merges nothing
        if copy is not None and FR.arrival(con, copy, json.loads(copy['payload']), json.loads(copy['sources']), obs, opp) == 'duplicate':
            return 'duplicate'
        row, opp, link = CI.intake(con, LIFE, obs, opp, row, same_thing)
    elif row is None:
        twin, _ = same_thing(con, opp)
        if twin:
            row = con.execute('SELECT * FROM opps WHERE key=?', (twin,)).fetchone()
    if row is not None and row['state'] != 'skipped':
        srcs = json.loads(row['sources'])
        old = json.loads(row['payload'])
        # Nothing stale (fresh.py, switch fresh-on; None = carry on exactly as before): the same content delivered again
        # is not news, and a thing they dropped or handled reopens only when it really changed.
        apart = ci and CI.as_told(LIFE, row, old)   # integrity.py, for the fold decision only: an item waiting for a receipt, and one whose note may not have reached them and that asks them nothing, count as already told, as they are without the switch; False without the switch
        again = FR.arrival(con, {**dict(row), 'state': 'done'} if apart else row, old, srcs, obs, opp)   # set apart: weighed as news about something already told, as it would be without the switch
        if again == 'duplicate':
            return 'duplicate'
        if row['state'] in OPEN_STATES and not apart and not __import__('glue').watch_apart(con, row, old, obs):   # glue.py: a watched page's change is never folded silently into a worked item
            merged = {**old, **{k: v for k, v in opp.items() if v not in (None, '', []) and k not in KEEP_ON_MERGE and k != 'dedupe_key'}}
            merged = CM.merge_kept(LIFE, old, merged, obs, opp)   # commit.py (switch commit-on): a job they corrected keeps its own title, reason, steps and times; any other item gets the same dict back
            _note_email(merged, obs)
            if __import__('timing').on(LIFE):   # timing.py (switch merge-timing-on): an urgent update moves the due time earlier; a verified reschedule moves it either way
                return _merge_timed(con, row, obs, opp, decision, merged, srcs)
            con.execute('UPDATE opps SET payload=?, sources=?, updated=?, urgency=?, action_class=? WHERE key=?',
                        (json.dumps(merged, default=str), json.dumps(sorted(set(srcs) | {obs['id']})), now, opp.get('urgency'),
                         opp.get('action_class'), row['key']))
            CM.merge_noted(con, LIFE, row['key'], obs, opp)   # commit.py (switch commit-on): what the new entry says goes on the commitment as a fact; a clash with their correction is noted
            __import__('glue').watch_landed(con, row['key'], obs)   # glue.py (watches-on + fresh-on): a result built before that change goes back for fresh work
            return 'updated'
        # Already closed (told, dropped or no longer needed). Another copy of the same thing is not news.
        if again != 'reopen' and (obs['id'] in srcs or (obs.get('source') == 'calendar' and any(str(s).startswith('calendar:') for s in srcs))):
            con.execute('UPDATE opps SET sources=? WHERE key=?', (json.dumps(sorted(set(srcs) | {obs['id']})), row['key']))
            return 'duplicate'
        # News about something already handled: a fresh item that knows its history, so only the change is told.
        key = f"{row['key']}~u{hashlib.sha1(str(obs['id']).encode()).hexdigest()[:6]}"
        if con.execute('SELECT 1 FROM opps WHERE key=?', (key,)).fetchone():
            return 'duplicate'
        opp = {**opp, 'dedupe_key': key, 'update_of': row['key'],
               'earlier': {'how_it_ended': row['state'], 'what_he_was_told': (old.get('packet') or {}).get('headline'),
                           'why_closed': (old.get('closed') or {}).get('why')}}
        __import__('glue').watch_earlier(opp, row, old)   # glue.py: an update set apart from a thing still OPEN says so truthfully
        if apart and row['state'] == 'surfaced':   # the note about the earlier thing may not have reached them: the update's job is not told that they were told, or asked
            opp['earlier'] = {'how_it_ended': 'not yet; a note about it went out and it is not known whether it reached them', 'what_he_was_told': None,
                              'why_closed': 'nothing yet. Your result so far: ' + (str((old.get('packet') or {}).get('headline') or '').strip() or '(none)')}
    state = 'skipped' if decision['autonomy'] == 'skip' else 'pending'
    payload = {**opp, 'decision': decision, '_source': obs.get('source')}
    FR.stamp(con, payload, obs)   # fresh.py: the version of what this item was noticed from (nothing without its switch files)
    if obs.get('source') == 'email':
        payload['thread'] = {'mailbox': obs.get('mailbox'), 'thread_id': obs.get('thread_id')}
        _note_email(payload, obs)
    con.execute('INSERT OR REPLACE INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)',
                (opp['dedupe_key'], state, decision['autonomy'], decision['surface_at'], opp.get('urgency'), opp.get('action_class'),
                 json.dumps(payload, default=str), json.dumps([obs['id']]), now, now))
    if ci:
        CI.born(con, LIFE, opp['dedupe_key'], obs, link)   # integrity.py: the new item gets its commitment with the source it came from, and a suggested link is written down (nothing is merged)
    return 'new'



def _merge_timed(con, row, obs, opp, decision, merged, srcs, tries=3):
    """tick.upsert's merge with timing.py (switch merge-timing-on): the due time follows the update's kind, and the
    write is a compare-and-set on `updated`, so a change another writer made in between is merged, not lost."""
    import timing as TM
    for _ in range(tries):
        before = json.loads(row['payload'])                 # what the item had seen BEFORE this arrival was noted
        try:
            new_seen = FR.stamp_of(obs)
        except Exception:
            new_seen = None
        from ownertime import PACIFIC as here
        now_dt = datetime.now(here)
        move = TM.classify(row['surface_at'], before, new_seen, opp, decision, now_dt)
        move = CM.merge_move(LIFE, before, move, opp)   # commit.py (switch commit-on): a time the new entry carries does not move a job they corrected; otherwise the same move comes back
        due = row['surface_at']
        if move.get('to') and move['to'] != due:
            TM.apply_to_payload(merged, move['to'])
            TM.log(merged, due, move, (new_seen or {}).get('v'))
            due = move['to']
        now = time.time()
        cur = con.execute('UPDATE opps SET payload=?, sources=?, updated=?, urgency=?, action_class=?, surface_at=? WHERE key=? AND updated IS ?',
                          (json.dumps(merged, default=str), json.dumps(sorted(set(srcs) | {obs['id']})), now, opp.get('urgency'),
                           opp.get('action_class'), due, row['key'], row['updated']))
        if cur.rowcount == 1:
            CM.merge_noted(con, LIFE, row['key'], obs, opp)   # commit.py (switch commit-on): as in upsert
            __import__('glue').watch_landed(con, row['key'], obs)
            return 'updated'
        row = con.execute('SELECT * FROM opps WHERE key=?', (row['key'],)).fetchone()   # someone else wrote it: merge again onto theirs
        if row is None or row['state'] not in OPEN_STATES:
            return 'duplicate'
        old = json.loads(row['payload'])
        srcs = json.loads(row['sources'])
        merged = {**old, **{k: v for k, v in opp.items() if v not in (None, '', []) and k not in KEEP_ON_MERGE and k != 'dedupe_key'}}
        merged = CM.merge_kept(LIFE, old, merged, obs, opp)   # commit.py (switch commit-on): as in upsert
        _note_email(merged, obs)
    raise RuntimeError(f"merge kept losing a race for {row['key'] if row else '?'}")


def notice(con, report, now_dt, args, cap, days, only):
    import noticer as N
    import sources as S
    grants = P.Grants(LIFE / 'grants.json')
    muted = F.muted_families(LIFE)
    prefs_text = prefs_for_prompts()
    cal, meta = S.calendar_observations()
    report['calendar'] = meta
    # "Could not read" is not "nothing there": when a calendar failed or the copy is old, the noticer is told so in one
    # plain sentence. When everything was read the sentence is empty and the prompt is exactly what it was.
    try:
        coverage = H.coverage_sentence(H.calendar_state(meta, H.read(LIFE), now_dt))
    except Exception as e:
        coverage = ''
        report['errors'].append(f'calendar coverage: {type(e).__name__}')
    queue = []
    if only in (None, 'calendar'):
        queue += [o for o in cal if o.get('status') != 'cancelled']
    if only in (None, 'email'):
        for row in S.email_candidates(days):
            if (LIFE / 'urgent-fallback-on').exists() and 'sender' in row.keys() and settings.AGENT_EMAIL and settings.AGENT_EMAIL.lower() in str(row['sender']).lower(): continue   # switch urgent-fallback-on: Zeus's own stand-in email in the owner's inbox is never read back as new mail  # noqa: E701
            queue.append({'_row': row, 'id': f"email:{row['mailbox']}:{row['message_id']}", 'fp': None})
    if only is None and '--dry' not in args:    # watches (switch file watches-on): code re-read what was due; each change is one more observation, first in line
        queue[:0] = __import__('watches').due(con, LIFE, now_dt, report)
    for item in queue:
        if report['calls'] >= cap:
            report['deferred'] = True
            break
        prior = con.execute('SELECT fp FROM seen WHERE id=?', (item['id'],)).fetchone()
        if prior and (item['fp'] is None or prior['fp'] == item['fp']):
            continue
        if '_row' in item:
            try:
                obs = S.email_observation(item['_row'])
            except Exception as e:  # a failed read is never "nothing there"; retried next tick
                report['errors'].append(f"read {item['id']}: {str(e)[:80]}")
                continue
            if obs is None:
                continue
            try:    # code check for text written to steer an assistant (screen.py); a screen bug never stops the tick
                import screen as X
                found = X.scan(obs.get('subject'), obs.get('text'))
                if found['flag']:
                    obs['screen_warning'] = X.warning(found)
                    report.setdefault('screened', []).append(obs['id'])
            except Exception as e:
                report['errors'].append(f"screen {item['id']}: {type(e).__name__}")
        else:
            obs = item
        if '--dry' in args:
            report.setdefault('would_notice', []).append(obs['id'])
            continue
        ctx = {'now': now_dt.isoformat(), 'open': open_opps(con), 'prefs': prefs_text, 'handled': recently_handled(con),
               'nearby_calendar': S.nearby(cal, obs.get('start') or obs.get('when') or now_dt.isoformat())}
        if learned_in_prompts():
            try:    # who is named in this item and what they have said about them; if this breaks, noticing goes on without it
                import people
                ctx['people'] = people.known_people(LIFE, people.text_of(obs), named_only=True)
            except Exception:
                ctx['people'] = []
        if coverage:
            ctx['coverage'] = coverage
        res = N.notice(__import__('ownertime').with_local_times(obs, ctx.get('nearby_calendar')), ctx)   # no owner-zone.json: obs itself
        report['calls'] += 1
        if res is None:
            report['errors'].append(f"model {obs['id']}")
            report['model_failed'] = report.get('model_failed', 0) + 1   # neither the model nor its backup answered
            continue
        report['cost'] += res.get('cost') or 0
        opps = N.check(obs, res['opportunities'], ctx)
        raised = 0
        for o in opps:
            if obs.get('source') == 'calendar':
                st = obs.get('start')
                if obs.get('all_day') and st:
                    st = str(st)[:10] + 'T12:00:00-08:00'  # all-day items: midday, not midnight
                o.setdefault('event_start', st)
            else:
                o['owner_involved'] = N.whose(obs, o)  # addressed to the owner: theirs. Only copied: the noticer's reading stands
                if obs.get('screen_warning'):
                    o['suspicious_mail'] = True         # worker: no tool-holding job is started from this email
            decided = P.decide(o, grants, now_dt, muted=muted)
            change = upsert(con, obs, o, decided)
            report['new_opps'] += change == 'new'
            raised += decided['autonomy'] != 'skip'
        con.execute('INSERT OR REPLACE INTO seen VALUES(?,?,?,?,?)', (obs['id'], obs.get('fp'), time.time(), len(opps), res.get('cost')))
        con.commit()


def watchdog(con, now_dt, report):
    """Nothing silently stuck: an item an hour past due that still has not been told (and is not just waiting out quiet
    hours or a switched-off sender) is reported, and raises one repair ticket per item per day."""
    if __import__('ownertime').alert_open(now_dt, life=LIFE) > now_dt or not (LIFE / 'notify-live').exists():
        return                                   # nothing can be sent now anyway; overdue is judged in the day
    cutoff = (now_dt - timedelta(minutes=60)).isoformat()
    today = now_dt.date().isoformat()
    stuck = []
    for r in con.execute("SELECT key,state,urgency,payload FROM opps WHERE state IN ('pending','executing','ready') AND surface_at<=?", (cutoff,)).fetchall():
        if __import__('signedin').waited_for_signin(json.loads(r['payload']), now_dt):
            continue                             # held for a sign-in (or just let go after one): late is counted from then, like an item held for the morning
        if now_dt.hour == 9 and r['state'] == 'ready':
            continue                             # held overnight; this hour's note is carrying it
        stuck.append(r['key'])
        opp = json.loads(r['payload'])
        if opp.get('alarmed_day') != today:
            W._alarm(LIFE, r['key'], f"Zeus is over an hour late telling {settings.OWNER_FIRST} about: {opp.get('title')}", kind='life_item_stuck', detail=f"item state: {r['state']}")
            opp['alarmed_day'] = today
            con.execute('UPDATE opps SET payload=? WHERE key=? AND state=?', (json.dumps(opp, default=str), r['key'], r['state']))
    if stuck:
        report['overdue'] = stuck


STUCK_AFTER = 30 * 60


def _probe(fn):
    """-> (what the source reported, '') or (None, why it could not be read). A probe never stops a tick."""
    try:
        return fn(), ''
    except Exception as e:
        return None, f'{type(e).__name__}: {str(e)[:120]}'


def health_alarms_on() -> bool:
    """Switch file `health-alarms-on` in the life folder. Without it the health note is written exactly the same, but
    none of its own repair alarms is raised (lifehealth.alarms_due): the note lists them under `alarms_held`
    instead. The engine's other alarms (a stuck item, a failed job, a note that could not be settled) do not look at
    this switch. Off by default."""
    return (LIFE / 'health-alarms-on').exists()


def health_note(now_dt, report=None, *, off=False, live=False, noticer_ran=False, settled=None, crashed=None,
                calendar=None, inbox=None, alarm=None) -> dict:
    """Write <LIFE_HOME>/health.json for this run and raise the tickets that are due (lifehealth.py has the rules),
    the tickets only when the switch file health-alarms-on is present (health_alarms_on).
    Runs on every tick, also with `off` present; then it only writes the note: off is the owner's stop, so no ticket.
    `calendar`, `inbox` and `alarm` are stand-ins for tests. Never raises: a broken health note must not stop a tick
    or hide a crash, so trouble here is written into the note itself."""
    report = report or {}
    try:
        prev = H.read(LIFE)
        meta, cal_err = (report['calendar'], '') if isinstance(report.get('calendar'), dict) else \
            _probe(calendar or (lambda: __import__('sources').calendar_observations()[1]))
        feed, feed_err = _probe(inbox or (lambda: __import__('sources').inbox_feed()))
        N = sys.modules.get('noticer')                      # not loaded = no model call was made on this run
        note = H.build(LIFE, now_dt, off=off, live=live, prev=prev, calendar_meta=meta, calendar_error=cal_err, inbox=feed,
                       inbox_error=feed_err, noticer_ran=noticer_ran, tried=int(report.get('calls') or 0),
                       failed=int(report.get('model_failed') or 0), model_stats=N.stats() if N and hasattr(N, 'stats') else None,
                       settled=settled, crashed=crashed)
        if 'people' in report:
            part = report['people']
            note['people'] = {'ok': not bool(part.get('errors') or part.get('retry_pending')), 'errors': part.get('errors', []),
                              'retry_pending': part.get('retry_pending', 0), 'waiting': part.get('rows_waiting', 0), 'folded': part.get('rows_folded', 0)}
            if note['people']['ok']:
                note['alarms'].pop('people_extraction', None)
        elif 'people' in prev:
            note['people'] = prev['people']
        missing = (report.get('research_delivery') or {}).get('missing_decisions')
        if missing:
            note['delivery_decisions'] = {'last_failure': now_dt.isoformat(), 'items': missing}
        elif prev.get('delivery_decisions'):
            note['delivery_decisions'] = prev['delivery_decisions']
        alarms_on = health_alarms_on()
        note['switches'].update({'health-alarms-on': alarms_on})   # not part of lifehealth.SWITCHES
        __import__('watches').health_part(LIFE, note)    # only with the switch files watches-on / signin-check-on: what they could not read
        __import__('glue').health_part(LIFE, note)   # glue.py: the other glue switch files that are present (none present: nothing added)
        CM.health_part(LIFE, note)   # commit.py: only with the switch file commit-on, a small `commitments` part
        __import__('relevance').health_part(LIFE, note)   # relevance.py: a small `deliveries` part (the relevance-on switch is folded in)
        __import__('memsync').health_part(LIFE, note, prev, now_dt, alarm or W._alarm, alarms_on)   # memsync.py: only with the switch file claims-on, a `memory_sync` part and its one ticket
        __import__('capability').health_part(LIFE, note, now_dt)   # capability.py: only with the switch file capability-on, a `capabilities` part; with neither switch nothing is added
        if (LIFE / 'order-watch-on').exists():   # a small `orders` part; without the switch file the note is exactly what it was
            __import__('orderwatch').health_part(LIFE, note)
        if (LIFE / 'pages-fresh-on').exists():   # a small `pages_fresh` part (what could not be read); without the switch file the note is exactly what it was
            __import__('pagesfresh').health_part(LIFE, note)
        if (LIFE / 'refund-watch-on').exists():   # a small `refunds` part; without the switch file the note is exactly what it was
            __import__('refundwatch').health_part(LIFE, note)
        due = H.alarms_due(note, now_dt)
        if due and not alarms_on:       # switch absent: say what is wrong in the note, raise nothing, count nothing as raised
            note['alarms_held'] = [a['condition'] for a in due]
            due = []
        for a in due:
            try:
                took = (alarm or W._alarm)(LIFE, 'health:' + a['condition'], a['title'], kind=a['kind'], detail=a['detail'])
            except Exception as e:
                took = False
                note.setdefault('alarm_errors', []).append(f"{a['condition']}: {type(e).__name__}")
            if took is False:      # the alarm could not be recorded or handed off: not counted as today's ticket, so the next tick tries again
                note.setdefault('alarm_errors', []).append(f"{a['condition']}: the alarm was not taken")
            else:
                H.mark_raised(note, a['condition'], now_dt)
    except Exception as e:
        note = {'when': now_dt.isoformat(), 'when_ts': now_dt.timestamp(), 'status': 'off' if off else 'ran', 'off': bool(off),
                'mode': 'live' if live else 'shadow', 'error': f'health note could not be worked out: {type(e).__name__}: {str(e)[:160]}'}
    try:
        H.write(LIFE, note)
    except OSError as e:
        print(f'health note could not be written: {e}', file=sys.stderr)
    return note


def off_run(now_dt, live, *, line=None, **stand_ins) -> dict:
    """What a tick still does with `off` present. off is the owner's stop and stays a true stop: nothing is noticed,
    worked, written or sent, no job is started and no ticket is raised. Two things only:
    (a) a note that was already on its way when the owner said stop is settled by reading back, never by sending again;
    (b) the health note is written, so "off was left on" can be seen instead of looking like a healthy quiet tick."""
    settled = None
    if (LIFE / 'ledger.sqlite3').exists():
        try:
            with closing(db()) as con:
                settled = notifier.settle_only(con, now_dt, LIFE, line=line)
        except Exception as e:
            settled = {'error': f'{type(e).__name__}: {str(e)[:160]}'}
    return health_note(now_dt, None, off=True, live=live, settled=settled, **stand_ins)


def acquire(life_home: Path):
    """One tick at a time. Writing the note now takes a few model turns, so a tick can outlast the 5-minute schedule;
    two at once could each write and send the same items. -> the open lock file (keep it until exit), or None when
    another tick holds it. The lock dies with its process, so it cannot go stale."""
    import fcntl
    life_home.mkdir(parents=True, exist_ok=True)
    fh = open(life_home / 'tick.lock', 'a+')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(time.time()))
    fh.flush()
    return fh


def held_for(life_home: Path) -> float:
    """Seconds the current holder has had the lock (0 if unknown)."""
    try:
        return max(0.0, time.time() - float((life_home / 'tick.lock').read_text().strip() or 0))
    except (OSError, ValueError):
        return 0.0


def main():
    args = sys.argv[1:]
    cap = int(args[args.index('--max-calls') + 1]) if '--max-calls' in args else 25
    days = float(args[args.index('--email-days') + 1]) if '--email-days' in args else 2
    only = args[args.index('--only') + 1] if '--only' in args else None
    off = (LIFE / 'off').exists()
    lock = acquire(LIFE)
    if lock is None:
        if off:   # another tick is still finishing; off is the owner's stop, so this one neither waits nor complains
            print(json.dumps({'status': 'off', 'busy': True}))
            return
        age = held_for(LIFE)
        print(json.dumps({'status': 'busy', 'previous_tick_running_s': round(age)}))
        if age > STUCK_AFTER:   # a tick that never ends would stop the pipes without a sound: fail loudly so the scheduler's alerting hears
            print(f'life tick has been running for {round(age / 60)} minutes; newer ticks cannot start', file=sys.stderr)
            sys.exit(1)
        return
    now_dt = datetime.now(P.PACIFIC)
    live = (LIFE / 'notify-live').exists()
    if off:
        off_run(now_dt, live)
        print(json.dumps({'status': 'off'}))
        return
    report = {'at': now_dt.isoformat(), 'engine': 'v2', 'mode': 'live' if live else 'shadow', 'calls': 0, 'cost': 0.0, 'new_opps': 0,
              'errors': [], 'sends': 0}
    crashed = []   # a phase that blew up: the other phases still run, then the job exits 1 so the scheduler's alerting hears
    noticing = '--no-notice' not in args and '--dry' not in args
    try:
        run_phases(args, cap, days, only, now_dt, live, report, crashed)
    except BaseException as e:   # not one phase but the tick itself (a locked ledger): say so in the note, then fail as before
        crashed.append(f'tick: {type(e).__name__}: {str(e)[:160]}')
        raise
    finally:       # the health note is written whatever happened above, a crash included; it never raises
        health_note(now_dt, report, live=live, noticer_ran=noticing, crashed=crashed)
    print(json.dumps({k: report.get(k) for k in ('engine', 'mode', 'calls', 'new_opps', 'surfaced', 'sends')} | {'errors': len(report['errors'])}))
    if crashed:
        sys.exit(1)


def run_phases(args, cap, days, only, now_dt, live, report, crashed):
    """The tick itself: learn, notice, work, deliver, watch. It sits outside main() so the health note can be
    written after it whatever happens."""

    def phase(name, fn):
        try:
            return fn()
        except Exception as e:
            import traceback
            crashed.append(f'{name}: {type(e).__name__}: {str(e)[:160]}')
            traceback.print_exc(file=sys.stderr)
            return None

    if '--dry' not in args:   # what the owner asked Zeus to forget leaves this engine's stores before any phase reads them
        swept = phase('forget', lambda: __import__('claims').forget_sweep(LIFE, locked=True))
        if swept and swept.get('removed'):
            report['forget'] = swept['removed']
    with closing(db()) as con:
        if '--dry' not in args:
            # The owner is on another clock (owner-zone.json, written by `life zone`): the stored times of open items follow
            # it. With no such file this does nothing. Own guard.
            try:
                moved = __import__('ownertime').settle(con, LIFE)
                if moved:
                    report['zone'] = moved
            except Exception as e:
                report['zone'] = {'error': f'{type(e).__name__}: {str(e)[:160]}'}
        research_on = (LIFE / 'research-flow-on').exists()   # research.py (one switch): research, select, deliver with one evaluator step. Without the file every line below is what it was
        if research_on and '--dry' not in args:   # monitor.py, trips: the next active leg of each watched trip is read and the flight rulebook applied; what qualifies goes out with the code texts just below. Never sends by itself
            flown = phase('trips', lambda: __import__('monitor').tend_trips(con, LIFE, now_dt))
            if flown:
                report['trips'] = flown
        if (LIFE / 'watches-on').exists() and '--dry' not in args:   # monitor.py: monitors are read, and verified news sent, before any model call
            phase('monitors', lambda: __import__('watches').monitors_first(con, LIFE, now_dt, report, live))
        if (LIFE / 'commit-on').exists() and '--dry' not in args:   # commit.py: a monitor whose start time has come is started by code, monitors are renewed, watches are checked against their jobs; before any model call
            kept = phase('commitments', lambda: CM.due_actions(con, LIFE, now_dt))
            if kept:
                report['commitments'] = kept
        if CI.on(LIFE) and '--dry' not in args:   # integrity.py (switch commit-integrity-on): every open item has a commitment that names its source, and an item waiting for a receipt is looked at again; before any model call and before fresh.tend. Never sends
            whole = phase('integrity', lambda: CI.tend(con, LIFE, now_dt))
            if whole:
                report['integrity'] = whole
        if '--no-learn' not in args and '--dry' not in args:
            # Learning from the owner's chats. Each step is free when nothing is new, never sends, and has its own
            # guard: a failure is written into the report and never stops the notice, the work, the note or the watchdog.
            try:
                con.commit()   # let go of the ledger first: the pass adds a promise through its own connection
                learn = __import__('quietpass').run(now_dt, LIFE, max_conversations=1)
                report['cost'] += learn.get('cost') or 0
                report['learn'] = {k: learn[k] for k in ('status', 'calls', 'learned', 'dropped', 'waiting', 'left_for_next_run', 'errors', 'stuck')
                                   if learn.get(k)}
            except Exception as e:
                report['learn'] = {'errors': [f'{type(e).__name__}: {str(e)[:160]}']}
            try:
                grew = __import__('people').update(LIFE)
                if grew.get('rows_new') or grew.get('errors') or grew.get('retry_pending'):
                    report['people'] = {k: grew.get(k) for k in ('rows_new', 'calls', 'rows_folded', 'rows_waiting', 'retry_pending', 'people_new', 'people_changed', 'errors') if grew.get(k)}
                report['cost'] += grew.get('cost') or 0
            except Exception as e:
                report['people'] = {'errors': [f'{type(e).__name__}: {str(e)[:160]}']}
            if report.get('people', {}).get('errors') or report.get('people', {}).get('retry_pending'):
                report['errors'].append('people: local people data could not be updated')
        if '--no-notice' not in args and research_on and '--dry' not in args:   # research.py: watches are still re-read by code; this hour's research run (bundle, research, select) is started once, in its own process. No per-item noticer call
            found = phase('research_research', lambda: __import__('research').tick_research(con, LIFE, now_dt, report))
            if found:
                report['research_research'] = found
        elif '--no-notice' not in args:
            phase('notice', lambda: notice(con, report, now_dt, args, cap, days, only))
            __import__('capability').after_notice(LIFE, report, crashed, now_dt)   # capability.py (switch capability-on): which source this pass could not read, and since when; nothing without the switch
        if (LIFE / 'travel-ask-on').exists() and '--dry' not in args:
            # travel.py (also needs zone-on): a confirmed trip on the owner's own calendar becomes ONE ask the
            # day before they leave, as an ordinary item the note carries. Never sends, never sets the clock. Own guard.
            trip = phase('travel', lambda: __import__('travel').tend(con, LIFE, now_dt))
            if trip:
                report['travel'] = trip
        if (LIFE / 'loops-on').exists() and '--dry' not in args:
            # loops.py: who is waiting on the owner, what they promised, who owes them an answer. A few thread
            # reads a tick; each line worth telling becomes its own item (no daily list). Never sends. Own guard.
            looped = phase('loops', lambda: __import__('loops').tend(con, LIFE, now_dt, discover=not research_on))
            if looped:
                report['loops'] = looped
        if FR.on(LIFE) and '--dry' not in args:
            # Nothing stale (fresh.py): an ask past its own deadline ends, asks already put to the owner are compared with
            # their source, and a result that slept is sent back for one fresh look. Never sends.
            tended = phase('fresh', lambda: FR.tend(con, LIFE, now_dt))
            if tended:
                report['fresh'] = tended
        if '--dry' not in args:   # recover.py (the recover-on switch is folded in): an item whose sign-in came back goes back to work once, a standing OK the owner took back stops, and what recovery could not finish is put to them by the two-hour rule. Never sends.
            mended = phase('recover', lambda: __import__('recover').tend(con, LIFE, now_dt))
            if mended:
                report['recover'] = mended
        if (LIFE / 'order-watch-on').exists() and '--dry' not in args:   # orderwatch.py, every store order is followed until it arrives; at most ONE ordinary item per order per reason for the note below. Never sends. Own guard.
            ordered = phase('orders', lambda: __import__('orderwatch').tend(con, LIFE, now_dt))
            if ordered:
                report['orders'] = ordered
            # The owner's refill rule with the cart read first (orderwatch.refill_prepare). Right after the order tick, so
            # "has the owner ordered" is this run's own answer. One step and at most one page read a run; it does nothing
            # until the cart page is set up to be read (order-pages.json). Never sends, never checks out. Own guard.
            prepared = phase('refill', lambda: __import__('orderwatch').refill_prepare(con, LIFE, now_dt))
            if prepared:
                report['refill'] = prepared
        if (LIFE / 'refund-watch-on').exists() and '--dry' not in args:   # refundwatch.py, promised refunds and return deadlines are followed; at most ONE ordinary item per case per reason for the note below. Never sends. Own guard.
            refunds = phase('refunds', lambda: __import__('refundwatch').tend(con, LIFE, now_dt))
            if refunds:
                report['refunds'] = refunds
        work = phase('work', lambda: W.run(con, now_dt, LIFE, prefs_text=prefs_for_prompts()))
        if work is not None:
            report['work'] = work
        sends = phase('one_send', lambda: __import__('onesend').tend(con, LIFE, now_dt))   # switch file one-send-on; without it nothing happens
        if sends:
            report['one_send'] = sends
        if (LIFE / 'own-outcome-on').exists() and '--dry' not in args:   # scorecard.py: Monday's first run at or after 9am stores the week's scorecard as a record (nothing for them; no digests). Never sends
            card = phase('scorecard', lambda: __import__('scorecard').tend(con, LIFE, now_dt))
            if card:
                report['scorecard'] = card
        res = phase('deliver', lambda: notifier.deliver(con, now_dt, LIFE, live=live))
        if res is not None:
            report['delivery'] = res
            report['sends'] = res.get('sent', 0)
            report['surfaced'] = res.get('told', 0)
        if research_on and '--dry' not in args:   # research.py: a selected message whose time has come goes through code's gates and the evaluator (send or skip, one reason code, before any text), then the same claim / send / read-back
            told = phase('research_deliver', lambda: __import__('research').deliver(con, now_dt, LIFE, live=live))
            if told is not None:
                report['research_delivery'] = told
                report['sends'] = report.get('sends', 0) + told.get('sent', 0)
        report['notify_mode'] = 'live' if live else 'shadow'
        phase('watchdog', lambda: watchdog(con, now_dt, report))
        if '--dry' not in args:   # relevance.py (the relevance-on switch is folded in): chat asks are written down, an accepted note is read back, an unresolved delivery has its ticket. Never sends
            dv = phase('deliveries', lambda: __import__('relevance').tend(con, LIFE, now_dt, live))
            if dv:
                report['deliveries'] = dv
        if (LIFE / 'file-delivery-on').exists() and '--dry' not in args:   # filedeliver.py: a sent file's receipt is read from the send journal, a file that did not go is told once. Never sends. Own guard.
            filed = phase('files', lambda: __import__('filedeliver').tend(con, LIFE, now_dt, live))
            if filed:
                report['files'] = filed
        acted = phase('acted', lambda: __import__('actcheck').run(con, LIFE))   # what the chat Zeus really did after the owner's yes
        if acted:
            report['acted'] = acted
        if '--no-learn' not in args and '--dry' not in args:
            # How the owner writes, from their own sent mail (switch file style-on): at most once a week. Never sends. Own guard.
            try:
                ws = __import__('writingstyle').refresh(LIFE, now_dt)
                if ws.get('status') not in ('off', 'fresh', 'waiting', 'owner note stays'):
                    report['style'] = ws
                report['cost'] += ws.get('cost') or 0
            except Exception as e:
                report['style'] = {'status': 'error', 'why': f'{type(e).__name__}: {str(e)[:160]}'}
            # Nightly "how we work together" review: once a day, on the first tick after 8:30pm. Never sends. Own guard.
            try:
                tg = __import__('together').run(now_dt, LIFE)
                if tg.get('status') not in ('early', 'already', 'waiting'):
                    report['together'] = {k: v for k, v in tg.items() if k not in ('row', 'page')}
            except Exception as e:
                report['together'] = {'status': 'error', 'why': f'{type(e).__name__}: {str(e)[:160]}'}
            if (LIFE / 'conflicts-on').exists():
                # conflicts.py: once a night, after the page above, standing things that cannot both be
                # followed are settled by the owner's later written rule, or become ONE plain question (once a week at most, the
                # offers slot, its own line). Never sends. Own guard.
                cf = phase('conflicts', lambda: __import__('conflicts').run(con, LIFE, now_dt))
                if cf and cf.get('status') not in ('early', 'off'):
                    report['conflicts'] = {k: v for k, v in cf.items() if k != 'findings'}
        if '--dry' not in args:   # closure.py (the closure-on switch is folded in): once a day, the first run at or after 8:30pm the owner's time, after the passes above. Never sends
            shut = phase('closure', lambda: __import__('closure').run(con, LIFE, now_dt, report))
            if shut and shut.get('status') not in ('early', 'already', 'off'):
                report['closure'] = shut
        if (LIFE / 'pages-fresh-on').exists() and '--dry' not in args:   # pagesfresh.py, once a day (the first run at or after 6:00am the owner's time; a failed read tries again after 2 hours) new mail and CRM changes go onto the person and company pages. Never sends. Own guard.
            con.commit()   # let go of the ledger first (as before the learning pass): this pass reads mail and the CRM for up to a minute and never touches the ledger
            pf = phase('pages_fresh', lambda: __import__('pagesfresh').tend(LIFE, now_dt))
            if pf and pf.get('status') not in ('off', 'early', 'already', 'waiting', 'not seeded'):
                report['pages_fresh'] = pf
        if (LIFE / 'miss-review-on').exists() and '--dry' not in args:   # missreview.py, once a night after the page and the closing above: what was missed, late or out of order, with proof; at most ONE ordinary item, told on its own at the next daytime moment. Never sends
            missed = phase('miss_review', lambda: __import__('missreview').run(con, LIFE, now_dt, report))
            if missed and missed.get('status') not in ('early', 'already', 'off', 'waiting'):
                report['miss_review'] = missed
        report['errors'] += crashed
        con.execute('INSERT OR REPLACE INTO runs VALUES(?,?)', (time.time(), json.dumps(report, default=str)))
        con.commit()
    (LIFE / 'last-tick.json').write_text(json.dumps(report, indent=2, default=str))


if __name__ == '__main__':
    main()

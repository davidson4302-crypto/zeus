"""Health note ("loud when broken"): what the life pipes could and could not do on this run.

Without it a tick that ran fine but did nothing would look exactly like a healthy one: the noticer's model down, a
calendar that could not be read, a mail feed that had stopped, the `off` switch left in place. Each exits 0. Every tick writes <LIFE_HOME>/health.json (also with `off` present), and a few
conditions raise one repair ticket a day through the engine's own alarm path.

Three rules this file keeps:
- "Could not read" is never "nothing there". A calendar that failed, or a calendar copy that is old, becomes one
  plain sentence for the noticer (coverage_sentence). When everything reads fine the sentence is empty and nothing
  is added anywhere.
- This file reads nothing itself. tick.py hands it what the sources reported; it only works out the note.
- Nothing here can stop a tick. tick.py wraps every call.

Keys of health.json (plain on purpose; the alarm handler and the chat plugin read them):
  when, when_ts          this run
  status                 "ran" or "off"
  mode                   "live" or "shadow"
  off                    is the off switch present
  noticer                ran, tried, failed (this run), all_failed_runs (runs in a row where every call failed while
                         there was something to look at), all_failed_since
  models                 answered / failed per model for every model call of this run, both_failed, answered_by
  calendar               ok, checked_at, snapshot_age_seconds, stale, stale_since, stale_runs,
                         unreadable [{scope, stage, name, since}], not_ok_since
  inbox_feed             ok, readable, age_seconds, oldest_mailbox, mailboxes, stale, stale_since, stale_runs
  switches               which switch files exist
  alarms                 the last day each condition raised its ticket (at most one a day)
  settled                with `off`: what the read-back of an uncertain earlier note found
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import settings

PACIFIC = ZoneInfo(settings.OWNER_TZ)   # the owner's home zone (name kept for the code below)
FILE = 'health.json'
SWITCHES = ('off', 'notify-live', 'executor-off', 'learn-in-prompts', 'learn-off', 'guard-off', 'guard-shadow', 'chat-records-his-yes')
# Feature switches. Each is
# listed only while its file is there (the way glue.health_part lists its own), so a note with none of them is exactly what
# it was. A name that is not in the note is off.
LISTED_WHEN_ON = ('commit-on', 'watches-on', 'turnctx-on', 'research-scope-on', 'promise-check-on',
                  'merge-timing-on', 'pages-on', 'freshness-on', 'status-answer-on')
LISTED_WHEN_ON += ('timer-truth-on', 'order-watch-on', 'standing-ok-offer-on', 'pages-fresh-on', 'miss-review-on', 'on-it-line-on',
                   'file-delivery-on', 'stock-fallback-on', 'tapback-yes-on', 'draft-voice-on', 'refund-watch-on',
                   'inferred-work-on', 'cross-channel-fence-on', 'urgent-fallback-on', 'own-outcome-on')

# How often each feeder normally updates, and so when it counts as stale.
# Calendar: the calendar copy (config calendar.snapshot_file, written by whatever refreshes the owner's calendar) is
# expected to be refreshed at minute 0 of each hour in calendar.refresh_hours (owner's home clock; default every hour).
# The copy is stale when it is older than the run BEFORE the last one that should have finished: two runs missed in a
# row. One late or failed run is not stale, and a planned gap between refresh hours never is.
CALENDAR_RUN_HOURS = tuple(int(h) for h in (settings.get('calendar.refresh_hours', list(range(24))) or range(24)))
CALENDAR_RUN_SLACK = 30 * 60          # a run gets this long to finish before it counts as missed
# Mail: the mail feed checks each mailbox often (about every minute). The feed is stale when any mailbox this engine
# reads has had no successful check for mail.feed_stale_after_seconds (default 30 minutes), which a restart or a short
# nap of the machine never is.
INBOX_STALE_AFTER = int(settings.get('mail.feed_stale_after_seconds', 30 * 60))
STALE_RUNS_BEFORE_ALARM = 2           # stale on two runs in a row (about 5 minutes apart): a Mac that just woke is not an alarm
CALENDAR_UNREADABLE_ALARM = 2 * 3600  # a calendar that could not be read for longer than this raises a ticket
NOTICER_FAIL_RUNS = 3                 # runs in a row where the model and its backup failed on every call
DAY_STARTS, DAY_ENDS = settings.QUIET_END, settings.QUIET_START   # tickets only outside quiet hours, like the engine's watchdog


def read(life) -> dict:
    """The note the previous run left, or {} (no note yet, or it could not be read)."""
    try:
        d = json.loads((Path(life) / FILE).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def write(life, note: dict) -> None:
    """Whole file or nothing: written beside the real one, then moved over it in one step."""
    life = Path(life)
    life.mkdir(parents=True, exist_ok=True)
    tmp = life / f'.{FILE}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w') as fh:
            fh.write(json.dumps(note, indent=2, default=str))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, life / FILE)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def switches(life) -> dict:
    life = Path(life)
    return {name: (life / name).exists() for name in SWITCHES} | {name: True for name in LISTED_WHEN_ON if (life / name).exists()}


def _t(value):
    """An ISO time -> a datetime on the owner's home clock, or None when it cannot be read."""
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=PACIFIC)).astimezone(PACIFIC)


def _when(value) -> str:
    """'YYYY-MM-DDT18:00:21-08:00' -> 'Wed Nov 11 at 6:00pm'. The noticer is given `now`, so a full date is plain."""
    t = _t(value)
    if t is None:
        return 'an unknown time'
    clock = t.strftime('%I:%M%p').lstrip('0').replace('AM', 'am').replace('PM', 'pm')
    return t.strftime('%a %b %d').replace(' 0', ' ') + ' at ' + clock


def expected_calendar_runs(now_dt: datetime) -> tuple:
    """The two most recent calendar checks that should have finished by now -> (the last one, the one before it)."""
    now_dt = now_dt.astimezone(PACIFIC)
    limit = now_dt - timedelta(seconds=CALENDAR_RUN_SLACK)
    runs = []
    for back in range(0, 4):
        day = (now_dt - timedelta(days=back)).replace(minute=0, second=0, microsecond=0)
        runs += [day.replace(hour=h) for h in CALENDAR_RUN_HOURS]
    done = sorted((r for r in runs if r <= limit), reverse=True)
    return done[0], done[1]


def calendar_stale(checked_at, now_dt: datetime) -> bool:
    """Is the calendar copy older than two of its own hourly checks in a row? A copy with no readable time on it
    cannot be called fresh."""
    t = _t(checked_at)
    if t is None:
        return True
    return t < expected_calendar_runs(now_dt)[1]


ANY_CALENDAR = "any of the owner's calendars"


def _owner_s(cap: bool = False) -> str:
    """\"Alex's\" (or \"the owner's\"), for prompt sentences."""
    s = settings.OWNER_FIRST + "'s"
    return s[:1].upper() + s[1:] if cap else s


def plain_calendar(scope, stage='') -> str:
    """Which calendar, in words the owner could read. The calendar watch names a failure by the calendar's own name or
    address, or by an internal account label when a whole account failed; that label never leaves this file."""
    s = str(scope or '').strip()
    if stage in ('file', 'scan') or s.lower() == 'calendar connections':
        return ANY_CALENDAR
    if stage == 'inventory' or not s or s.startswith('googlecalendar_'):
        return "one of the owner's calendar accounts"
    if '@group.' in s or '#' in s:
        return "one of the owner's calendars"
    # A shared calendar is named by whoever owns it, and this name goes into a prompt: keep letters, digits and
    # ordinary name punctuation only, on one line, and short.
    s = ' '.join(re.sub(r"[^A-Za-z0-9 @._'&+-]", ' ', s).split())[:60].strip()
    if not s:
        return "one of the owner's calendars"
    if '@' in s and ' ' not in s:
        return f'the {s} calendar'
    return f'the "{s}" calendar'


def calendar_state(meta, prev: dict, now_dt: datetime) -> dict:
    """What sources.calendar_observations() reported, plus since when. `prev` is the previous health note: a calendar
    that was already failing keeps its "since", a new failure starts now. So "since" means "since at least"."""
    meta = meta if isinstance(meta, dict) else {}
    before = (prev or {}).get('calendar') or {}
    now_iso = now_dt.isoformat()
    since = {(u.get('scope'), u.get('stage')): u.get('since') for u in before.get('unreadable') or [] if isinstance(u, dict)}
    checked = meta.get('checked_at')
    no_copy = not checked and not meta.get('ok')              # the copy itself could not be opened
    failing = []
    if no_copy:
        failing.append(('snapshot', 'file'))
    else:
        for e in meta.get('errors') or []:
            failing.append((str(e.get('scope') or ''), str(e.get('stage') or '')) if isinstance(e, dict) else (str(e), ''))
        if not meta.get('ok') and not failing:
            failing.append(('', 'inventory'))                 # marked incomplete without saying which: still not "all read"
    unreadable = [{'scope': sc, 'stage': st, 'name': plain_calendar(sc, st), 'since': since.get((sc, st)) or now_iso}
                  for sc, st in dict.fromkeys(failing)]
    stale = (not no_copy) and calendar_stale(checked, now_dt)
    t = _t(checked)
    state = {'ok': bool(meta.get('ok')) and not stale and not unreadable,
             'checked_at': checked,
             'snapshot_age_seconds': round((now_dt - t).total_seconds()) if t else None,
             'stale': bool(stale),
             'stale_since': (before.get('stale_since') or now_iso) if stale else None,
             'stale_runs': (int(before.get('stale_runs') or 0) + 1) if stale else 0,
             'unreadable': unreadable}
    marks = [u['since'] for u in unreadable] + ([state['stale_since']] if stale else [])
    state['not_ok_since'] = min(marks, key=lambda v: _t(v) or now_dt) if marks else None
    return state


def _names(unreadable: list) -> list:
    return list(dict.fromkeys(u.get('name') for u in unreadable if u.get('name')))


def _join(parts: list) -> str:
    return parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]


def coverage_sentence(cal: dict) -> str:
    """One plain sentence for the noticer when calendar coverage is not whole: which calendar, since when, and that
    a gap on it is unknown, not free. '' when every calendar was read and the copy is fresh: then nothing is added
    to any prompt."""
    if not isinstance(cal, dict) or cal.get('ok') is not False:
        return ''
    unreadable = [u for u in cal.get('unreadable') or [] if isinstance(u, dict)]
    names = _names(unreadable)
    earliest = min((u.get('since') for u in unreadable if u.get('since')), key=lambda v: _t(v) or datetime.max.replace(tzinfo=PACIFIC), default=None)
    if ANY_CALENDAR in names:
        return (f"None of {_owner_s()} calendars have been readable since at least {_when(earliest)}, so their whole schedule is unknown right now: "
                "treat every gap as unknown, not free.")
    if cal.get('stale'):
        also = f", and {_join(names)} could not be read at all" if names else ''
        return (f"{_owner_s(True)} calendars were last read {_when(cal.get('checked_at'))} and could not be refreshed since{also}, so anything added, "
                "moved or cancelled after that is unknown: treat a gap as unknown, not free.")
    if not names:
        return ''
    what = _join(names)
    one = len(names) == 1
    return (f"{what[:1].upper()}{what[1:]} {'has' if one else 'have'} not been readable since at least {_when(earliest)}, so what is on {'it' if one else 'them'} is unknown: "
            "treat a gap there as unknown, not free.")


def _mail_configured() -> bool:
    """Mail off (no mailbox set up) is not a stale feed. Unknown counts as configured, so a real outage still alarms."""
    try:
        return bool(__import__('sources').MAIL_CONFIGURED)
    except Exception:
        return True


def inbox_state(feed, prev: dict, now_dt: datetime, error: str = '') -> dict:
    """`feed` = sources.inbox_feed(): {mailbox: time of its last successful check}. Not readable is not "fresh"."""
    before = (prev or {}).get('inbox_feed') or {}
    if not error and not feed and not _mail_configured():
        return {'ok': True, 'configured': False, 'readable': True, 'why': 'no mailbox configured', 'age_seconds': None,
                'oldest_mailbox': None, 'stale': False}
    now_iso, now = now_dt.isoformat(), now_dt.timestamp()
    if error or not isinstance(feed, dict) or not feed:
        state = {'ok': False, 'readable': False, 'why': (error or 'no mailbox is listed')[:160], 'age_seconds': None, 'oldest_mailbox': None, 'stale': True}
    else:
        ages = {}
        for box, at in feed.items():
            try:
                ages[str(box)] = max(0, round(now - float(at)))
            except (TypeError, ValueError):
                ages[str(box)] = None                         # never checked, or no time on record
        never = [b for b, a in ages.items() if a is None]
        oldest = never[0] if never else max(ages, key=ages.get)
        stale = bool(never) or ages[oldest] > INBOX_STALE_AFTER
        state = {'ok': not stale, 'readable': True, 'age_seconds': None if never else ages[oldest], 'oldest_mailbox': oldest,
                 'mailboxes': ages, 'stale': stale}
    stale = state['stale']
    state['stale_since'] = (before.get('stale_since') or now_iso) if stale else None
    state['stale_runs'] = (int(before.get('stale_runs') or 0) + 1) if stale else 0
    return state


def noticer_state(prev: dict, now_dt: datetime, ran: bool, tried: int, failed: int) -> dict:
    """tried / failed = this run's noticing calls (one per new email or event) and how many got no answer from the
    model or its backup. all_failed_runs counts runs in a row where there was work and every call failed. A run with
    nothing new to look at proves nothing either way, so it leaves the count alone."""
    before = (prev or {}).get('noticer') or {}
    streak, since = int(before.get('all_failed_runs') or 0), before.get('all_failed_since')
    if ran and tried > 0:
        if failed >= tried:
            streak, since = streak + 1, since or now_dt.isoformat()
        else:
            streak, since = 0, None
    return {'ran': bool(ran), 'tried': int(tried), 'failed': int(failed), 'all_failed_runs': streak, 'all_failed_since': since}


def models_state(stats) -> dict:
    """Which model answered this run's calls (noticer.stats()): the main model, its backup, both, or nobody."""
    stats = stats if isinstance(stats, dict) else {}
    answered = {k: v for k, v in (stats.get('answered') or {}).items() if v}
    return {'answered': answered, 'failed': {k: v for k, v in (stats.get('failed') or {}).items() if v},
            'both_failed': int(stats.get('no_answer') or 0),
            'answered_by': None if not answered else next(iter(answered)) if len(answered) == 1 else 'both'}


def build(life, now_dt: datetime, *, off: bool, live: bool, prev: dict, calendar_meta=None, calendar_error: str = '',
          inbox=None, inbox_error: str = '', noticer_ran: bool = False, tried: int = 0, failed: int = 0, model_stats=None,
          settled=None, crashed=None) -> dict:
    note = {'when': now_dt.isoformat(), 'when_ts': now_dt.timestamp(), 'status': 'off' if off else 'ran',
            'mode': 'live' if live else 'shadow', 'off': bool(off),
            'noticer': noticer_state(prev, now_dt, noticer_ran and not off, tried, failed),
            'models': models_state(model_stats),
            'calendar': calendar_state({'ok': False} if calendar_error else calendar_meta, prev, now_dt),
            'inbox_feed': inbox_state(inbox, prev, now_dt, inbox_error),
            'switches': switches(life),
            'alarms': dict((prev or {}).get('alarms') or {})}
    if calendar_error:
        note['calendar']['why'] = calendar_error[:160]
    if settled is not None:
        note['settled'] = settled
    if crashed:
        note['crashed_phases'] = [str(c)[:200] for c in crashed]
    return note


def alarms_due(note: dict, now_dt: datetime) -> list:
    """The silent cases worth one repair ticket: -> [{'condition', 'kind', 'title', 'detail'}]. At most one per
    condition per day (note['alarms'] remembers the day), daytime only, and never with `off` present: off is the
    owner's stop. If the alarm handler has to ask the owner, it reads `title` word for word, so each is plain words, whole, and short
    enough that the alarm path never has to shorten it."""
    if note.get('off') or not (DAY_STARTS <= now_dt.astimezone(PACIFIC).hour < DAY_ENDS):
        return []
    today = now_dt.astimezone(PACIFIC).date().isoformat()
    raised = note.get('alarms') or {}
    due = []

    def add(condition, kind, title, detail):
        if raised.get(condition) != today:
            due.append({'condition': condition, 'kind': kind, 'title': title, 'detail': detail})

    if (note.get('people') or {}).get('ok') is False and 'people_extraction' not in raised:
        add('people_extraction', 'life_people_extraction',
            "Zeus could not update its local people notes. The source is kept for another try.",
            ['The people processor reported an error.'])
    decisions = note.get('delivery_decisions') or {}
    last_failure = _t(decisions.get('last_failure'))
    if last_failure and last_failure.date().isoformat() > (raised.get('delivery_decisions') or ''):
        add('delivery_decisions', 'life_research_decision_failed',
            'Zeus could not decide whether to deliver a prepared update after its allowed tries.',
            ['Item: ' + str(item) for item in decisions.get('items', [])][:5])
    n = note.get('noticer') or {}
    if int(n.get('all_failed_runs') or 0) >= NOTICER_FAIL_RUNS:
        m = note.get('models') or {}
        add('noticer_down', 'life_noticer_down',
            'Zeus cannot notice new mail or calendar changes: its model and the backup both failed on every try, three checks in a row',
            [f"runs in a row with every call failed: {n.get('all_failed_runs')}, since {n.get('all_failed_since')}",
             f"this run: tried {n.get('tried')}, failed {n.get('failed')}; per model failed {m.get('failed')}, answered {m.get('answered')}"])
    box = note.get('inbox_feed') or {}
    if box.get('stale') and int(box.get('stale_runs') or 0) >= STALE_RUNS_BEFORE_ALARM:
        add('inbox_feed_stale', 'life_inbox_feed_stale',
            f"Zeus's mail feed has gone quiet: the inbox watch has not checked the owner's mailboxes for over {INBOX_STALE_AFTER // 60} minutes",
            [f"stale since {box.get('stale_since')}; oldest mailbox {box.get('oldest_mailbox')}, seconds since its last good check: {box.get('age_seconds')}",
             f"feed readable: {box.get('readable')} {box.get('why') or ''}".strip()])
    cal = note.get('calendar') or {}
    if cal.get('stale') and int(cal.get('stale_runs') or 0) >= STALE_RUNS_BEFORE_ALARM:
        add('calendar_stale', 'life_calendar_stale',
            'Zeus is working from an old copy of the calendars: the calendar check has missed its last two runs',
            [f"calendar copy last checked {cal.get('checked_at')} ({cal.get('snapshot_age_seconds')} seconds old); stale since {cal.get('stale_since')}"])
    long_gone = [u for u in cal.get('unreadable') or []
                 if (_t(u.get('since')) is not None and (now_dt - _t(u['since'])).total_seconds() > CALENDAR_UNREADABLE_ALARM)]
    if long_gone:
        names = _names(long_gone)
        which = _join(names) if len(' '.join(names)) <= 60 else f'{len(names)} calendars'
        add('calendar_unreadable', 'life_calendar_unreadable',
            f"Zeus has not been able to read {which} for over two hours",
            [f"{u.get('scope')} (stage {u.get('stage')}) unreadable since {u.get('since')}" for u in long_gone][:5])
    return due


def mark_raised(note: dict, condition: str, now_dt: datetime) -> None:
    note.setdefault('alarms', {})[condition] = now_dt.astimezone(PACIFIC).date().isoformat()

"""statusanswer: a grounded answer to "is everything working?".

Why: asked "is everything working?", "what do you do for me on your own?" or "are you watching my email?", the chat
model has nothing to go on. How fresh its email and calendar reads are, which stores signed it out, which jobs it runs
and when, and which standing OKs the owner gave are all on disk (health.json, the profile's cron/jobs.json,
grants.json, standing-rules.json), but none of it reaches the chat model, so it could only guess ("I'm watching your
email and calendar as normal") or say nothing. This answers the owner when they ask.

Switch file `status-answer-on` in the life folder. Without it nothing here is loaded and every turn and every reply
is byte for byte what it was.

  is_status_question(message, kind, ask_kind)
        A narrow trigger checked by code. The WHOLE message (lower case, punctuation gone) must be one of three shapes,
        with only a few filler words around it ("hey", "still", "right now", "on your end"):
          health     "is everything working", "are you running", "is anything broken", "did something break"
          own work   "what do you do for me on your own", "what runs in the background", "what are you watching"
          feeds      "are you watching my email", "is my calendar connected", "do you still see my inboxes"
        and the chat plan must not already know the message (chatplan.kind() == '' and the reply checks' ask_kind()
        == 'chat'), so a greeting, a rundown, a yes-or-no about what needs the owner, or "keep an eye on X" keep their
        own plans. Missing a status question costs only the plain behaviour; firing on "how are you" would cost a
        status speech on small talk. So: misses before false fires, and at most 16 words.

  block(life_home, jobs_file, now, tz)
        The plain-words lines the agent answers from, built in code from read-only files, never by the model:
          the bottom line (all running, or what is not), how long ago the owner's inboxes and calendar were read,
          whether its own look ran recently, signed-out stores and the fix (the sign-in link it texts the owner; some
          stores only that way, because they take the agent's own sign-in for a robot), the jobs it runs on its own
          under plain names with last and next run (a name code does not know, or one with machinery in it, is only
          counted), and the owner's standing OKs and the ways of working they set.
        Every line is checked: one carrying a path, a file name, an underscore name, brackets or a machinery word
        (the reply checks' own list) is left out.

  standing_feeds(life_home, now)
        For the promise check: the words a "watching" sentence may be about and still be true without a watch
        record, because the life engine reads the owner's email and calendar every few minutes on its own. Only while
        health.json says that feed was read in the last 15 minutes. "I'm watching your 3 inboxes" is then true and
        must not be rewritten into "That isn't set up".

Reading only: nothing here writes, sends or calls a model.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _settings():
    mod = sys.modules.get('settings')
    if mod is not None and hasattr(mod, 'OWNER_FIRST'):
        return mod
    spec = importlib.util.spec_from_file_location('settings', Path(__file__).resolve().parents[2] / 'engine' / 'settings.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return sys.modules.setdefault('settings', mod)


settings = _settings()
O = settings.OWNER_FIRST if settings.OWNER_FIRST != 'the owner' else 'the owner'      # how the lines name the owner
POSS = O + "'s"
AGENT = str(settings.AGENT_NAME or 'Zeus')

SWITCH = 'status-answer-on'
HEAD = 'WHAT YOU RUN ON YOUR OWN, AS CODE FOUND IT JUST NOW'
PACIFIC = ZoneInfo(settings.OWNER_TZ)          # the owner's home zone (name kept for importers)
MAX_WORDS = 16
LOOK_STALE = 15 * 60          # the life tick runs every 5 minutes; three missed looks is not "running"
INBOX_STALE = 30 * 60         # lifehealth.INBOX_STALE_AFTER
# how often the calendar copy refreshes, in words (calendar.refresh_hours, the same key lifehealth reads)
_CAL_EVERY = settings.get('calendar.refresh_hours', 1)
try:
    _CAL_EVERY = float(_CAL_EVERY)
except (TypeError, ValueError):
    _CAL_EVERY = 1.0
CAL_HOURS = 'every hour' if _CAL_EVERY <= 1 else f'every {_CAL_EVERY:g} hours'
# the owner's own names and the agent's name, as filler around a status question ("hey <agent>, is everything working")
_NAMES = '|'.join(sorted({re.escape(w) for w in ' '.join([str(settings.OWNER_NAME or ''), AGENT]
                                                           + [str(n) for n in (settings.get('owner.nicknames', []) or [])]).lower().split()
                          if w not in ('the', 'owner')}, key=len, reverse=True))

# ---- the trigger -----------------------------------------------------------------------------------------------------
PRE = (r"(?:(?:hey|hi|yo|ok|okay|so|and|also|btw|" + (_NAMES + '|' if _NAMES else '')
       + r"quick question|real quick|just checking|just curious|question|honestly)\s+)*")
POST = (r"(?:\s+(?:still|right now|now|ok|okay|properly|correctly|for me|on your end|on your side|over there|"
        + (_NAMES + '|' if _NAMES else '') + r"these days|at the moment|today|as usual|like normal|as normal|lol))*")
OWN = (r"(?:on your own|by yourself|automatically|without (?:me|asking|me asking)|in the background|behind the scenes|"
       r"on autopilot)")
FEED = (r"(?:my |all my |all of my |both my |the )?(?:emails?|e mails?|mail|inbox(?:es)?|gmail|mailboxes|calendars?|"
        r"emails? and (?:my )?calendars?|calendars? and (?:my )?emails?|inboxes and (?:my )?calendars?)")
CORES = (
    # health
    r"(?:is |are )?(?:everything|it all|all of it|all your stuff|all your jobs|things|stuff) (?:still )?"
    r"(?:working|running|connected|hooked up|online|up and running)(?: ok| okay| alright| fine| properly| right)?",
    r"(?:is |are )?(?:everything|all) (?:ok|okay|alright|all right|fine|good) (?:on your end|on your side|behind the scenes|"
    r"in the background|with your stuff|with your jobs)",
    r"(?:are|r) (?:you|u|your jobs|your checks|your automations?) (?:still )?(?:working|running|online|up and running)"
    r"(?: ok| okay| properly| fine| right)?",
    r"(?:is |are )?(?:anything|something) (?:broken|down|not working|stuck|failing|acting up)",
    r"(?:did|has) (?:anything|something) (?:break|broken|stop working|stopped working|go down|gone down|fail|failed)",
    r"(?:any|are there any) (?:problems|issues|errors) (?:on your end|on your side|with your stuff|with your jobs)",
    # what it does on its own
    r"what (?:all |exactly )?do you (?:do|handle|take care of|watch|run|keep an eye on|look after|check)"
    r"(?: for me)?(?: " + OWN + r")?",
    # "what can you do" is about what the agent is able to do, not what it is doing: only with "on your own" and the like
    r"what (?:all |exactly )?(?:can|will) you (?:do|handle|take care of|watch|run|keep an eye on|look after|check)"
    r"(?: for me)? " + OWN,
    r"what (?:all |exactly )?(?:are|do) you (?:handling|taking care of|watching|running|keeping an eye on|looking after|"
    r"checking|monitoring|tracking)(?: for me)?(?: " + OWN + r")?",
    r"(?:what|whats|what is|what are) (?:running|runs|are your jobs|are your automations|jobs are you running|jobs do you run)"
    r"(?: for me)?(?: " + OWN + r"| on its own| on their own)?",
    # the owner's email and calendar
    r"(?:are|r) (?:you|u) (?:still )?(?:watching|reading|checking|monitoring|seeing|getting|tracking|following|"
    r"keeping an eye on|keeping tabs on|on top of|connected to|hooked up to|plugged into|syncing) " + FEED,
    r"(?:do|can) (?:you|u) (?:still )?(?:see|get|watch|monitor|access|read) " + FEED + r"(?: " + OWN + r")?",
    r"(?:do|can) (?:you|u) (?:still )?check " + FEED + r" " + OWN,
    r"(?:is|are) " + FEED + r" (?:still )?(?:connected|hooked up|working|syncing|coming through|getting through|being watched|"
    r"being read|plugged in)",
)
TRIGGER = re.compile(PRE + r"(?:" + '|'.join(CORES) + r")" + POST)


def norm(message) -> str:
    """chatplan.norm, plus "?" gone: lower case, apostrophes dropped, every other mark a space."""
    t = str(message or '').replace('’', "'").replace('‘', "'").lower().replace("'", '')
    return ' '.join(re.sub(r"[^a-z0-9\s]", ' ', t).split())


def is_status_question(message, kind: str = '', ask_kind: str = 'chat') -> bool:
    """kind = chatplan.kind(message); ask_kind = replychecks.ask_kind(message). Both come from the plugin."""
    t = norm(message)
    if not t or len(t.split()) > MAX_WORDS or kind or ask_kind != 'chat':
        return False
    return bool(TRIGGER.fullmatch(t))


# ---- small readers (any trouble = nothing) -------------------------------------------------------------------------
def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _dt(value, tz):
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=PACIFIC)).astimezone(tz)


def _clock(t: datetime) -> str:
    return t.strftime('%I:%M%p').lstrip('0').lower().replace(':00', '')


def ago(seconds) -> str:
    s = max(0, round(seconds))
    if s < 90:
        return 'just now'
    if s < 3600:
        return f'{round(s / 60)} min ago'
    if s < 2 * 3600:
        return 'about an hour ago'
    if s < 36 * 3600:
        return f'about {round(s / 3600)} hours ago'
    return f'{round(s / 86400)} days ago'


def at_words(t: datetime, now: datetime) -> str:
    days = (t.date() - now.date()).days
    day = {0: 'today', 1: 'tomorrow', -1: 'yesterday'}.get(days) or t.strftime('%a %b %d').replace(' 0', ' ')
    return f'{day} at {_clock(t)}'


def until_words(t: datetime, now: datetime) -> str:
    s = (t - now).total_seconds()
    if s < 90:
        return 'in a minute or so'
    if s < 3600:
        return f'in {round(s / 60)} min'
    return at_words(t, now)


def _join(parts: list) -> str:
    parts = [p for p in parts if p]
    return '' if not parts else parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]


# ---- the pieces --------------------------------------------------------------------------------------------------------
# The owner's mailboxes by the labels the inbox watch uses -> display names (mail.mailbox_names; a label with no
# display name is shown title-cased).
MAILBOXES = {str(k): str(v) for k, v in (settings.get('mail.mailbox_names', {}) or {}).items()}
# Store ids (as in signins.json) -> display names (stores.display_names, merged over these defaults).
STORES = {'walmart': 'Walmart', 'amazon': 'Amazon', 'samsclub': "Sam's Club", 'southwest': 'Southwest',
          **{str(k): str(v) for k, v in (settings.get('stores.display_names', {}) or {}).items()}}
# Stores where the agent signing itself in does not work (the store shows a robot check), so the phone link is the
# only fix (stores.phone_only_signin). Any store whose last own try stopped on a robot check (relogin.py: robot_check)
# joins it, read from relogin.jsonl.
PHONE_ONLY = {str(s) for s in (settings.get('stores.phone_only_signin', ['amazon']) or [])}


def _box_name(box: str) -> str:
    return MAILBOXES.get(box) or (box.title() if re.fullmatch(r'[a-z]{2,20}', box) else f'one of {POSS} inboxes')


def _health_lines(h: dict, life: Path, now: datetime, problems: list) -> list:
    lines = []
    look = h.get('when_ts')
    look_age = (now.timestamp() - float(look)) if isinstance(look, (int, float)) else None
    if h.get('off') or (life / 'off').exists():
        lines.append(f'Your watching is paused: you are not reading {POSS} email or calendar until {O} says to start again.')
        problems.append('your watching is paused')
        return lines
    if look_age is None or look_age > LOOK_STALE:
        when = f', {ago(look_age)}' if look_age is not None else ''
        lines.append(f'Your own look at {POSS} email and calendar has not run for a while (last one{when}); it normally runs every 5 minutes.')
        problems.append(f'your own look at {POSS} email and calendar has not run lately')
    if h.get('mode') and h.get('mode') != 'live':
        lines.append(f'You are reading {POSS} email and calendar but not texting {O} anything on your own right now.')
    feed = h.get('inbox_feed') or {}
    boxes = feed.get('mailboxes') if isinstance(feed.get('mailboxes'), dict) else {}
    extra = look_age or 0
    names = [n for n in (_box_name(b) for b in boxes) if n]
    if not feed.get('readable', True) or not boxes:
        lines.append(f'Email: you cannot read {POSS} inboxes right now.')
        problems.append(f'you cannot read {POSS} email right now')
    else:
        late = [(_box_name(b), a) for b, a in boxes.items() if _box_name(b) and (a is None or a + extra > INBOX_STALE)]
        fresh_age = max([a for b, a in boxes.items() if _box_name(b) and a is not None] or [0]) + extra
        if late:
            lines.append(f'Email: you have not been able to read {_join([n for n, _ in late])} for over 30 min.')
            problems.append(f'you cannot read {_join([n for n, _ in late])}')
        else:
            lines.append(f'Email: you read {POSS} {len(names)} inboxes ({_join(names)}) every few minutes; last read {ago(fresh_age)}.')
    cal = h.get('calendar') or {}
    unreadable = [u.get('name') for u in cal.get('unreadable') or [] if isinstance(u, dict) and u.get('name')]
    age = cal.get('snapshot_age_seconds')
    if unreadable:
        lines.append(f'Calendar: you cannot read {_join(list(dict.fromkeys(unreadable)))} right now.')
        problems.append(f'you cannot read {_join(list(dict.fromkeys(unreadable)))}')
    if cal.get('stale'):
        lines.append(f'Calendar: your copy is {ago((age or 0) + extra).replace(" ago", " old")}; it should refresh {CAL_HOURS}.')
        problems.append(f'your copy of {POSS} calendar is out of date')
    elif isinstance(age, (int, float)) and not unreadable:
        lines.append(f'Calendar: last read {ago(age + extra)}; it refreshes {CAL_HOURS}.')
    return lines


def _sent_last(sent: dict, site: str):
    days = []
    for k, v in (sent or {}).items():
        if str(k).split('#')[0] == site:
            days += [v] if isinstance(v, str) else [str(d) for d in (v.get('dates') or [])] if isinstance(v, dict) else []
    return max(days) if days else None


def _robot_stores(life: Path) -> set:
    out = set()
    try:
        for line in (life / 'relogin.jsonl').read_text(encoding='utf-8').splitlines():
            r = json.loads(line)
            if r.get('event') == 'finished' and r.get('stop') == 'robot_check':
                out.add(str(r.get('store')))
    except (OSError, ValueError):
        pass
    return out


def _signin_lines(h: dict, life: Path, jobs: list, now: datetime, tz, problems: list, needs: list) -> list:
    sites = ((h.get('signins') or {}).get('sites')) or ((_json(life / 'signins.json') or {}).get('sites')) or {}
    if not isinstance(sites, dict) or not sites:
        return []
    ins = [STORES.get(s, s.title()) for s, v in sites.items() if isinstance(v, dict) and v.get('status') == 'signed_in']
    outs = [(s, v) for s, v in sites.items() if isinstance(v, dict) and v.get('status') == 'signed_out']
    lines = []
    if ins:
        lines.append(f'Stores you are signed in to: {_join(ins)}.')
    links_on = not (life / 'handoff-links-off').exists() and any(j.get('_kind') == 'links' for j in jobs)
    sent = _json(life / 'signin-handoff-sent.json') or {}
    phone_only = PHONE_ONLY | _robot_stores(life)
    for s, v in outs:
        name = STORES.get(s, s.title())
        since = _dt(v.get('lapsed_since') or (v.get('lapse') or {}).get('since'), tz)
        when = f' {at_words(since, now)}' if since else ''
        if links_on:
            last = _sent_last(sent, s)
            told = ''
            if last:
                d = (now.date() - datetime.fromisoformat(last).date()).days
                told = ' (last sent today)' if d == 0 else ' (last sent yesterday)' if d == 1 else f' (last sent {d} days ago)'
            fix = f'The fix: {O} opens the sign-in link you text them{told} on their phone.'
        else:
            fix = f'The phone sign-in link is switched off, so it stays signed out until {O} signs in again.'
        only = f' That link is the only way for {name}: it took your own sign-in for a robot.' if s in phone_only else ''
        lines.append(f'{name} signed you out{when} and still has you signed out. {fix}{only}')
        needs.append(f'{name} is signed out')
    return lines


# Plain names for the jobs the agent runs on its own, by the job's name in the profile's job list. What each does is
# from its own code: the life tick (tick.py), the buy lock's settle check, lapse_links.py, the work maintenance.
JOBS = (
    (re.compile(r'life pipes tick', re.I), 'tick', f'Reading {POSS} email and calendar for things to handle'),
    (re.compile(r'purchase (?:settle|lock)', re.I), 'buy', f'Making sure nothing is bought without {POSS} yes'),
    (re.compile(r'phone sign-?in links', re.I), 'links', f'Texting {O} a sign-in link when a store signs you out'),
    (re.compile(r'work maintenance', re.I), 'tidy', 'Tidying your own to-do list'),
)
FOLLOW_UP = re.compile(r'^life-work:\s*(?:follow-?up:\s*)?(.+)$', re.I)
MACHINE_NAME = re.compile(r'\b(?:tick|pipes?|cron|shadow|script|agent|hermes|sync|heartbeat|bridge|gateway|webhook|api|daemon|'
                          r'plugin|engine|worker|test|sandbox|' + re.escape(AGENT.lower()) + r'-)\b|[_/{}\[\]`]', re.I)


def jobs_of(jobs_file, life: Path) -> list:
    """The enabled jobs, each with a plain name or folded as unknown."""
    d = _json(jobs_file) if jobs_file else None
    rows = (d.get('jobs') if isinstance(d, dict) else d) or []
    out = []
    for j in rows if isinstance(rows, list) else []:
        if not isinstance(j, dict) or not j.get('enabled') or j.get('state') in ('paused', 'disabled') or j.get('paused_at'):
            continue
        name = str(j.get('name') or '')
        kind, plain = '', ''
        for pat, k, words in JOBS:
            if pat.search(name):
                kind, plain = k, words
                break
        if not kind:
            m = FOLLOW_UP.match(name)
            rest = m.group(1).strip() if m else re.sub(r'\(.*?\)', '', re.sub(r'^' + re.escape(AGENT) + r'\s+', '', name, flags=re.I)).strip()
            if rest and len(rest) <= 80 and not MACHINE_NAME.search(rest):
                kind, plain = ('once' if m else 'other'), rest[0].lower() + rest[1:] if m else rest[0].upper() + rest[1:]
        out.append(dict(j, _kind=kind or 'unknown', _plain=plain))
    return out


# The routine check ("life pipes tick") may be started by the always-on watcher instead of a Hermes job. Found by the
# JOB's name alone, "Reading <owner>'s email and calendar for things to handle" would drop out of the answer while the
# check runs fine. Whether it runs is the engine's own record of its last check (last-tick.json, each check's last act),
# with the same 15 minutes the engine's own silence alarm uses. No new file.
ENGINE_SILENT = 15 * 60
CHECK_BEAT_MIN = 5                      # the watcher starts a check at least this often (zeus_watch.BEAT)


def routine_check(jobs: list, life: Path, now: datetime, tz):
    """-> (a job row to list | None, why it is not running | ''). The Hermes job is on: (None, ''), it is listed as itself.
    Not on and the last check began under 15 minutes ago: a row made from that record. Not on and no fresh check:
    the plain words that the routine check is NOT running, with when it last ran when that is known."""
    if any(j.get('_kind') == 'tick' for j in jobs):
        return None, ''
    last = _dt((_json(life / 'last-tick.json') or {}).get('at'), tz)
    if last is not None and 0 <= (now - last).total_seconds() <= ENGINE_SILENT:
        return {'id': 'zeus-watch', 'name': f'{AGENT} life pipes tick (always-on watcher)', 'enabled': True, 'state': 'scheduled',
                'schedule': {'kind': 'interval', 'minutes': CHECK_BEAT_MIN}, 'last_run_at': last.isoformat(),
                'next_run_at': (last + timedelta(minutes=CHECK_BEAT_MIN)).isoformat(), 'last_status': 'ok', '_kind': 'tick', '_plain': JOBS[0][2]}, ''
    return None, 'it last ran ' + ago((now - last).total_seconds()) if last is not None and now >= last else 'there is no record of it running'


def _every(j) -> str:
    sch = j.get('schedule') or {}
    m = sch.get('minutes') if isinstance(sch, dict) else None
    if isinstance(m, (int, float)) and m > 0:
        return 'every minute' if m == 1 else f'every {int(m)} min' if m < 60 else f'every {round(m / 60)} hours'
    return ''


def _job_line(j, now, tz, life: Path, problems: list) -> str:
    last, nxt = _dt(j.get('last_run_at'), tz), _dt(j.get('next_run_at'), tz)
    bad = []
    if j.get('last_status') not in (None, 'ok') or j.get('last_error'):
        bad.append('its last run hit a problem')
    if j.get('last_delivery_error'):
        bad.append(f'its last text to {O} did not go through')
    sch = j.get('schedule') or {}
    every_min = sch.get('minutes') if isinstance(sch, dict) and isinstance(sch.get('minutes'), (int, float)) else None
    if nxt and (now - nxt).total_seconds() > max(600, 2 * 60 * (every_min or 0)):
        bad.append(f'it is late (it was due {at_words(nxt, now)})')
    name = j['_plain']
    if j['_kind'] == 'once':
        text = f'A one-time check, {name}: {at_words(nxt, now) if nxt else "soon"}'
    else:
        parts = [_every(j)] + ([f'last ran {ago((now - last).total_seconds())}'] if last else []) + \
                ([f'next {until_words(nxt, now)}'] if nxt and nxt >= now else [])
        text = f'{name}: ' + ', '.join(p for p in parts if p)
    if bad:
        problems.append(f'"{name.lower()}": {_join(bad)}')
        text += '. But ' + _join(bad)
    return text + '.'


def _ok_lines(life: Path) -> list:
    lines = []
    g = _json(life / 'grants.json')
    oks = []
    for v in (g.values() if isinstance(g, dict) else []):
        if isinstance(v, dict) and (v.get('title') or v.get('owners_words')):
            said = str(v.get('owners_words') or v.get('note') or '').strip()
            oks.append(f'{str(v.get("title") or "this kind of thing").strip().rstrip(".")} and things like it'
                       + (f' ({O} said "{said[:80]}")' if said else ''))
    lines.append((f'Standing OKs {O} has given (you do these without asking, then tell them): ' + '; '.join(oks) + '.') if oks else
                 f'Standing OKs {O} has given: none, so everything that needs {POSS} yes still waits for it.')
    rules = _json(life / 'standing-rules.json')
    owner_set, learned = [], []
    for r in rules if isinstance(rules, list) else []:
        if not isinstance(r, dict) or r.get('state') not in ('confirmed', 'adopted'):
            continue
        kind = str(r.get('kind') or '').strip()
        brand = str(r.get('sender_domain') or '').split('.')[0]
        if not kind:
            continue
        label = kind if not brand or brand.lower() in kind.lower() else f'{brand.title()} {kind}'
        if r.get('state') == 'confirmed':
            owner_set.append(label + (f' (the step that needs {POSS} yes still waits for it)' if r.get('needs_his_yes') else ''))
        else:
            learned.append(label)
    if owner_set:
        lines.append(f'Ways of working {O} set for you: ' + '; '.join(owner_set) + '.')
    if learned:
        lines.append(f'Picked up from {POSS} mail, and nothing in it needs {POSS} yes: ' + '; '.join(learned) + '.')
    return lines


def _plain(line: str, rc=None) -> bool:
    if re.search(r"/(?:Users|home)/|~/|\w\.(?:jsonl?|py|md|sqlite3|ya?ml|db)\b|[A-Za-z]_[A-Za-z]|[{}\[\]`]|<[A-Za-z/][^>]*>", line):
        return False
    if rc is not None:
        if any(p.search(line) for p in rc.MACHINERY) or rc.SNAKE.search(line) or rc.PATHS.search(line):
            return False
    return True


INTRO = (HEAD + f'. {POSS[:1].upper() + POSS[1:]} message asks whether your own work is running, what you do for them on '
         f'your own, or whether you are watching their email or calendar. Answer from these lines only, in your own voice, '
         'as a short text (about 40 words): the bottom line first, then only the lines the question is about (asked about '
         'email, answer about email). Say times the way they are written here. If a line says something is not working, '
         'say so plainly and never call everything fine; add no job, inbox, store or time that is not here. Plain '
         'everyday words, no list unless they ask for everything you do, and never mention these lines.')


def block(life_home, jobs_file=None, now=None, tz=None, rc=None, timer_line=None) -> str:
    """-> the whole section (heading, how to use it, the lines), or '' when there is nothing to stand on.
    timer_line (timers, switch file timer-truth-on): one more line, built by the engine's timers.py, added
    last and checked like every other line. None, the default, leaves every line exactly as it was."""
    life = Path(life_home)
    tz = tz or PACIFIC
    now = datetime.fromtimestamp(time.time() if now is None else float(now), tz)
    h = _json(life / 'health.json')
    problems, needs = [], []
    if not isinstance(h, dict):
        lines = ['You cannot see how your own work is running this minute. Say so plainly, in one line, and nothing else about it.']
        return INTRO + '\n' + '\n'.join(lines)
    jobs = jobs_of(jobs_file, life)
    check_row, check_gone = routine_check(jobs, life, now, tz)   # the routine check when the watcher, not a Hermes job, runs it
    if check_row:
        jobs.insert(0, check_row)
    lines = _health_lines(h, life, now, problems)
    lines += _signin_lines(h, life, jobs, now, tz, problems, needs)
    shown = [j for j in jobs if j['_kind'] != 'unknown']
    if shown or check_gone:
        lines.append('Jobs you run on your own:')
        if check_gone:      # neither the job nor the watcher is running it: said plainly, never left out
            lines.append(f'- {JOBS[0][2]}: this is NOT running right now ({check_gone}).')
            problems.append(f'"{JOBS[0][2].lower()}" is NOT running ({check_gone})')
        lines += ['- ' + _job_line(j, now, tz, life, problems) for j in shown]
    others = [j for j in jobs if j['_kind'] == 'unknown']
    if others:
        failing = [j for j in others if j.get('last_status') not in (None, 'ok') or j.get('last_error')]
        lines.append(f'- Plus {len(others)} other small job{"s" if len(others) > 1 else ""}'
                     + (f', and {len(failing)} of them hit a problem last time.' if failing else ', all fine last time.'))
        if failing:
            problems.append(f'{len(failing)} small job{"s" if len(failing) > 1 else ""} hit a problem')
    lines += _ok_lines(life)
    w = h.get('watches') or {}
    if isinstance(w.get('watching'), int) and w['watching'] > 0:
        lines.append(f'Web pages you are watching for {O}: {w["watching"]}.')
    if timer_line:   # timers: how many run, what is next, what is late right now and why
        lines.append(str(timer_line))
    lines = [x for x in lines if _plain(x, rc)]
    if problems:
        bottom = 'Bottom line: not everything is working: ' + _join(problems) + '.'
    else:
        bottom = 'Bottom line: everything is running.'
    if needs:
        bottom += f' The one thing that needs {O}: ' + _join(needs) + '.' if len(needs) == 1 else \
                  f' What needs {O}: ' + _join(needs) + '.'
    return INTRO + '\n' + bottom + '\n' + '\n'.join(lines)


# ---- for the promise check -----------------------------------------------------------------------------------------
FEED_TEXT = re.compile(r"\b(?:e-?mails?|mail|inbox(?:es)?|gmail|mailboxes|calendars?)\b", re.I)
EMAIL_TEXT = re.compile(r"\b(?:e-?mails?|mail|inbox(?:es)?|gmail|mailboxes)\b", re.I)
CAL_TEXT = re.compile(r"\bcalendars?\b", re.I)
# The words a sentence about watching the owner's email or calendar may carry and still be only about that (plus the
# words of the configured mailbox labels and display names).
GENERIC = frozenset('yes yeah yep yup still all both your their five four three two six normal usual always constantly '
                    'closely every few last read ago min mins minute minutes just fine running reading checking right '
                    'inboxes inbox emails email mail gmail mailboxes calendar calendars personal work and the as of on for '
                    'you every couple seconds second hour hours'.split()
                    + [w for w in ' '.join(list(MAILBOXES) + list(MAILBOXES.values()) + list(settings.OWNER_EMAILS)).lower().split()])


def standing_feeds(life_home, now=None) -> dict:
    """-> {'email': bool, 'calendar': bool}: is that feed really being read on its own right now (health.json)."""
    h = _json(Path(life_home) / 'health.json')
    if not isinstance(h, dict) or h.get('off') or (Path(life_home) / 'off').exists():
        return {'email': False, 'calendar': False}
    now = time.time() if now is None else float(now)
    look = h.get('when_ts')
    if not isinstance(look, (int, float)) or now - float(look) > LOOK_STALE:
        return {'email': False, 'calendar': False}
    feed, cal = h.get('inbox_feed') or {}, h.get('calendar') or {}
    return {'email': bool(feed.get('ok')) and not feed.get('stale'),
            'calendar': bool(cal.get('ok')) and not cal.get('stale') and not cal.get('unreadable')}


def covers(sentence: str, feeds: dict) -> bool:
    """Is this "watching" sentence only about the owner's email and/or calendar, and is every feed it names being read?"""
    s = str(sentence or '')
    if not FEED_TEXT.search(s):
        return False
    if (EMAIL_TEXT.search(s) and not feeds.get('email')) or (CAL_TEXT.search(s) and not feeds.get('calendar')):
        return False
    rest = {w for w in re.findall(r"[a-z][a-z']+", s.lower()) if len(w) > 2} - GENERIC
    rest -= {'i\'m', 'im', 'watching', 'watch', 'keeping', 'keep', 'eye', 'eyes', 'tabs', 'monitoring', 'tracking', 'are', 'yes'}
    return not rest

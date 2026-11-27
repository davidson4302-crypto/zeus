"""promisecheck: a promise of later work must match what is really set up.

The failure it stops: "Done, I'll watch Alex's flight and text you the live status around 1:30 today", with ONE check
at 1:30 (`life add --due`) behind it and no watch. A cancellation at 11:33 could not reach the owner before 1:30.

What this does, in code, on every reply to the owner (switch file `promise-check-on` in the life folder):
  1. find promise sentences: first person, future or ongoing, about watching or a later check or text
       monitor   watch, keep an eye on, monitor, track, keep tabs on, "I'm watching"
       timed     check / text / tell / update / let you know ... at or around a clock time, or in N minutes or hours
     Negated ("I won't watch"), conditional ("If ...") and quoted sentences are not promises.
  2. read what is really set up, from the durable records, not from what a tool printed:
       the ledger (<life>/ledger.sqlite3, opps): open items, their due time and how they reach the owner
       the watch list (<life>/watches.json): active watches, and whether each is a monitor and until when
     A record counts when it is about the same thing (it shares a real word with the promise or with the owner's
     message), whether it was made this turn or already existed: an existing suitable watch is accepted, never
     duplicated.
  3. compare
       monitor promise  needs an active monitor (watches.py with monitor.py) that runs at least until the promised
                        time, or for at least the next hour when no time is named. A plain backoff watch does not
                        count as monitoring.
       timed promise    needs an open item due within 15 minutes of that time that reaches the owner's chat (not the
                        morning note), or an active monitor covering that time.
  4. a promise with nothing that matches is replaced by one plain sentence saying what is really set up:
       "I've set one check for 1:30pm, not a running watch, so a change before then won't reach you."
       "That isn't set up, so I won't be watching it."
     "Done" in front of an unmet promise goes with it.
Every trip is one line in <life>/promise-checks.log.jsonl. Any trouble: the reply goes out exactly as written, and
the trouble is logged. Reading only: nothing here writes to the ledger or the watch list.

Untimed promises (same switch, V2). "Then I'll text you the live status, the landing time and when to leave." names no
clock time, so step 1 alone never sees it. Three additions, none of them more word rules for judgment calls:
  a. Every first-person future sentence the word rules above do not already classify is a CANDIDATE. One short model
     reading (its own wall clock) says which candidates promise work AFTER this reply (a later check, a later message,
     watching something), as against something being done in this reply, an offer that waits on the owner's yes, or a
     manner of speaking. No model, no answer in time, or an answer out of shape: no candidate is treated as a promise.
  b. Code then checks each such promise against the durable records exactly as before, with one more kind:
       later     a later check or message with no clock time. Needs an open item about the same thing that is still
                 to come, an active watch or monitor about it, tracking that is saved to start later, or a piece of
                 the agent's long-running work about it that is under way.
  c. Tracking that is SAVED TO START LATER (a commitment whose next step is to arm a monitor, commit.py in the
     engine) carries a promise to track from that time. It does not carry "I'm watching it now".
A thing counts as "about the same thing" as before, and also when the shared reading of the owner's message
(turnctx.py) named it as what the message is about.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path


def _settings():
    mod = sys.modules.get('settings')
    if mod is not None and hasattr(mod, 'OWNER_FIRST'):
        return mod
    spec = importlib.util.spec_from_file_location('settings', Path(__file__).resolve().parents[2] / 'engine' / 'settings.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return sys.modules.setdefault('settings', mod)


settings = _settings()
OWNER = settings.OWNER_FIRST if settings.OWNER_FIRST != 'the owner' else 'the owner'
AGENT = str(settings.AGENT_NAME or 'Zeus')
# the owner's own names are never a word that ties a promise to a record
_OWNER_WORDS = {w for w in ' '.join([str(settings.OWNER_NAME or '')] + [str(n) for n in (settings.get('owner.nicknames', []) or [])]).lower().split()
                if w not in ('the', 'owner')}

SWITCH = 'promise-check-on'
V2 = 'promise-check-on'          # untimed promises, tracking saved to start later (same switch)
JUDGE_SECONDS = 12.0          # wall clock for the one model reading of the candidates (measured: 2 to 7 seconds, more under load).
                              # The turn reading may take 15 and the reply hook is abandoned at 30; this runs in the reply hook only.
PROVIDER_TIMEOUT = 30         # what the provider is told; a short one made it give up and retry
MAX_CANDIDATES = 8
LOG = 'promise-checks.log.jsonl'
SLACK = timedelta(minutes=15)
NO_TIME_HORIZON = timedelta(hours=1)

# Only a promise made by the assistant can require a saved future action. Third-person
# events, questions and “thanks for watching” inside a message for someone else are facts.
FIRST_FUTURE = (r"\b(?:i|we)(?:['’]ll| will)\b"
                r"|\b(?:i(?:['’]m| am)|we(?:['’]re| are))\s+(?:(?:still|also|already|now|[a-z]+ly)\s+)*"
                r"(?:going to|about to|watching|monitoring|keeping|tracking|checking|texting|confirming|telling|sending)\b"
                r"|^\s*(?:will (?:check|watch|track|monitor|tell|text|follow|send|keep)|watching|keeping (?:an eye|tabs))\b")
MONITOR = re.compile(r"\b(?:watch(?:ing)?|keep(?:ing)? an eye on|keep(?:ing)? tabs on|monitor(?:ing)?|(?<!off )(?<!on )(?<!back on )track(?:ing)?(?! record))\b", re.I)
LATER = re.compile(r"\b(?:confirm(?:ing)?|check(?:ing)?|text(?:ing)?|tell(?:ing)?|update|ping|let you know|send you|get back to you|circle back)\b", re.I)
NEGATED = re.compile(r"\b(?:not|won't|will not|can't|cannot|didn't|don't|never|no longer|stopped)\b", re.I)
CLOCK = re.compile(r"\b(?:at|around|by|about|near|before)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", re.I)
IN_N = re.compile(r"\bin\s+(\d{1,3})\s*(minutes?|mins?|hours?|hrs?)\b", re.I)
DONE_LEAD = re.compile(r"^\s*(?:done|ok(?:ay)?|got it|on it|sure|will do)\s*[,.!:-]\s*", re.I)
SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])|\n+")
STOP = frozenset('''a an the and or but to of for on in at by it its this that these those you your yours me my i i'll ill will
                 watch watching keep eye eyes monitor track tabs check text tell update ping let know send get back about around
                 today tonight tomorrow morning afternoon evening live status time when then now just also any some so
                 with from up out over what how is are be been was were have has had do does did done can could would
                 should one two am pm minutes hours there here page site status account update thing things
                 test link app email mail note message text texts flight's please thanks okay sure need want'''.split()) | _OWNER_WORDS
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
# Standing watches the engine runs all the time ("I'm watching your email and calendar as normal" is true without any
# watches.json entry). These are backed by the engine's own feeds, read from health.json, not by watches.json.
STANDING = {'email': 'inbox_feed', 'emails': 'inbox_feed', 'inbox': 'inbox_feed', 'inboxes': 'inbox_feed', 'mail': 'inbox_feed',
            'mailbox': 'inbox_feed', 'mailboxes': 'inbox_feed', 'calendar': 'calendar', 'calendars': 'calendar'}
HEALTH_FRESH_S = 20 * 60      # the tick writes health.json every 5 minutes
QUIET_CHANNELS = ('morning_brief',)


def on(life_home) -> bool:
    try:
        return (Path(life_home) / SWITCH).exists()
    except Exception:
        return False


def words(text) -> set:
    return {w for w in re.findall(r"[a-z][a-z0-9']{2,}", str(text or '').lower()) if w not in STOP}


def nums(text) -> set:
    """Numbers of three or more digits in a text: a flight number, an order number. "Delta 1846" and an item titled
    "Jordan on Delta 1846" share one real word and the number; the number is what names it."""
    return set(re.findall(r"(?<![\d.:$])\d{3,}(?![\d:])", str(text or '')))


def _dt(v, tz):
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=tz)).astimezone(tz)


_DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
_MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
_DAY_WORD = re.compile(r"\b(today|tonight|tomorrow|tmrw|mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\b|"
                       r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)


def promised_day(sentence: str, now: datetime):
    """The calendar day a sentence names ("Thursday", "tomorrow", "Jun 17"), or None. Without it "I'll check Thursday
    at noon" reads as noon TODAY, the Thursday item does not match it, and a true promise gets rewritten."""
    m = _DAY_WORD.search(sentence)
    if not m:
        return None
    if m.group(2):
        try:
            d = now.replace(month=_MONTHS.index(m.group(2).lower()[:3]) + 1, day=int(m.group(3)))
        except ValueError:
            return None
        return d if d.date() >= now.date() else d.replace(year=now.year + 1)
    w = m.group(1).lower()
    if w in ('today', 'tonight'):
        return now
    if w in ('tomorrow', 'tmrw'):
        return now + timedelta(days=1)
    return now + timedelta(days=(_DAYS.index(w[:3]) - now.weekday()) % 7)


def promised_time(sentence: str, now: datetime, v2: bool = False):
    """The clock time a sentence promises, as the next such moment from now (within 12 hours), or None.
    v2 (promise-check-on): when the sentence also names a day, the time is on that day."""
    m = IN_N.search(sentence)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return now + (timedelta(hours=n) if unit.startswith('h') else timedelta(minutes=n))
    sentence = re.sub(r'\bnoon\b', '12:00pm', re.sub(r'\bmidnight\b', '12:00am', sentence, flags=re.I), flags=re.I)
    m = CLOCK.search(sentence)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or '').lower().replace('.', '')
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    if ap == 'pm' and h < 12:
        h += 12
    elif ap == 'am' and h == 12:
        h = 0
    day = promised_day(sentence, now) if v2 else None
    if day is not None:
        at = day.replace(hour=h % 24, minute=mi, second=0, microsecond=0)
        if not ap and h < 8:
            at += timedelta(hours=12)          # "Thursday at 3" with no am/pm: the afternoon
        if at < now - timedelta(minutes=5) and day.date() == now.date() and not re.search(r"\b(?:today|tonight)\b", sentence, re.I):
            at += timedelta(days=7)            # a weekday named on that same weekday, with the time already past: next week
        return at
    cands = [now.replace(hour=h % 24, minute=mi, second=0, microsecond=0)]
    if not ap and h < 12:
        cands.append(cands[0] + timedelta(hours=12))
    cands = [c if c >= now - timedelta(minutes=5) else c + timedelta(days=1) for c in cands]
    return min(cands)


# "It isn't watching yet, so you'll hear from me once it is." contains "watching" but promises nothing: tracking may
# well be saved to start later. A clause that says something is NOT happening promises nothing. Only with V2.
NEGATED_V2 = re.compile(r"\b(?:is|are|was|were|has|have|had|does|do|did)n'?t\b|\bnot yet\b|\bnothing(?:'s| is)\b", re.I)
FOLLOWS_ON = re.compile(r"^\W*(?:it|that|this|then|and|so|also|after that|once|from then|until then|either way)\b", re.I)


def promises(reply: str, now: datetime, v2: bool = False) -> list:
    """-> [{'sentence', 'kind': 'monitor'|'timed', 'at': datetime|None}] for each promise sentence."""
    out = []
    for s in SENT.split(str(reply or '').strip()):
        core = s.strip()
        if not core or core.startswith(('"', '“', 'If ', 'if ')) or core.endswith('?') or NEGATED.search(core) or (v2 and NEGATED_V2.search(core)):
            continue
        if not re.search(FIRST_FUTURE, core, re.I):
            continue
        at = promised_time(core, now, v2)
        if MONITOR.search(core):
            out.append({'sentence': s, 'kind': 'monitor', 'at': at})
        elif LATER.search(core) and at is not None:
            out.append({'sentence': s, 'kind': 'timed', 'at': at})
    return out


def setup(life_home, tz) -> dict:
    """What is really set up, from the durable records. Read only."""
    life = Path(life_home)
    items, watches = [], []
    db = life / 'ledger.sqlite3'
    if db.exists():
        with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)) as con:
            con.row_factory = sqlite3.Row
            marks = ','.join('?' * len(OPEN))
            for r in con.execute(f'SELECT key,state,surface_at,payload,first_seen,updated FROM opps WHERE state IN ({marks})', OPEN).fetchall():
                try:
                    p = json.loads(r['payload'])
                except ValueError:
                    continue
                d = p.get('decision') if isinstance(p.get('decision'), dict) else {}
                items.append({'key': r['key'], 'due': _dt(r['surface_at'], tz), 'channel': d.get('channel') or 'text',
                              'touched': float(r['first_seen'] or 0),      # made, not merely updated (ticks update rows)
                              'words': words(' '.join(str(x) for x in (r['key'], p.get('title'), p.get('proposed_action'), p.get('evidence')))),
                              'nums': nums(' '.join(str(x) for x in (p.get('title'), p.get('proposed_action'), p.get('evidence')))),
                              **_arm(p, tz)})
    wf = life / 'watches.json'
    if wf.exists():
        d = json.loads(wf.read_text())
        for w in d.get('watches') or []:
            if w.get('state') != 'watching':
                continue
            m = w.get('monitor') if isinstance(w.get('monitor'), dict) else None
            made = _dt(w.get('created_at'), tz)
            watches.append({'id': w.get('id'), 'monitor': bool(m), 'until': _dt((m or {}).get('until') or w.get('ends_at'), tz),
                            'words': words(' '.join(str(x) for x in (w.get('for'), w.get('item'), w.get('item_title'), w.get('words')))),
                            # when it was made, the item it belongs to, and the owner's words it was set up for
                            'touched': made.timestamp() if made else 0.0, 'key': w.get('item') or None, 'said': str(w.get('words') or ''),
                            'nums': nums(' '.join(str(x) for x in (w.get('for'), w.get('item_title'))))})
    return {'items': items, 'watches': watches, 'work': _work(life), 'goals': _goals(life)}


def _goals(life) -> list:
    """The goals the owner gave that are still open (the engine's goals.py, read only): [{'words', 'touched'}]. A goal is
    a durable record the hourly research follows, so "I'll keep track of that" said about one is true; without this the
    reply to the owner's own goal would be rewritten to "That isn't set up". No table, or any trouble: []."""
    db = Path(life) / 'ledger.sqlite3'
    try:
        if not db.exists():
            return []
        with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=2)) as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'").fetchone():
                return []
            return [{'words': words(f'{title} {said}'), 'touched': float(created or 0)}
                    for title, said, created in con.execute("SELECT title,owners_words,created FROM goals WHERE status IN ('active','paused')").fetchall()]
    except (sqlite3.Error, OSError):
        return []


def _goal_backed(have: dict, pw: set, aw: set, since) -> bool:
    """Is this promise about a goal on record (two shared words, or one for a goal saved this very turn)?"""
    return any(_about(g['words'], pw, aw) or (_this_turn(g, since) and _about(g['words'], pw, aw, strong=False)) for g in have.get('goals') or [])


def _arm(payload: dict, tz) -> dict:
    """Tracking saved to start later on this item (commit.py: next step 'arm_monitor'): {'arm_due', 'arm_until'} or {}."""
    try:
        na = ((payload.get('commitment') or {}).get('next_action') or {})
        if na.get('do') != 'arm_monitor':
            return {}
        due = _dt(na.get('due'), tz)
        untils = [_dt(leg.get('until'), tz) for leg in ((na.get('monitor') or {}).get('legs') or []) if isinstance(leg, dict)]
        untils = [u for u in untils if u]
        return {'arm_due': due, 'arm_until': max(untils) if untils else None} if due else {}
    except (AttributeError, TypeError):
        return {}


def _work(life) -> list:
    """The agent's long-running work that is under way (the zeus-work plugin's own store, read only):
    [{'words', 'touched'}]. No store, or any trouble: []."""
    db = Path(os.environ.get('ZEUS_WORK_DB') or os.environ.get('ZEUS_WORK_DB')
              or (Path(life).resolve().parents[1] / 'plugin-data/zeus-work/state.sqlite3'))
    out = []
    try:
        if not db.exists():
            return out
        with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=2)) as con:
            for (payload,) in con.execute('SELECT payload FROM work').fetchall():
                try:
                    w = json.loads(payload)
                except ValueError:
                    continue
                if str(w.get('state') or '') in ('completed', 'stopped', 'cancelled', 'failed', ''):
                    continue
                out.append({'words': words(str(w.get('title') or '')), 'touched': float(w.get('created_at') or 0)})
    except (sqlite3.Error, OSError):
        return []
    return out


def _about(rec_words: set, promise_words: set, asked_words: set, strong: bool = True) -> bool:
    """Is a record about the same thing? One shared word is not enough ("page" matches a store sign-in item;
    "saturday" matches a birthday). strong: two shared words.
    strong=False: one shared word, used only for a record made this very turn or the only active monitor."""
    shared = rec_words & (promise_words | asked_words)
    return len(shared) >= (2 if strong else 1)


def _this_turn(rec: dict, since) -> bool:
    return since is not None and float(rec.get('touched') or 0) >= float(since) - 60


def standing(life_home, sentence: str, now: datetime):
    """A watch promise about the owner's email or calendar. -> None (not about them) or {'ok': True|False|None, 'by', 'truth'}.
    True: every feed it names is fresh in a fresh health note. False: health says one is stale (honest line).
    None: health cannot be read or is old: cannot tell, the sentence stays as written."""
    named = sorted({feed for w, feed in STANDING.items() if re.search(r'\b' + w + r'\b', sentence, re.I)})
    if not named:
        return None
    try:
        h = json.loads((Path(life_home) / 'health.json').read_text())
        age = now.timestamp() - float(h.get('when_ts') or 0)
    except (OSError, ValueError, TypeError):
        return {'ok': None, 'by': 'health note unreadable: cannot tell'}
    if not 0 <= age <= HEALTH_FRESH_S:
        return {'ok': None, 'by': f'health note {int(age // 60)} min old: cannot tell'}
    bad = [f for f in named if not isinstance(h.get(f), dict) or h[f].get('stale') is not False or h[f].get('ok') is False]
    if not bad:
        return {'ok': True, 'by': 'standing watch fresh: ' + ', '.join(named)}
    what = ' and '.join({'inbox_feed': 'email', 'calendar': 'calendar'}[f] for f in bad)
    return {'ok': False, 'by': 'standing watch stale: ' + ', '.join(bad),
            'truth': f"My {what} watch isn't current right now, so I can't promise that."}


NOW_WORDS = re.compile(r"\b(?:i'?m|i am|am|currently|already|now)\s+(?:watching|monitoring|tracking|keeping (?:an eye|tabs))\b", re.I)


def _bare(ws) -> set:
    """Words without a possessive ending, so "Taylor's flight" matches the record "Taylor Frontier 733"."""
    return {re.sub(r"['’]s$", '', w) for w in ws}


def _same(rec: dict, pw: set, aw: set, since, about_keys=(), pn=frozenset()) -> bool:
    """Is this record about what the promise is about: named by the shared reading of the owner's message, two shared
    words, a shared number of three or more digits (pn: the numbers in the promise, the message and the reply), or one shared
    word when it was made this very turn."""
    rw, pw, aw = _bare(rec['words']), _bare(pw), _bare(aw)
    return ((rec.get('key') in about_keys) or _about(rw, pw, aw) or bool(pn and (rec.get('nums') or set()) & set(pn))
            or (_this_turn(rec, since) and _about(rw, pw, aw, strong=False)))


def _plain(text) -> str:
    return ' '.join(re.findall(r"[a-z0-9']+", str(text or '').lower()))


def _watch_same(w: dict, pw: set, aw: set, since, about_keys=(), asked: str = '', pn=frozenset()) -> bool:
    """Is this watch the one the promise is about? "Watching it." after "keep an eye on it for me" shares no word with
    anything, though a real watch is on record. So besides shared words: the watch was made this very turn, it belongs
    to a thing the shared reading says the owner's message is about, or it was set up for these exact words."""
    return (_about(_bare(w['words']), _bare(pw), _bare(aw)) or _this_turn(w, since) or (w.get('key') is not None and w.get('key') in about_keys)
            or bool(pn and (w.get('nums') or set()) & set(pn)) or (bool(_plain(asked)) and _plain(w.get('said')) == _plain(asked)))


def _armed(p: dict, have: dict, pw: set, aw: set, now: datetime, since, about_keys=(), pn=frozenset()):
    """Tracking saved to start later that carries this promise -> that item, or None. Besides the usual links (words,
    a number, the reading, made this turn): the promise names the very time the saved tracking starts ("I'll start
    watching at 9:40am next Tuesday" and tracking saved to start Tuesday 9:40am), when only one saved start is that time."""
    need = p['at'] or now
    arms = [i for i in have['items'] if i.get('arm_due')]
    for i in arms:
        if _same(i, pw, aw, since, about_keys, pn) and (i.get('arm_until') is None or i['arm_until'] >= need):
            return i
    if p['at'] is not None:
        at_start = [i for i in arms if abs(i['arm_due'] - p['at']) <= SLACK]
        if len(at_start) == 1:
            return at_start[0]
    return _armed_this_turn(have, since, need)


def _armed_this_turn(have: dict, since, need=None):
    """The ONE tracking-to-start-later item chat made this very turn -> it, or None. "Set up. I'll start checking
    Taylor's Frontier 733 from Tuesday at 9:40am ... I'm not watching yet. I'll text you if the status changes." The
    last sentence shares no word with the saved item and does not open as a follow-on, yet it is carried. A watch made
    this turn already carries such a sentence (_watch_same); so does saved tracking made this turn, when it is the only
    one (two made in one turn: nothing is guessed)."""
    made = [i for i in have['items'] if i.get('arm_due') and _this_turn(i, since)]
    if len(made) == 1 and (need is None or made[0].get('arm_until') is None or made[0]['arm_until'] >= need):
        return made[0]
    return None


def judge_later(p: dict, have: dict, asked: str, now: datetime, since=None, life_home=None, about_keys=(), reply: str = '') -> dict:
    """A later check or message with no clock time (V2)."""
    pw, aw, pn = words(p['sentence']), words(asked), nums(p['sentence']) | nums(asked)
    if life_home is not None:
        st = standing(life_home, p['sentence'], now)
        if st is not None and st.get('ok') is not False:
            return st
    for i in have['items']:
        if _same(i, pw, aw, since, about_keys, pn) and ((i['due'] and i['due'] >= now - SLACK) or i.get('arm_due')):
            return {'ok': True, 'by': f"item {i['key']} still to come"}
    for w in have['watches']:
        if _watch_same(w, pw, aw, since, about_keys, asked, pn) or (len(have['watches']) == 1 and _about(w['words'], pw, aw, strong=False)):
            return {'ok': True, 'by': f"watch {w['id']}"}
    for w in have.get('work') or []:
        if _about(w['words'], pw, aw) or (_this_turn(w, since) and _about(w['words'], pw, aw, strong=False)):
            return {'ok': True, 'by': 'long-running work under way'}
    if _goal_backed(have, pw, aw, since):                  # a goal the owner gave is on record and the hourly research follows it
        return {'ok': True, 'by': 'a goal the owner gave, on record'}
    arm = _armed_this_turn(have, since)
    if arm:
        return {'ok': True, 'by': f"tracking saved this turn to start {arm['arm_due'].isoformat()} (item {arm['key']})"}
    return {'ok': False, 'by': 'nothing', 'truth': "That isn't set up yet, so I won't be following up on it on my own."}


def judge(p: dict, have: dict, asked: str, now: datetime, since=None, life_home=None, about_keys=(), v2=False, reply: str = '') -> dict:
    """-> {'ok': bool, 'by': what carries it, 'truth': the sentence to say instead when not ok}
    since: when the owner's message arrived (this turn). A record made or updated since then is what chat just set up.
    about_keys, v2: the things the message is about by the shared reading; the V2 switch."""
    if v2 and p['kind'] == 'later':
        return judge_later(p, have, asked, now, since, life_home, about_keys, reply)
    pw, aw = words(p['sentence']), words(asked)
    if p['kind'] == 'monitor' and p.get('at') is None and _goal_backed(have, pw, aw, since):      # "I'll keep track of it", said about a goal on record, with no clock time promised
        return {'ok': True, 'by': 'a goal the owner gave, on record'}
    if v2 and p['kind'] == 'monitor':
        fmt_day = lambda t: t.strftime('%a at %I:%M%p').replace(' 0', ' ').lower().replace(':00', '').replace(t.strftime('%a').lower(), t.strftime('%a'))
        pn = nums(p['sentence']) | nums(asked)
        arm = _armed(p, have, pw, aw, now, since, about_keys, pn)
        live = [w for w in have['watches'] if w['monitor'] and _watch_same(w, pw, aw, since, about_keys, asked, pn)]
        need = p['at'] or (now + NO_TIME_HORIZON)
        good = [w for w in live if w['until'] and w['until'] >= need]
        if good and not (p['at'] is not None and LATER.search(p['sentence'])):     # a named update time still needs its own item (below)
            return {'ok': True, 'by': f"monitor {good[0]['id']} until {good[0]['until'].isoformat()}"}
        if arm and not live:
            if NOW_WORDS.search(p['sentence']) and arm['arm_due'] > now + SLACK:
                return {'ok': False, 'by': f"tracking saved to start {arm['arm_due'].isoformat()} (item {arm['key']}), not running yet",
                        'truth': f"Tracking is set to start {fmt_day(arm['arm_due'])}; it isn't running yet."}
            return {'ok': True, 'by': f"tracking saved to start {arm['arm_due'].isoformat()} (item {arm['key']})"}
    if p['kind'] == 'monitor' and life_home is not None:
        st = standing(life_home, p['sentence'], now)
        if st is not None:
            return st
    live_mons = [w for w in have['watches'] if w['monitor']]
    mons = [w for w in live_mons if _about(w['words'], pw, aw) or (len(live_mons) == 1 and _about(w['words'], pw, aw, strong=False))]
    need_until = p['at'] or (now + NO_TIME_HORIZON)
    fmt = lambda t: t.strftime('%I:%M%p').lstrip('0').lower().replace(':00', '')
    if p['kind'] == 'monitor':
        good = [w for w in mons if w['until'] and w['until'] >= need_until]
        if good:
            if p['at'] is not None and LATER.search(p['sentence']) and not _timed_ok(p, have, pw, aw, since):
                return {'ok': False, 'by': f"monitor {good[0]['id']} but no item at that time",
                        'truth': f"I'm watching it until {fmt(good[0]['until'])} and will text you when it changes; there's no set {fmt(p['at'])} update."}
            return {'ok': True, 'by': f"monitor {good[0]['id']} until {good[0]['until'].isoformat()}"}
    elif _timed_ok(p, have, pw, aw, since) or any(w['until'] and w['until'] >= p['at'] for w in mons):
        return {'ok': True, 'by': 'an item due at that time, or a monitor covering it'}
    # Only a record chat made THIS turn may be named as what is really set up; anything older is never guessed at.
    made = [i for i in have['items'] if i['due'] and i['channel'] not in QUIET_CHANNELS
            and ((_this_turn(i, since) and _about(i['words'], pw, aw, strong=False)) or _about(i['words'], pw, aw))]
    made.sort(key=lambda i: abs((i['due'] - (p['at'] or i['due'])).total_seconds()))
    if made:
        when = fmt(made[0]['due'])
        if p['kind'] == 'monitor':
            truth = f"I've set one check for {when}, not a running watch, so a change before then won't reach you."
        else:
            truth = f"I've set a check for {when}, not for the time I said."
        return {'ok': False, 'by': f"only item {made[0]['key']} (made this turn)", 'truth': truth}
    plain = [w for w in have['watches'] if not w['monitor'] and _about(w['words'], pw, aw)]
    if plain and p['kind'] == 'monitor':
        return {'ok': False, 'by': f"only a plain watch {plain[0]['id']}",
                'truth': "I'm checking that page for changes, but only every half hour to a few hours, not closely."}
    return {'ok': False, 'by': 'nothing', 'truth': "That isn't set up, so I won't be watching it." if p['kind'] == 'monitor'
            else "That isn't set up, so I won't be checking back on it."}


def _timed_ok(p, have, pw, aw, since=None) -> bool:
    if p['at'] is None:
        return False
    return any(i['due'] and abs(i['due'] - p['at']) <= SLACK and i['channel'] not in QUIET_CHANNELS
               and (_about(i['words'], pw, aw) or (_this_turn(i, since) and _about(i['words'], pw, aw, strong=False)))
               for i in have['items'])


OFFER = re.compile(r"\b(?:say|tell me|reply|text me|give me|let me know|send me)\b[^.!?]{0,60}\b(?:and|then|so)\s+i(?:'ll| will)\b|\bif you(?:'d| would)? (?:like|want)\b|\bwant me to\b", re.I)
JUDGE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['sentences'], 'properties': {'sentences': {
    'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'required': ['number', 'kind'], 'properties': {
        'number': {'type': 'integer'}, 'kind': {'type': 'string', 'enum': ['watch', 'later', 'none']}}}}}}
JUDGE_INSTRUCTIONS = (
    f'An assistant named {AGENT} is about to send the REPLY below to {OWNER}. Some of its sentences are numbered. For each '
    'numbered sentence say whether it promises work that happens AFTER this reply is sent:\n'
    f'- watch: it says {AGENT} will follow something over time (a flight, a delivery, a price, an inbox) and speak up when it changes.\n'
    f'- later: it says {AGENT} will check, do, send or tell them something at a later time, with or without a time named '
    '("I\'ll text you the landing time", "I\'ll have it ready Monday", "I\'ll follow up with her").\n'
    f'- none: anything else. That includes something {AGENT} is doing in this same reply, an offer that waits for their yes '
    f'("say yes and I\'ll send it"), a question, a statement about what will happen in the world rather than what {AGENT} '
    'will do, and a manner of speaking ("I\'ll be honest", "I\'ll keep it short").\n'
    'Return only the JSON object, one entry per numbered sentence.')


def candidates(reply: str, already: list) -> list:
    """First-person future sentences the word rules did not classify, that are not negated, quoted, conditional, a
    question or an offer waiting on the owner's yes."""
    taken = {p['sentence'] for p in already}
    out = []
    for s in SENT.split(str(reply or '').strip()):
        core = s.strip()
        if (not core or s in taken or core.startswith(('"', '“', 'If ', 'if ')) or core.endswith('?') or NEGATED.search(core)
                or NEGATED_V2.search(core) or OFFER.search(core) or not re.search(FIRST_FUTURE, core, re.I)):
            continue
        if not re.search(r"\bi(?:['’]ll| will| am going to|['’]m going to)\b", core, re.I):
            continue          # "will" about someone or something else, "I'm" plus a state: not the agent promising to do something
        out.append(s)
    return out[:MAX_CANDIDATES]


def ask_judge(llm, reply: str, cands: list, seconds: float, ask=None):
    """-> ({sentence: 'watch'|'later'}, '') or (None, why not). One call with its own wall clock; left behind when it is up.
    ask(instructions, text, schema, seconds) -> (dict | None, model): the engine's own model step; used instead of llm when given."""
    import contextvars
    import threading
    numbered = '\n'.join(f'{n}. {c.strip()}' for n, c in enumerate(cands, 1))
    text = 'THE REPLY:\n' + str(reply).strip()[:3000] + '\n\nNUMBERED SENTENCES:\n' + numbered
    box = {}

    def run():
        try:
            if ask is not None:
                got, _model = ask(JUDGE_INSTRUCTIONS, text, JUDGE_SCHEMA, min(seconds, PROVIDER_TIMEOUT))
                box['parsed'], box['text'] = got, ''
                if got is None:
                    box['error'] = 'no answer from the engine route'
                return
            r = llm.complete_structured(instructions=JUDGE_INSTRUCTIONS, input=[{'type': 'text', 'text': text}], json_schema=JUDGE_SCHEMA,
                                        schema_name='promise_reading', max_tokens=300, timeout=PROVIDER_TIMEOUT, purpose='promise check: which sentences promise later work')
            box['parsed'] = getattr(r, 'parsed', None)
            box['text'] = str(getattr(r, 'text', '') or '')
        except Exception as e:
            box['error'] = type(e).__name__
    worker = threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True, name='promise-judge')
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        return None, f'no answer within {seconds:g} seconds'
    if 'error' in box:
        return None, 'the model call failed (' + box['error'] + ')'
    got = box.get('parsed')
    if not isinstance(got, dict):
        try:
            got = json.loads(box.get('text') or '')
        except ValueError:
            got = None
    rows = got.get('sentences') if isinstance(got, dict) else None
    if not isinstance(rows, list):
        return None, 'the answer was not the object asked for'
    out = {}
    for row in rows:
        try:
            n, kind = int(row.get('number')), str(row.get('kind'))
        except (TypeError, ValueError, AttributeError):
            return None, 'the answer was out of shape'
        if 1 <= n <= len(cands) and kind in ('watch', 'later'):
            out[cands[n - 1]] = kind
    return out, ''


def check(reply: str, life_home, asked: str = '', now: datetime | None = None, tz=None, since=None, llm=None, about_keys=(), v2=None, ask=None):
    """-> (reply to send, findings). The reply is unchanged when every promise is carried by a real record.
    v2=None reads the switch file; llm=None (and no ask) means no candidate is judged."""
    from zoneinfo import ZoneInfo
    tz = tz or ZoneInfo(settings.OWNER_TZ)
    now = now or datetime.now(tz)
    v2 = (Path(life_home) / V2).exists() if v2 is None else bool(v2)
    found = promises(reply, now, v2=v2)
    judged_note = None
    if v2:
        cands = candidates(reply, found)
        if cands and (llm is not None or ask is not None):
            verdicts, why = ask_judge(llm, reply, cands, JUDGE_SECONDS, ask=ask)
            if verdicts is None:
                judged_note = {'sentence': '(candidates not judged)', 'kind': 'note', 'at': None, 'ok': None, 'by': why}
            else:
                order = {s: n for n, s in enumerate(SENT.split(str(reply or '').strip()))}
                for s, kind in verdicts.items():
                    found.append({'sentence': s, 'kind': 'monitor' if kind == 'watch' else 'later', 'at': promised_time(s.strip(), now, True)})
                found.sort(key=lambda p: order.get(p['sentence'], 0))
        elif cands:
            judged_note = {'sentence': '(candidates not judged)', 'kind': 'note', 'at': None, 'ok': None, 'by': 'no way to reach the model from here'}
    if not found:
        return reply, ([judged_note] if judged_note else [])
    have = setup(life_home, tz)
    out, findings = str(reply), ([judged_note] if judged_note else [])
    carried_before = None      # v2: the last promise in this reply that a record carries
    for p in found:
        v = judge(p, have, asked, now, since, life_home, about_keys=tuple(about_keys or ()), v2=v2, reply=str(reply))
        if v2:
            # "I'll start tracking it Wednesday. Then I'll text you if anything changes." The second sentence names
            # nothing of its own: it is about what the first one set up, and is carried by the same record. Only when
            # it opens as a follow-on, shares no two words with any OTHER saved thing, and is not a promise of a set time.
            if v.get('ok') is False and carried_before and FOLLOWS_ON.match(p['sentence'].strip()) and p['at'] is None:
                pw = words(p['sentence'])
                elsewhere = any(len(rec['words'] & pw) >= 2 for rec in list(have['items']) + list(have['watches']))
                if not elsewhere:
                    v = {'ok': True, 'by': 'follows on from the promise before it: ' + str(carried_before)[:120]}
            if v.get('ok') is True and v.get('by'):
                carried_before = v['by']
        findings.append({'sentence': p['sentence'].strip(), 'kind': p['kind'], 'at': p['at'].isoformat() if p['at'] else None, **v})
        if v['ok'] is False:
            out = out.replace(p['sentence'], v['truth'], 1)
    out = DONE_LEAD.sub('', out) if any(f['ok'] is False for f in findings) and DONE_LEAD.match(str(reply)) and DONE_LEAD.match(out) else out
    if v2:        # two promises about the same thing with nothing behind them: say it once
        for truth in {f['truth'] for f in findings if f.get('ok') is False and f.get('truth')}:
            while (truth + ' ' + truth) in out:
                out = out.replace(truth + ' ' + truth, truth)
    return out, findings


def run(reply: str, life_home, asked: str = '', now=None, since=None, llm=None, about_keys=(), ask=None):
    """The plugin's call. -> the reply to send. Never raises; logs every trip."""
    if not on(life_home):
        return reply
    try:
        new, findings = check(reply, life_home, asked, now, since=since, llm=llm, about_keys=about_keys, ask=ask)
    except Exception as e:
        _log(life_home, {'at': time.time(), 'error': f'{type(e).__name__}: {str(e)[:200]}', 'reply': str(reply)[:500]})
        return reply
    if findings:
        _log(life_home, {'at': time.time(), 'asked': str(asked)[:300], 'before': reply, 'after': new, 'findings': findings, 'changed': new != reply})
    return new


def _log(life_home, rec) -> None:
    try:
        with open(Path(life_home) / LOG, 'a') as f:
            f.write(json.dumps(rec, default=str) + '\n')
    except OSError:
        pass

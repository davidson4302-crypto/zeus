"""together: the nightly "how we work together" page and the daily no-correction score.

Each night the day's chats are reviewed and ONE short standing page is kept: boundaries, current frictions, what not
to bring up again. Only that short page goes into prompts, never the nightly notes. It also logs the share of the
owner's messages that needed no correction, a free daily quality score, so drift shows without a costly blind test.

What happens here, once a day, on the first 5-minute tick after 8:30pm on the owner's home clock:
  1. Read the day's direct chat between the owner and Zeus (chats.py, read-only).
  2. ONE model call marks the few places where they corrected Zeus or said how they want things done.
  3. Code checks every item: their exact words must be in the message the model named, or the item is dropped.
     (Text the gateway wraps around a message, such as a photo's attached rules, is cut away first: his_words.)
  4. Code works out the score from the checked items and adds one row to alignment-log.jsonl.
  5. Code rebuilds how-we-work.md from the log. There is no second model call: the page is the log, sorted,
     aged, de-duplicated and cut to size.
Nothing is ever sent, to anyone.

"A day" runs from 8:30pm the evening before to 8:30pm. A calendar day would lose everything they say between the
8:30pm review and midnight, and late-evening messages often carry standing rules.

Files in life_home: together-state.json (which day is done), alignment-log.jsonl (one row per reviewed day; a day
reviewed again adds a row and the later row wins), how-we-work.md (the page).

Retire path (conflicts.py, switch conflicts-on): a boundary that turns out to clash with something else the owner
wants can be set aside. how-we-work-retired.json lists lines that were
set aside: by their own answer to one plain question, or by code when one of their later written rules decides it. The
log row and their words are never touched; the line only leaves the page, with the reason and a pointer kept in that
file. A retired line comes back only when the owner says it again in a new message. No such file: nothing is retired.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chats as C  # noqa: E402
import settings  # noqa: E402

PACIFIC = ZoneInfo(settings.OWNER_TZ)   # the owner's HOME zone (not the travel zone): the nightly day must not shift on trips
ENGINE = os.environ.get('LIFE_ENGINE_DIR', str(settings.ENGINE_DIR))
REVIEW_AT = (20, 30)        # the day closes at 8:30pm, before the 9pm quiet hours
CATCH_UP_DAYS = 3           # a night missed (Mac asleep, model down) is still reviewed for this long, oldest first
RETRY_MINUTES = 15          # after a failed model call wait this long, longer each time, so a dead model cannot
RETRY_MAX_MINUTES = 120     # hold every 5-minute tick for the length of a timeout
STATE, LOG, PAGE = 'together-state.json', 'alignment-log.jsonl', 'how-we-work.md'
RETIRED = 'how-we-work-retired.json'   # lines set aside (conflicts.py), each with the reason and a pointer
PAGE_CAP = 1200             # the whole page, so it is cheap to put in every prompt
LINE_CAP = 140              # one line of the page, before its date; a longer one is refused, never cut short
PROMPT_CAP = 60000          # one model call reads at most this much chat; a longer day is read in parts
HIS_CAP, REPLY_CAP = 3000, 700
LEAD_MINUTES, LEAD_MAX = 60, 6   # a little of what was said just before the day opened, so a late reply makes sense

OWNER = settings.OWNER_FIRST     # how the page and the prompt name the owner
KINDS = ('corrections', 'frictions', 'boundaries', 'do_not_raise')
# kind, heading on the page, heading in a prompt, days a line stays (None = until they say otherwise), most lines
SECTIONS = (('boundaries', 'Boundaries (these stand until they say otherwise)', 'Boundaries, which stand until they say otherwise:', None, 6),
            ('frictions', 'Current frictions (fix these now)', 'Current frictions, fix these now:', 14, 4),
            ('do_not_raise', 'Do not bring up again', 'Do not bring up again:', 60, 4))
WINS_A_DUPLICATE = ('boundaries', 'do_not_raise', 'frictions')    # the same thing said twice shows once, as the firmer kind
CUT_FIRST = ('frictions', 'do_not_raise', 'boundaries')           # when the page is too long, boundaries go last
WEIGHT = {'boundaries': 2.0, 'frictions': 1.0, 'do_not_raise': 1.0}

_ITEM = {'type': 'object', 'required': ['text', 'quote', 'message_id'], 'properties': {
    'text': {'type': 'string', 'description': 'one short plain sentence to Zeus, 15 words or fewer, saying only what their words say'},
    'quote': {'type': 'string', 'description': "the owner's exact words, copied from one of their messages"},
    'message_id': {'type': ['integer', 'string'], 'description': 'the id shown on that message'}}}
SCHEMA = {'type': 'object', 'required': list(KINDS), 'properties': {
    'corrections': {'type': 'array', 'items': {'type': 'object', 'required': ['quote', 'message_id', 'what_to_change'], 'properties': {
        'quote': {'type': 'string', 'description': "the owner's exact words, copied from the message"},
        'message_id': {'type': ['integer', 'string'], 'description': 'the id shown on that message'},
        'what_to_change': {'type': 'string', 'description': 'one short plain sentence: what Zeus should do differently'}}}},
    'frictions': {'type': 'array', 'items': _ITEM},
    'boundaries': {'type': 'array', 'items': _ITEM},
    'do_not_raise': {'type': 'array', 'items': _ITEM}}}

SYSTEM = """You are reading one day of the private chat between the owner and Zeus, their AI assistant. You do this for Zeus, so it can work better with them. Only Zeus's own code reads your answer. You are not summing up the day, and you are not collecting facts about their life or their tasks. You are looking for the few places where the owner told Zeus something about HOW to work with them.

Return four lists. Most days have few entries or none at all, and four empty lists is a normal, correct answer. Never put something in a list just to have something there.

corrections: a message where the owner had to fix, redo, stop or push back on something Zeus did or said. ("No, the other one." "That's wrong." "Stop." "I already told you that." "Way too long.") Their ordinary questions and requests are NOT corrections, even when they are blunt, short, or come one after another. A follow-up question is not a correction. A new request about a new thing is not a correction. Telling Zeus a fact is not a correction. Turning down an offer ("no thanks", "not now") is not a correction. If you are unsure, it is not a correction. At most one entry per message.

frictions: something about the way Zeus works that is annoying them or getting in their way right now (too long, asks permission too often, repeats itself, texts at the wrong time, shows them its inner workings). Only when their own words show it.

boundaries: a standing rule from them about how Zeus must behave from now on ("never", "always", "from now on", a quiet time, what needs their yes first). An instruction for one task is not a boundary.

do_not_raise: a person, company, subject or kind of reminder they want to hear no more about: they told Zeus to stop bringing it up, or said it is handled, is not theirs, or is not wanted ("stop reminding me about X", "Jordan isn't for me"). "Not now", "later" and "ignore for now" are not this.

Rules for every entry:
- `quote` is the owner's own words, copied exactly from ONE of their messages of the day. A short piece is enough (3 to 25 words). Never Zeus's words, never your own wording, never anything from the "Just before" lines.
- `message_id` is the number in [message N] on that same message.
- `text` (or `what_to_change`) is one short plain sentence written as a note to Zeus, saying only what their words say and nothing more: add no condition, exception or reason that is not in that message, even when Zeus's reply or common sense suggests one. One sentence, fifteen words or fewer. Everyday words. No names of files, tools, settings or code. No dashes. Zeus reads the line, the owner does not say it: start with what to do ("Never text them before 9am", "Keep replies short", "The Jordan invoice is not theirs"), call the owner "they" or "them", and never write "I", "me" or "my".
- Every `text` must make sense on its own a month from now, to someone who never saw this chat. Name the thing (the person, the company, the subject); you may take its name from the chat around their message. If their words only point at something ("number 4", "that one", "this") and you cannot name it, leave the entry out.
- Only lines marked OWNER are evidence. Lines marked Zeus are there so you can see what they were reacting to. Some Zeus lines answer messages you cannot see (tests typed by a builder): ignore those. An OWNER line with no message number had no words of their that can be read (a photo, a voice note): it is not evidence either.
- One message may be in two lists when it truly is both (a correction that also sets a standing rule)."""


# ---- time --------------------------------------------------------------------------------------------------------

def _pacific(now) -> datetime:
    if isinstance(now, datetime):
        return now.astimezone(PACIFIC) if now.tzinfo else now.replace(tzinfo=PACIFIC)
    return datetime.fromtimestamp(float(now if now is not None else time.time()), PACIFIC)


def window(day: date) -> tuple:
    """The stretch of chat that counts as this day: 8:30pm the evening before, up to 8:30pm. Built from two wall-clock
    times (not "minus 24 hours") so the days the clocks change still meet end to end."""
    prev = day - timedelta(days=1)
    start = datetime(prev.year, prev.month, prev.day, *REVIEW_AT, tzinfo=PACIFIC)
    end = datetime(day.year, day.month, day.day, *REVIEW_AT, tzinfo=PACIFIC)
    return start.timestamp(), end.timestamp()


def _closed_day(now_dt: datetime) -> date:
    """The latest day whose 8:30pm has passed."""
    today = now_dt.date()
    return today if (now_dt.hour, now_dt.minute) >= REVIEW_AT else today - timedelta(days=1)


def _day_of(ts) -> date:
    return datetime.fromtimestamp(float(ts), PACIFIC).date()


def _date_words(d: date, today: date) -> str:
    return d.strftime('%b ') + str(d.day) + ('' if d.year == today.year else ', %d' % d.year)


# ---- small files -------------------------------------------------------------------------------------------------

def _load(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(p: Path, text: str):
    """Whole file or nothing: a prompt must never read half a page."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(p)


def read_log(life_home) -> list:
    """One row per reviewed day, oldest day first. A day reviewed twice keeps its LATER row (a replay replaces the
    first reading). A line that will not parse is skipped: one bad line must not empty the page."""
    by_day = {}
    try:
        lines = (Path(life_home) / LOG).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(row.get('day') or '')):
            by_day[row['day']] = row
    return [by_day[d] for d in sorted(by_day)]


# ---- which day to review -------------------------------------------------------------------------------------------

def _next_day(state: dict, now_dt: datetime):
    """-> (day to review, None) or (None, why not). Tonight's review never starts before 8:30pm. Once this has run at
    least once, a night that was missed is picked up on the next tick, oldest first, one day per tick."""
    due = _closed_day(now_dt)
    waiting_for_tonight = due < now_dt.date()
    anchor = state.get('last_day')
    failed = state.get('failed') or {}
    if not anchor and failed.get('day'):          # the very first review failed: that day is still owed
        try:
            anchor = (date.fromisoformat(failed['day']) - timedelta(days=1)).isoformat()
        except ValueError:
            anchor = None
    if not anchor:
        return (None, 'early') if waiting_for_tonight else (due, None)   # first run ever: start with tonight
    try:
        last = date.fromisoformat(str(anchor))
    except ValueError:
        return (None, 'early') if waiting_for_tonight else (due, None)
    if last >= due:
        return None, ('early' if waiting_for_tonight else 'already')
    return max(last + timedelta(days=1), due - timedelta(days=CATCH_UP_DAYS - 1)), None


def _note_failure(life: Path, state: dict, day: date, now_dt: datetime):
    """Only the time of the failed try is kept: no log row, no page, and the day is not marked done."""
    old = state.get('failed') or {}
    tries = (int(old.get('tries') or 0) + 1) if old.get('day') == day.isoformat() else 1
    wait = min(RETRY_MINUTES * tries, RETRY_MAX_MINUTES) * 60
    state['failed'] = {'day': day.isoformat(), 'tries': tries, 'at': now_dt.timestamp(), 'retry_at': now_dt.timestamp() + wait}
    _write(life / STATE, json.dumps(state, indent=2))


# ---- only their own words ---------------------------------------------------------------------------------------------
# Some rows that arrive as "a message from the owner" carry text they never typed. A
# photo arrives with pages of Zeus's own operating rules pasted after it ("Never repeat an external effect..."), a
# Slack thread arrives with another bot's message inside, an emailed message arrives with the relay's instructions in
# front ("do not send it again with another tool"). A model could quote any of that, and the quote check would pass
# because the words ARE in the message. So before anything else, each message is cut down to what they typed, and when
# that cannot be told for sure the message has no words at all: it cannot be quoted and is not counted.
EMAILED = re.compile(r'^\[[^\],\]]{1,60}, by email[^\]]*\]')   # the relay's lead-in: "[<owner name>, by email ...]"
STAMPED = re.compile(r'^\[[^\]]{12,}\]|^\(attachment\)|<[a-z][a-z_]{2,}>|Linked native session IDs'
                     r'|\[(?:Thread context|End of thread context|New message|Image attached at|screenshot|The user sent|Photon content)', re.I | re.M)


def his_words(text) -> str:
    """What the owner themselves typed in this message, or '' when it is a photo, a voice note, another sender's words or
    anything else wrapped by the gateway. An emailed message keeps its subject and body and loses the relay's lead-in
    and their signature."""
    t = str(text or '').strip()
    if EMAILED.match(t):
        i = t.find('Subject:')
        if i < 0:
            return ''                               # the relay changed shape: do not guess where their words start
        t = re.split(r'\n-- ?\n', t[i + len('Subject:'):])[0].strip()
    return '' if STAMPED.search(t) else t


# "Ignore item 5 for now" or "Item 4 is handled" is true to the owner's words,
# and meaningless a day later, when "item 4" is something else. A line that points instead of naming is refused.
POINTS_AT = re.compile(r"\b(?:items?|numbers?|no\.|options?|points?|lines?)\s*#?\d+\b|#\d+\b"
                       r"|\bthe (?:first|second|third|fourth|fifth|last|other|same) (?:one|item|thing|two)\b"
                       r"|\b(?:this|that) one\b|\bthe (?:above|following|former|latter)\b", re.I)
# Not "that item" or "this thing": that would refuse a real rule like 'if I say "automate this," own that item'.


FIRST_PERSON = ((r"\bmyself\b", 'themselves'), (r"\bmine\b", 'theirs'), (r"\bmy\b", 'their'), (r"\bme\b", 'them'))


def to_zeus(line: str) -> str:
    """A page line is read by Zeus, so "me" in it would mean the wrong one of them. "me" and "my" become "them" and
    "their"; a line that still says "I" is refused ('' = drop it) rather than rewritten by guesswork."""
    for pat, word in FIRST_PERSON:
        line = re.sub(pat, word, line, flags=re.I)
    if re.search(r"\bI\b|\bI'[a-z]+\b", line):
        return ''
    return line[:1].upper() + line[1:]


# ---- the model call ------------------------------------------------------------------------------------------------

def _real_complete():
    """The live engine's one model door. In the engine folder it is a plain import; from the sandbox it is found by
    path, and Python is told not to leave compiled files behind in the live folder."""
    try:
        import noticer
    except ImportError:
        sys.dont_write_bytecode = True
        sys.path.insert(0, ENGINE)
        import noticer
    return noticer.complete


def _clip(text: str, cap: int) -> str:
    """Zeus's replies can be long and are only context: keep how it started and how it ended."""
    text = str(text).strip()
    if len(text) <= cap:
        return text
    head = cap * 2 // 3
    return text[:head].rstrip() + ' [...] ' + text[-(cap - head):].lstrip()


def _line(m: dict, citeable: bool = True) -> str:
    clock = datetime.fromtimestamp(float(m['at']), PACIFIC).strftime('%a %I:%M%p').replace(' 0', ' ').replace('AM', 'am').replace('PM', 'pm')
    if m['who'] == 'owner':
        if not m['text']:
            return '%s OWNER: (sent something with no words that can be read here)' % clock
        tag = ('[message %s] ' % m['id']) if citeable else ''
        return '%s%s OWNER: %s' % (tag, clock, str(m['text']).strip()[:HIS_CAP])
    return '%s Zeus: %s' % (clock, _clip(m['text'], REPLY_CAP))


def _prompts(msgs: list, lead: list, day: date) -> list:
    """Normally one prompt for the whole day. A very long day is split where it overflows, and each later part is
    shown the last few lines of the part before, so no message is ever quietly left unread."""
    parts, cur, size = [], [], 0
    for m in msgs:
        n = len(_line(m)) + 1
        if cur and size + n > PROMPT_CAP:
            parts.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += n
    if cur:
        parts.append(cur)
    out = []
    for i, part in enumerate(parts):
        if not any(m['who'] == 'owner' and m['text'] for m in part):
            continue                               # Zeus talking with nobody answering: nothing of theirs to review
        before = lead if i == 0 else parts[i - 1][-4:]
        text = 'The day: %s (the owner\'s home time).' % day.strftime('%A %B %d, %Y').replace(' 0', ' ')
        if len(parts) > 1:
            text += ' This is part %d of %d of a long day.' % (i + 1, len(parts))
        if before:
            text += '\n\nJust before (only so the first lines make sense; never quote from these):\n' + '\n'.join(_line(m, False) for m in before)
        text += '\n\nThe chat:\n' + '\n'.join(_line(m) for m in part)
        out.append(text)
    return out


def review(msgs: list, lead: list, day: date, complete) -> dict | None:
    """-> {'out': the four raw lists, 'cost': float | None}, or None when any model call failed (then the whole day
    counts as not reviewed: half a day would make the score look better than it was)."""
    out, cost = {k: [] for k in KINDS}, None
    for prompt in _prompts(msgs, lead, day):
        r = complete(SYSTEM, prompt, SCHEMA)
        if r is None or not isinstance(r.get('out'), dict):
            return None
        for k in KINDS:
            got = r['out'].get(k)
            out[k] += got if isinstance(got, list) else []
        if r.get('cost') is not None:
            cost = (cost or 0) + r['cost']
    return {'out': out, 'cost': cost}


# ---- code has the last word ----------------------------------------------------------------------------------------

NOT_PLAIN = re.compile(r"https?://|~/|/Users/|\.(?:py|json|jsonl|md|ya?ml|sqlite3?|sh)\b|[`{}<>\[\]\\|]|\b[a-z0-9]+_[a-z0-9_]+\b", re.I)


def plain(text, cap: int = LINE_CAP) -> str:
    """A line fit for the page: one short everyday sentence, or '' when it is not one. Dashes become commas (house
    style: no em dashes); anything that looks like a path, a link, code or a field name is refused whole. So is a line
    that is too long: a cut can turn "...changing the calendar or deleting anything" into "...changing the calendar
    or", and a rule with its end missing is a different rule."""
    t = re.sub(r'\s*[—–]\s*|\s+-\s+', ', ', str(text or ''))
    t = re.sub(r'\s+', ' ', t).strip().strip('"“”\'‘’').strip().rstrip('.').strip()
    if len(t) < 8 or len(t) > cap or NOT_PLAIN.search(t):
        return ''
    return t[0].upper() + t[1:]


def verify(out: dict, his: list) -> tuple:
    """Keep only what the owner really said. Each item must name one of THEIR messages of the day, and its quote must be in
    that very message (chats.quoted_in). A quote that is real but sits in another message is dropped too: a wrong
    learned line is worse than a missed one. -> (kept lists, [{'kind', 'why'}] for what was dropped)."""
    by_id = {str(m['id']): m for m in his}
    kept, dropped = {k: [] for k in KINDS}, []
    corrected = set()
    for kind in KINDS:
        items = out.get(kind) if isinstance(out, dict) else None
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                dropped.append({'kind': kind, 'why': 'not an item'})
                continue
            m = by_id.get(str(it.get('message_id')).strip())
            if m is None:
                dropped.append({'kind': kind, 'why': 'no such message from them that day'})
                continue
            quote = re.sub(r'\s+', ' ', str(it.get('quote') or '')).strip()
            if C.quoted_in(quote, [m], m['id']) is None:
                dropped.append({'kind': kind, 'why': 'quote is not in that message'})
                continue
            base = {'quote': quote[:300], 'message_id': m['id'], 'said_at': m['at']}
            if kind == 'corrections':
                if m['id'] in corrected:
                    continue                       # one correction per message; the score counts messages
                corrected.add(m['id'])
                # The score rests on their words, not on how well the model phrased the fix: a clumsy note still counts.
                kept[kind].append({**base, 'what_to_change': plain(it.get('what_to_change'), 240)})
                continue
            text = plain(it.get('text'))
            if not text:
                dropped.append({'kind': kind, 'why': 'the line is not short plain words', 'message_id': m['id']})
                continue
            text = to_zeus(text)
            if not text:
                dropped.append({'kind': kind, 'why': 'the line speaks as them instead of to Zeus', 'message_id': m['id']})
                continue
            if POINTS_AT.search(text):
                dropped.append({'kind': kind, 'why': 'the line points at something without naming it', 'message_id': m['id']})
                continue
            kept[kind].append({**base, 'text': text})
    return kept, dropped


def rate(his_count: int, corrected_count: int):
    """Share of their messages that needed no correction. No messages means no score, not a perfect one."""
    if his_count <= 0:
        return None
    return round((his_count - min(corrected_count, his_count)) / his_count, 4)


# ---- the page, built by code from the log ----------------------------------------------------------------------------

FILLER = {'the', 'a', 'an', 'to', 'of', 'and', 'or', 'me', 'my', 'i', 'you', 'your', 'about', 'that', 'this', 'it', 'is', 'are',
          'be', 'for', 'with', 'on', 'in', 'at', 'him', 'his', 'he', 'she', 'her', 'they', 'them', 'their', 'owner', 'zeus', 'please', 'any', 'again', 'more',
          settings.OWNER_FIRST.lower()}
SAYS_NO = {'not', 'no', 'never', "don't", 'dont', 'stop', 'without', 'nothing', "isn't", "won't", "doesn't", "can't"}


def _words(s: str) -> set:
    out = set()
    for w in re.findall(r"[a-z0-9']+", str(s).lower()):
        if w in FILLER:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith('s') and not w.endswith('ss') else w)
    return out


def same(a: str, b: str) -> bool:
    """Two lines that say the same thing in nearly the same words. Lines that differ by a "never" or by a number
    ("before 9am" / "before 8am") are never the same: merging those would turn a rule into its opposite."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if bool(wa & SAYS_NO) != bool(wb & SAYS_NO):
        return False
    if {w for w in wa if re.search(r'\d', w)} != {w for w in wb if re.search(r'\d', w)}:
        return False
    shared = len(wa & wb)
    if shared / len(wa | wb) >= 0.6:
        return True
    if min(len(wa), len(wb)) >= 3 and shared / min(len(wa), len(wb)) >= 0.85:
        return True
    return SequenceMatcher(None, ' '.join(sorted(wa)), ' '.join(sorted(wb))).ratio() >= 0.88


def retired_marks(life_home) -> set:
    """(kind, message id, text) of every line set aside. No file, or one that will not parse: nothing is."""
    try:
        d = json.loads((Path(life_home) / RETIRED).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    return {(str(x.get('kind')), str(x.get('message_id')), str(x.get('text'))) for x in (d.get('lines') if isinstance(d, dict) else None) or []
            if isinstance(x, dict)}


def standing(rows: list, today: date, retired=None) -> dict:
    """What is on the page today: {kind: [{'text', 'said', 'message_id', 'quote', 'said_at'}], newest first}. A
    friction drops off 14 days after they last said it and a "do not bring up" after 60; a boundary stays. The same
    thing said again shows once, with its latest date, so a friction they repeat stays on the page. A line in
    `retired` (retired_marks) is left out; the same words said in a new message are a new line and show."""
    retired = retired or set()
    found = {}
    for kind, _h, _p, keep_days, _most in SECTIONS:
        items = []
        for row in rows:
            for it in row.get(kind) or []:
                if not (isinstance(it, dict) and it.get('text') and it.get('said_at') is not None):
                    continue
                try:
                    said = _day_of(it['said_at'])
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
                if keep_days is not None and (today - said).days > keep_days:
                    continue
                if (kind, str(it.get('message_id')), str(it['text'])) in retired:
                    continue
                items.append((float(it['said_at']), str(it['text']), said, it))
        found[kind] = sorted(items, key=lambda x: (x[0], x[1]), reverse=True)
    seen, out = [], {}
    for kind in WINS_A_DUPLICATE:
        out[kind] = []
        for at, text, said, it in found[kind]:
            if any(same(text, s) for s in seen):
                continue
            seen.append(text)
            out[kind].append({'text': text, 'said': said, 'message_id': it.get('message_id'), 'quote': it.get('quote'), 'said_at': at})
    most = {kind: n for kind, _h, _p, _d, n in SECTIONS}
    return {kind: out[kind][:most[kind]] for kind in out}


def _layout(lines: dict, today: date, markdown: bool) -> str:
    if not any(lines.values()):
        return ('# How we work together\n\nNothing here yet. This page fills in as ' + OWNER + ' tells Zeus how they want things done.\n'
                if markdown else '')
    parts = ['# How we work together\n\nWhat ' + OWNER + ' has told Zeus about working with them. Updated %s.' % _date_words(today, today)
             if markdown else 'How ' + OWNER + ' wants you to work with them (from their own words; the date is when they said it):']
    for kind, heading, prompt_heading, _d, _n in SECTIONS:
        if lines.get(kind):
            body = '\n'.join('- %s (%s)' % (it['text'], _date_words(it['said'], today)) for it in lines[kind])
            parts.append(('## %s\n%s' % (heading, body)) if markdown else ('%s\n%s' % (prompt_heading, body)))
    return ('\n\n' if markdown else '\n').join(parts) + ('\n' if markdown else '')


def render(lines: dict, today: date, cap: int = PAGE_CAP, markdown: bool = True) -> str:
    """The page as text, never longer than cap. Whole lines are removed, never half a line: the fullest section
    gives up its oldest line first, and a boundary counts for half so boundaries are the last to go."""
    lines = {k: list(v) for k, v in lines.items()}
    while True:
        text = _layout(lines, today, markdown)
        if len(text) <= cap:
            return text
        if not any(lines.values()):
            return ''
        kind = max((k for k in CUT_FIRST if lines.get(k)), key=lambda k: (len(lines[k]) / WEIGHT[k], -CUT_FIRST.index(k)))
        lines[kind].pop()


def as_text(life_home, limit: int = 900, now=None) -> str:
    """The page as plain lines for a prompt, or '' when there is nothing on it. Built from the log each time, so a
    friction ages off on its own day even if a nightly review was missed. Never raises: a prompt must not fail
    because of this page."""
    try:
        return render(standing(read_log(life_home), _pacific(now).date(), retired_marks(life_home)), _pacific(now).date(), cap=limit, markdown=False)
    except Exception:
        return ''


def retire(life_home, kind: str, text: str, message_id, why: str, pointer: dict, now=None) -> bool:
    """Set one line of the page aside. Its log row and their words stay as they are; the line, the reason and a
    pointer to what won are added to how-we-work-retired.json, and the page is rebuilt at once, so the chat and the
    pipes stop reading it now, not tomorrow night. -> False when it was already set aside."""
    life = Path(life_home)
    p = life / RETIRED
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        d = d if isinstance(d, dict) and isinstance(d.get('lines'), list) else {'lines': []}
    except OSError:
        d = {'lines': []}
    except ValueError:
        raise RuntimeError('the set-aside list will not parse; left alone')   # never written over: it holds their answers
    mark = (str(kind), str(message_id), str(text))
    if any((str(x.get('kind')), str(x.get('message_id')), str(x.get('text'))) == mark for x in d['lines'] if isinstance(x, dict)):
        return False
    now_dt = _pacific(now)
    d['lines'].append({'kind': kind, 'text': text, 'message_id': message_id, 'why': why, 'pointer': pointer, 'at': now_dt.timestamp()})
    _write(p, json.dumps(d, indent=2, ensure_ascii=False, default=str))
    _write(life / PAGE, render(standing(read_log(life), now_dt.date(), retired_marks(life)), now_dt.date()))
    return True


def score(life_home, days: int = 7, now=None) -> dict:
    """The no-correction score for a status line: each reviewed day, and the average over the last `days` days. The
    average is over their messages (corrections / messages), so a two-message day does not weigh as much as a
    forty-message one. Days with no message from them have no rate and do not count."""
    today = _pacific(now).date()
    first = (today - timedelta(days=days - 1)).isoformat()
    daily = [{'day': r['day'], 'his_messages': int(r.get('his_messages') or 0), 'corrections': int(r.get('corrections') or 0),
              'correction_free_rate': r.get('correction_free_rate')}
             for r in read_log(life_home) if first <= r['day'] <= today.isoformat()]
    msgs = sum(d['his_messages'] for d in daily)
    corr = sum(min(d['corrections'], d['his_messages']) for d in daily)
    avg = rate(msgs, corr)
    if avg is None:
        line = 'No chats with them reviewed in the last %d days.' % days
    else:
        line = 'No correction needed on %d of their %d messages in the last %d days (%d%%).' % (msgs - corr, msgs, days, round(avg * 100))
    return {'days': daily, 'average': avg, 'his_messages': msgs, 'corrections': corr, 'days_reviewed': len(daily), 'line': line}


# ---- the nightly run -----------------------------------------------------------------------------------------------

def _own_words(msgs: list) -> list:
    return [{**m, 'text': his_words(m['text'])} if m['who'] == 'owner' else m for m in msgs]


def run(now, life_home, complete=None, dry: bool = False, day=None, path=None) -> dict:
    """Call this from every tick. It does its work at most once a day, on the first call after 8:30pm (home clock), and
    returns at once the rest of the time. It never sends anything.

    now       the time of the tick (a datetime or epoch seconds)
    complete  the model door (default: the live engine's noticer.complete). None from it = failed: nothing is
              written except the time of the try, and a later tick tries again.
    dry       work everything out and write nothing; the row and the page come back in the report.
    day       'YYYY-MM-DD' reviews that day again whatever the clock says. It adds a log row and rebuilds the page
              but leaves together-state.json alone, so tonight's own review still happens.
    path      another chat store (tests).
    -> {'status': 'early' | 'already' | 'waiting' | 'model_failed' | 'no_messages' | 'done', ...counts}
    his_messages counts messages with words of theirs that could be read; a photo or voice note with no text is
    counted apart as no_words and is left out of the score (nobody can tell whether it needed a correction)."""
    now_dt = _pacific(now)
    life = Path(life_home)
    replay = day is not None
    state = {} if replay else _load(life / STATE)
    if replay:
        target = date.fromisoformat(str(day))
    else:
        target, why_not = _next_day(state, now_dt)
        if target is None:
            return {'status': why_not}
        failed = state.get('failed') or {}
        if failed.get('day') == target.isoformat() and now_dt.timestamp() < float(failed.get('retry_at') or 0):
            return {'status': 'waiting', 'day': target.isoformat(), 'tries': failed.get('tries')}
    since, until = window(target)
    until = min(until, now_dt.timestamp())          # replaying today sees only what has been said so far
    msgs = _own_words(C.messages(since, until, path=path))   # a store that cannot be read raises: never "a quiet day"
    his = [m for m in msgs if m['who'] == 'owner' and m['text']]
    no_words = sum(1 for m in msgs if m['who'] == 'owner' and not m['text'])
    report = {'status': 'done', 'day': target.isoformat(), 'his_messages': len(his), 'no_words': no_words, 'dry': bool(dry), 'replay': replay}
    kept, dropped, cost = {k: [] for k in KINDS}, [], None
    if his:
        lead = _own_words(C.messages(since - LEAD_MINUTES * 60, since, path=path))[-LEAD_MAX:]
        got = review(msgs, lead, target, complete or _real_complete())
        if got is None:
            if not dry and not replay:
                _note_failure(life, state, target, now_dt)
            return {**report, 'status': 'model_failed'}
        kept, dropped = verify(got['out'], his)
        cost = got['cost']
    else:
        report['status'] = 'no_messages'            # a row is still written, so the day shows as looked at
    corrected = len({c['message_id'] for c in kept['corrections']})
    row = {'day': target.isoformat(), 'his_messages': len(his), 'no_words': no_words, 'corrections': corrected,
           'correction_free_rate': rate(len(his), corrected), 'correction_items': kept['corrections'],
           'frictions': kept['frictions'], 'boundaries': kept['boundaries'], 'do_not_raise': kept['do_not_raise'],
           'dropped': dropped, 'window': [since, until], 'at': now_dt.timestamp(), 'cost': cost}
    rows = [r for r in read_log(life) if r['day'] != row['day']] + [row]
    page = render(standing(rows, now_dt.date(), retired_marks(life)), now_dt.date())
    report.update({'corrections': corrected, 'correction_free_rate': row['correction_free_rate'],
                   'kept': {k: len(kept[k]) for k in KINDS}, 'dropped': len(dropped), 'cost': cost})
    if dry:
        return {**report, 'row': row, 'page': page}
    life.mkdir(parents=True, exist_ok=True)
    with open(life / LOG, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    _write(life / PAGE, page)
    if not replay:                                   # last, so a crash before this point only means one more review
        _write(life / STATE, json.dumps({'last_day': target.isoformat(), 'done_at': now_dt.timestamp()}, indent=2))
    return report


def main():
    """Sandbox use only (the live call is run() from the tick):
      python3 -I together.py --home DIR --day YYYY-MM-DD [--dry]     review one day into DIR
      python3 -I together.py --home DIR --days 5 [--dry]             review each of the last 5 days into DIR
      python3 -I together.py --home DIR --score | --page             read what is there"""
    args = sys.argv[1:]

    def value(flag):
        return args[args.index(flag) + 1] if flag in args else None

    home = value('--home')
    if not home:
        print('--home is required, so nothing here can write into the live folder by accident', file=sys.stderr)
        sys.exit(2)
    now_dt = datetime.now(PACIFIC)
    if '--score' in args:
        print(json.dumps(score(home, now=now_dt), indent=1))
        return
    if '--page' in args:
        print(as_text(home, now=now_dt))
        return
    if value('--day'):
        days = [value('--day')]
    else:
        n = int(value('--days') or 1)
        last = now_dt.date()                         # today's window, as far as it has got
        days = [(last - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]
    for d in days:
        r = run(now_dt, home, dry='--dry' in args, day=d)
        print(json.dumps({k: v for k, v in r.items() if k not in ('row', 'page')}, default=str))


if __name__ == '__main__':
    main()

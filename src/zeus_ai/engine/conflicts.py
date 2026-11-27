"""conflicts: two standing things Zeus keeps that cannot both be followed get settled, or become ONE plain question.

The memory ladder (ladder.py) settles a clash INSIDE what Zeus learned from chat, when the owner corrects themselves.
This file compares ACROSS the places Zeus keeps what it knows: the how-we-work page (together.py; its boundaries can
be set aside here), the owner's standing rules and notes (preferences.json), the plans they set up for things that
come in (standing-rules.json), standing OKs (grants.json), the refill dates (refills.json), the purchase lock, and
Zeus's own long-term memory (MEMORY.md, read only: this file never writes it). Example: a boundary "Never buy anything
for me" next to a pharmacy refill plan the owner set up earlier.

Once a night, on the first tick after the nightly page is rebuilt (together.py, 8:30pm), at most:
  code   compares typed things. DECISIONS below are the owner's own dated written rules (from the config: tools they
         retired, no phone calls, quiet hours with no exceptions, ...). A line anywhere that still says to use one of
         those, and is older than the rule, is SETTLED BY CODE without asking: the later rule wins. The line is never
         deleted. A page line, a rule, a plan or a standing OK is set aside with the reason and a pointer (archive); a
         note or a MEMORY.md line cannot be changed here, so it is only recorded, and the rule that wins is told to the
         noticer and the background jobs in one plain line.
  model  ONE judge call (the engine's own model door, noticer.complete, like ladderjudge) reads the prose items and
         names the few groups that truly pull against each other, with a severity and one plain yes-or-no question.
         Skipped when nothing changed since the last call. Code checks every id, the confidence floor (0.7), that the
         question is plain words naming both sides, and that answering would change something.
  ask    only a HIGH-stakes contradiction becomes ONE question: an ordinary life item, so the note writer the owner
         already reads words it, timed by policy.decide like an offer (the next 9:00am note, never in quiet hours). It
         shares the offers' once-a-day slot (never while an offer or another question is open, never on a day an offer
         was made) and is asked at most once a week. Everything else is recorded in conflicts.json and never shown:
         there is no separate "to settle" page.
  yes/no `life approve <item> "<the owner's words>"`. Code reads the owner's REAL message since the ask (hisreply.py:
         a clear yes plus filler is a yes; "sounds good" only right after this ask; "ok" is not). A yes keeps the second
         side, a no keeps the first; the other side is set aside with the owner's words and a pointer, through the same
         paths code uses. `life drop` = both stay and this pair is never asked again ("not a conflict"); no answer in 7
         days = closed, never asked again.

Switch (a file in the life folder): conflicts-on. Without it none of this runs, and the engine, the page and every
prompt are unchanged. With it the settled lines also reach the noticer and the background jobs. Files: conflicts.json
(what was found and what happened), conflicts-log.jsonl (every change), how-we-work-retired.json (together.py),
grants-archive.json. Nothing here sends anything, ever.

Config: `conflicts.decisions` (list of tables), each {id, date (YYYY-MM-DD), owner_words, find (regex), agrees (regex,
or "STOPS" for the built-in stop words), says, needs (optional switch file), mode ('retire' | 'override')}. Empty by
default: then only the model judge runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402

PACIFIC = ZoneInfo(settings.OWNER_TZ)   # the owner's clock (name kept for callers)
OWNER = settings.OWNER_FIRST
SWITCH = 'conflicts-on'
PROMPT_SWITCH = 'conflicts-on'
STATE, LOG, GRANTS_ARCHIVE = 'conflicts.json', 'conflicts-log.jsonl', 'grants-archive.json'
LIVE_LIFE = settings.LIFE_HOME
LIVE_MEMORY = settings.MEMORY_FILE
PROMPT_VERSION = 'conflicts-judge-2'
TIMEOUT = 120
CONFIDENCE_FLOOR = 0.7       # a contradiction the judge is less sure of than this is left out
ASK_EVERY_DAYS = 7           # at most one question a week
ANSWER_DAYS = 7              # told and unanswered this long: closed, never asked again (same as the offers' wait)
LATE_HOURS = 3               # the nightly page normally comes first; if it has not by 11:30pm, the pass runs anyway
MEMORY_ITEM_CAP = 700        # one MEMORY.md entry as the judge sees it
PROMPT_CAP = 700
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
JUST_ASKED = 15 * 60         # "sounds good" answers this ask only this soon after it (as in hisreply / onesend)
RETIRABLE = ('boundary', 'do_not_raise', 'rule', 'plan', 'grant')

# ---- the owner's written rules that decide a clash without asking ------------------------------------------------------
# Each is the owner's own dated rule, from the config. `find` says a line is about it; `agrees` (in the same clause)
# says the line itself states the stop, so it is no clash. `says` is the one line the pipes get when it wins.
# `needs`: a switch file that must be present for the rule to be how the engine behaves today.
# `mode`: 'retire' = a line whose whole point is the thing the owner ended may be set aside; 'override' = the rule
# changes a detail of lines that are otherwise right (a time zone, what counts as urgent), so they are never set aside:
# the rule that wins is recorded and told to the pipes instead.
STOPS = (r"\b(?:never|not|no longer|don'?t|do not|stop(?:ped)?|disconnect(?:ed)?|retired?|removed?|off the radar|isn'?t ours|"
         r"someone else|nothing|no)\b")


def _decisions() -> tuple:
    """The configured decisions, checked: a broken entry (no id, bad date, bad regex) is left out, never guessed."""
    out = []
    for d in settings.get('conflicts.decisions', []) or []:
        if not isinstance(d, dict):
            continue
        try:
            date.fromisoformat(str(d.get('date')))
            agrees = STOPS if str(d.get('agrees') or 'STOPS') == 'STOPS' else str(d['agrees'])
            re.compile(str(d['find']))
            re.compile(agrees)
        except (KeyError, ValueError, re.error):
            continue
        if not d.get('id') or not d.get('says'):
            continue
        out.append({'id': 'D-' + str(d['id']).removeprefix('D-'), 'date': str(d['date']),
                    'his_words': str(d.get('owner_words') or d.get('his_words') or ''), 'find': str(d['find']), 'agrees': agrees,
                    'says': str(d['says'])} | ({'needs': str(d['needs'])} if d.get('needs') else {})
                   | ({'mode': str(d['mode'])} if d.get('mode') in ('retire', 'override') else {}))
    return tuple(out)


DECISIONS = _decisions()
NOT_FOR_HIS_EYES = re.compile(r"[`{}\[\]<>_|\\]|https?://|\.(?:py|json|jsonl|md)\b|\b[A-Z]-?\d{1,3}\b|\bD-[a-z]|\b(?:json|schema|cron|ledger|"
                              r"switch file|preferences file|standing-rules|memory file|grants?\.json)\b", re.I)
COMMON = frozenset('''the and you for but not are was can all any out one our own has had get got too now yes way who why how day
did set put use may its let him his her she they them that this with what when then than from have here into just keep kept like
jan feb mar apr jun jul aug sep oct nov dec mon tue wed thu fri sat sun
about after again also always another anything around because before being between both could does doing
every last made make more most note notes only other over said same says should since
some still such than that their them then there these they thing things this those through time today told until very want what when
where which while will with without would your yours zeus his him he she her owner'''.split()) | {OWNER.lower()}
MONTHS = {m: i for i, m in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}


def on(life) -> bool:
    return (Path(life) / SWITCH).exists()


def in_prompts(life) -> bool:
    return on(life) and (Path(life) / PROMPT_SWITCH).exists()


def _now_dt(now) -> datetime:
    if isinstance(now, datetime):
        return now.astimezone(PACIFIC) if now.tzinfo else now.replace(tzinfo=PACIFIC)
    return datetime.fromtimestamp(float(now if now is not None else time.time()), PACIFIC)


def _day(value):
    """A date from an ISO string, epoch seconds, or None."""
    if value is None or value == '':
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), PACIFIC).date()
        s = str(value)
        if re.fullmatch(r'\d+(?:\.\d+)?', s):
            return datetime.fromtimestamp(float(s), PACIFIC).date()
        return date.fromisoformat(s[:10])
    except (ValueError, OverflowError, OSError):
        return None


def _words_day(d) -> str:
    return d.strftime('%b ') + str(d.day) if d else ''


def latest_date_in(text, today=None):
    """The latest date written in a MEMORY.md entry ("Oct 17, 2030", "Nov 12 2030", "2030-11-02"), or None. A date
    after today is a plan in the line (a flight on Nov 28), not when it was written, so it does not count."""
    found = []
    for y, m, d in re.findall(r'\b(20\d\d)-(\d\d)-(\d\d)\b', str(text)):
        try:
            found.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for mon, d, y in re.findall(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? (\d{1,2}),? (20\d\d)\b', str(text)):
        try:
            found.append(date(int(y), MONTHS[mon.lower()], int(d)))
        except ValueError:
            pass
    found = [d for d in found if today is None or d <= today]
    return max(found) if found else None


# ---- small files --------------------------------------------------------------------------------------------------------

def _read(p: Path, default):
    try:
        return json.loads(Path(p).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _write(p: Path, data):
    p = Path(p)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    tmp.replace(p)


def _state(life) -> dict:
    d = _read(Path(life) / STATE, {})
    d = d if isinstance(d, dict) else {}
    d.setdefault('findings', {})
    return d


def _log(life, what: str, key: str, **more):
    with open(Path(life) / LOG, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps({'at': datetime.now(PACIFIC).isoformat(timespec='seconds'), 'what': what, 'key': key, **more},
                            ensure_ascii=False, default=str) + '\n')


def memory_path(life):
    """Zeus's own MEMORY.md, read only. A sandbox reads only a copy named in LIFE_MEMORY_MD, never the live file."""
    named = os.environ.get('LIFE_MEMORY_MD')
    if named:
        return Path(named)
    return LIVE_MEMORY if Path(life).resolve() == LIVE_LIFE.resolve() else None


# ---- what is kept, from every place ----------------------------------------------------------------------------------

def _fp(store: str, text: str) -> str:
    return store + ':' + hashlib.sha1(' '.join(str(text).split()).encode()).hexdigest()[:12]


def collect(life, now=None, memory=None) -> list:
    """Every standing thing Zeus keeps, as plain items: {id, store, kind, text, quote, day, ref, fp, label}. Read only."""
    import together as T
    life = Path(life)
    now_dt = _now_dt(now)
    items = []

    def add(prefix, kind, store, text, label, day=None, quote=None, ref=None):
        text = ' '.join(str(text or '').split())
        if not text:
            return
        items.append({'id': f'{prefix}{sum(1 for i in items if i["id"].startswith(prefix)) + 1}', 'kind': kind, 'store': store, 'text': text,
                      'quote': quote, 'day': day.isoformat() if day else None, 'ref': ref or {}, 'fp': _fp(store, text), 'label': label})

    try:
        page = T.standing(T.read_log(life), now_dt.date(), T.retired_marks(life))
    except Exception:
        page = {}
    for kind, prefix in (('boundaries', 'B'), ('do_not_raise', 'X')):
        for it in page.get(kind) or []:
            k = 'boundary' if kind == 'boundaries' else 'do_not_raise'
            add(prefix, k, 'page:' + kind, it['text'], f'what {OWNER} said on {_words_day(it["said"])}: "{it.get("quote") or it["text"]}"',
                it['said'], it.get('quote'), {'kind': kind, 'message_id': it.get('message_id'), 'text': it['text']})
    prefs = _read(life / 'preferences.json', {})
    prefs = prefs if isinstance(prefs, dict) else {}
    plans = [r for r in (_read(life / 'standing-rules.json', []) or []) if isinstance(r, dict) and r.get('state') in ('confirmed', 'adopted')]
    plan_lines = {str(r.get('pref_text')) for r in plans if r.get('pref_text')}
    import ladder as LD
    for r in (prefs.get('rules') if isinstance(prefs.get('rules'), list) else []):
        if not (isinstance(r, dict) and r.get('text')) or str(r['text']) in plan_lines:
            continue                    # a plan's own line is the plan, below
        if LD._day(r.get('expires')) and LD._day(r['expires']) < now_dt.date():
            continue
        d = _day(r.get('at'))
        mail = str(r['text']).startswith(LD.ADOPTED)
        add('R', 'rule', 'rules', r['text'], (f'a habit Zeus picked up from mail on {_words_day(d)}' if mail else
                                              f'{OWNER}\'s rule from {_words_day(d)}: {str(r["text"]).rstrip(".")}'),
            d, r.get('quote'), {'rule_id': LD.rule_id(r)})
    for n in (prefs.get('notes') if isinstance(prefs.get('notes'), list) else []):
        if isinstance(n, dict) and n.get('text'):
            d = _day(n.get('at'))
            add('N', 'note', 'notes', n['text'], f'a note from {_words_day(d)}', d)
    for m in (prefs.get('muted') if isinstance(prefs.get('muted'), list) else []):
        if isinstance(m, dict):
            add('U', 'muted', 'muted', m.get('note') or m.get('title'), f'something {OWNER} said to stop raising: {m.get("title")}', _day(m.get('at')))
    for r in plans:
        d = _day(r.get('confirmed_at') or r.get('created'))
        text = f"When {str(r.get('when') or '').rstrip('.')}: {r.get('action') or ''}"
        when = str(r.get('when') or '').rstrip('.')
        add('S', 'plan', 'plans', text, (f'what {OWNER} set up on {_words_day(d)} for when {when}' if r.get('state') == 'confirmed'
                                         else f'a habit Zeus picked up from mail on {_words_day(d)}: {r.get("kind")}'),
            d, r.get('his_words'), {'key': r.get('key'), 'pref_text': r.get('pref_text')})
    grants = _read(life / 'grants.json', {})
    for fam, g in (grants.items() if isinstance(grants, dict) else []):
        if isinstance(g, dict):
            d = _day(g.get('said_at') or g.get('at'))
            add('G', 'grant', 'grants', f"Standing OK, no need to ask: {g.get('title') or fam}. {OWNER}'s words: \"{g.get('his_words') or g.get('note') or ''}\"",
                f'{OWNER}\'s standing OK from {_words_day(d)} for {g.get("title") or fam}', d, g.get('his_words'), {'family': fam})
    refills = _read(life / 'refills.json', {})
    if isinstance(refills, dict) and refills.get('refills'):
        names = ', '.join(str(x.get('name')) for x in refills['refills'] if isinstance(x, dict) and x.get('name'))
        add('F', 'refills', 'refills', f'Zeus keeps refill dates for {OWNER}\'s medicines ({names}), worked out from their pharmacy history.',
            f'the refill dates Zeus keeps for {OWNER}\'s medicines', _day(refills.get('made')))
    if (life / 'buy-lock-heartbeat.json').exists() and not (life / 'buy-lock-off').exists():
        hb = _read(life / 'buy-lock-heartbeat.json', {})
        add('L', 'lock', 'lock', f'The purchase lock is on: an order is placed only after {OWNER}\'s yes to that store and that total, once.',
            f'the purchase lock (an order needs {OWNER}\'s yes to that store and total)', _day((hb or {}).get('armed_at')))
    path = memory if memory is not None else memory_path(life)
    if path:
        try:
            raw = Path(path).read_text(encoding='utf-8')
        except OSError:
            raw = ''
        for first, chunk in memory_entries(raw):
            add('M', 'memory', 'memory', chunk, f'an older line in Zeus\'s own memory (line {first})', latest_date_in(chunk, now_dt.date()), ref={'line': first})
    return items


def memory_entries(raw: str) -> list:
    """MEMORY.md entries, which Hermes separates with a line holding only "§": [(first line number, text)]."""
    out, cur, start = [], [], None
    for n, ln in enumerate(str(raw).splitlines(), 1):
        if ln.strip() == '§':
            if cur:
                out.append((start, '\n'.join(cur).strip()))
            cur, start = [], None
            continue
        if ln.strip() and start is None:
            start = n
        if start is not None:
            cur.append(ln)
    if cur:
        out.append((start, '\n'.join(cur).strip()))
    return out


def active_decisions(life) -> list:
    return [d for d in DECISIONS if not d.get('needs') or (Path(life) / d['needs']).exists()]


# ---- code: the owner's written rules decide--------------------------------------------------------------------------------

def clauses(text) -> list:
    return [c for c in re.split(r'[.;:!?\n]+', str(text)) if c.strip()]


def code_findings(items: list, life) -> list:
    """A line still saying to use something the owner's later written rule ended. No model. -> findings settled by code."""
    out = []
    for d in active_decisions(life):
        when = date.fromisoformat(d['date'])
        for it in items:
            hits = [c for c in clauses(it['text']) if re.search(d['find'], c, re.I)]
            if not hits or all(re.search(d['agrees'], c, re.I) for c in hits):
                continue
            said = _day(it['day'])
            newer = bool(said and said > when)
            # Set aside only a line whose whole point is the ended thing (one clause, a kind code can archive).
            # A longer line is right apart from that detail: it stays, and the winning rule is told to the pipes.
            whole = d.get('mode', 'retire') == 'retire' and it['kind'] in RETIRABLE and len(clauses(it['text'])) == 1
            out.append({'key': pair_key([d['id']], [it['fp']]), 'how': 'code', 'side_a': [it['id']], 'side_b': [d['id']],
                        'decision': d['id'], 'subject': d['id'][2:], 'kind': 'contradiction', 'severity': 'medium',
                        'why': (f'{OWNER}\'s own newer words ({_words_day(said)}) mention it after the {_words_day(when)} rule, so code does not decide it'
                                if newer else f'{OWNER}\'s written rule of {_words_day(when)} ("{d["his_words"]}") is later than this line'),
                        'clause': [c.strip()[:200] for c in hits if not re.search(d['agrees'], c, re.I)][:2],
                        'state': 'seen' if newer else 'settled', 'action': 'set aside' if whole else 'override'})
    return out


def pair_key(a_fps: list, b_fps: list) -> str:
    """One pair, whichever side is called first (each pair is filed once)."""
    sides = sorted(['+'.join(sorted(a_fps)), '+'.join(sorted(b_fps))])
    return 'c-' + hashlib.sha1('||'.join(sides).encode()).hexdigest()[:12]


# ---- the model judge for prose ----------------------------------------------------------------------------------------

SYSTEM = """You compare the standing things Zeus ({owner}'s AI assistant) keeps about how to work for {owner}. They come from different places: lines from the nightly "how we work together" page ({owner}'s own words), {owner}'s standing rules, plans {owner} set up for things that come in, standing OKs, notes, Zeus's own long-term memory, and plain facts about how Zeus works. Find the few places where two of them cannot both be followed: doing what one says would break the other. Most nights there are none, and an empty list is a normal, correct answer.

Not a conflict:
- the same thing said twice, or in other words;
- one is narrower and fits inside the other ("never buy without a yes" and "ask for one yes before placing the order" fit together);
- a fact that simply changed over time, where the newer one plainly replaces the older (a flight that moved): give kind "updated";
- things about different subjects, or a fixed rule (listed separately) that already settles the matter.

For each conflict:
- side_a, side_b: the ids on each side. An id is on one side only. Never use a fixed rule's id.
- subject: a few plain words.
- kind: contradiction, updated or overlap.
- severity: high when it touches money, medicine, messages to other people, deleting, {owner}'s calendar, or anything Zeus might refuse to do, or do wrongly, because of it. medium: a value that may be stale. low: wording only.
- why: one plain sentence.
- Put on side_b the more specific thing {owner} set up on purpose (a plan, a standing OK, a rule for one kind of thing), and on side_a the broader or passing remark it clashes with.
- question: ONE plain yes-or-no question to {owner} that the answer settles: a yes keeps side_b and sets side_a aside, a no does the opposite. Shape: what side_a says and when, what side_b does and when, then "keep" side_b? For example: "On Nov 2 you said never to text you about school, but your Nov 5 plan has me text you when a grade posts. Keep that plan?" Write it to {owner} as "you", in everyday words, the way a friend would ask. No file names, settings, codes or ids. Under 220 characters.
- confidence: 0 to 1. Leave out anything below 0.7.""".replace('{owner}', OWNER)

SCHEMA = {'type': 'object', 'required': ['conflicts'], 'properties': {'conflicts': {'type': 'array', 'items': {
    'type': 'object', 'required': ['side_a', 'side_b', 'subject', 'kind', 'severity', 'why', 'question', 'confidence'], 'properties': {
        'side_a': {'type': 'array', 'items': {'type': 'string'}}, 'side_b': {'type': 'array', 'items': {'type': 'string'}},
        'subject': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['contradiction', 'updated', 'overlap']},
        'severity': {'type': 'string', 'enum': ['high', 'medium', 'low']}, 'why': {'type': 'string'}, 'question': {'type': 'string'},
        'confidence': {'type': 'number'}}}}}}
STORE_WORDS = {'page:boundaries': f"how-we-work page, a boundary in {OWNER}'s own words", 'page:do_not_raise': 'how-we-work page, do not bring up again',
               'rules': f"{OWNER}'s standing rule", 'notes': 'a note', 'muted': f'{OWNER} said to stop raising this', 'plans': 'a plan for things that come in',
               'grants': 'a standing OK', 'refills': 'refill dates', 'lock': 'how Zeus works', 'memory': "Zeus's own long-term memory"}


def build_prompt(items: list, life, today: date) -> str:
    lines = [f'Today is {today.strftime("%A %B %d, %Y")}.', '', f'Fixed rules of {OWNER}\'s (context only; they already settle what they cover):']
    lines += [f'- {d["says"]}' for d in active_decisions(life)]
    lines += ['', 'The items:']
    for it in items:
        text = it['text'] if len(it['text']) <= MEMORY_ITEM_CAP else it['text'][:MEMORY_ITEM_CAP] + ' [...]'
        q = f' {OWNER}\'s words: "{it["quote"][:300]}"' if it.get('quote') and it['quote'] not in text else ''
        lines.append(f'[{it["id"]}] {STORE_WORDS.get(it["store"], it["store"])}, {it["day"] or "no date"}: {text}{q}')
    return '\n'.join(lines)


def _content(text) -> set:
    return {w for w in re.findall(r"[a-z]{3,}", str(text).lower().replace("'", '').replace('’', '')) if w not in COMMON}


def check(c: dict, by_id: dict) -> tuple:
    """Code's last word on one judged group. -> (finding or None, why not)."""
    if not isinstance(c, dict):
        return None, 'not a group'
    a, b = [str(x) for x in c.get('side_a') or []], [str(x) for x in c.get('side_b') or []]
    if not a or not b or set(a) & set(b):
        return None, 'a side is empty, or an id is on both sides'
    if any(x not in by_id for x in a + b):
        return None, 'names an id that is not an item'
    try:
        conf = float(c.get('confidence'))
    except (TypeError, ValueError):
        return None, 'no confidence'
    if conf < CONFIDENCE_FLOOR:
        return None, f'confidence {conf} under the floor'
    if c.get('kind') not in ('contradiction', 'updated', 'overlap') or c.get('severity') not in ('high', 'medium', 'low'):
        return None, 'kind or severity is not one of the allowed words'
    q = ' '.join(str(c.get('question') or '').split())
    side_words = lambda ids: set().union(*(_content(by_id[x]['text'] + ' ' + str(by_id[x].get('quote') or '')) for x in ids))
    askable = (q.endswith('?') and 20 <= len(q) <= 260 and not NOT_FOR_HIS_EYES.search(q)
               and _content(q) & side_words(a) and _content(q) & side_words(b))
    changes = any(by_id[x]['kind'] in RETIRABLE for x in a + b)
    state = 'waiting' if (c['severity'] == 'high' and c['kind'] == 'contradiction' and askable and changes) else 'seen'
    why_seen = ('' if state == 'waiting' else 'not high stakes' if c['severity'] != 'high' else 'not a contradiction' if c['kind'] != 'contradiction'
                else 'the question is not plain words naming both sides' if not askable else 'answering would change nothing Zeus can change')
    return ({'key': pair_key([by_id[x]['fp'] for x in a], [by_id[x]['fp'] for x in b]), 'how': 'judge', 'side_a': a, 'side_b': b,
             'subject': ' '.join(str(c.get('subject') or '').split())[:80], 'kind': c['kind'], 'severity': c['severity'],
             'why': ' '.join(str(c.get('why') or '').split())[:300], 'question': q, 'confidence': conf, 'state': state}
            | ({'not_asked': why_seen} if why_seen else {})), ''


def _real_complete():
    import together as T
    return T._real_complete()


def judge(items: list, life, today: date, complete) -> dict:
    """-> {'findings': [...], 'dropped': [...], 'called': bool, 'error': str|None}."""
    by_id = {it['id']: it for it in items}
    if len(items) < 2:
        return {'findings': [], 'dropped': [], 'called': False, 'error': None}
    try:
        r = complete(SYSTEM, build_prompt(items, life, today), SCHEMA, timeout=TIMEOUT)
    except Exception as e:
        return {'findings': [], 'dropped': [], 'called': True, 'error': f'{type(e).__name__}: {str(e)[:120]}'}
    if r is None or not isinstance(r.get('out'), dict) or not isinstance(r['out'].get('conflicts'), list):
        return {'findings': [], 'dropped': [], 'called': True, 'error': 'no usable answer from the judge'}
    found, dropped = [], []
    for c in r['out']['conflicts']:
        f, why = check(c, by_id)
        if f:
            found.append(f)
        else:
            dropped.append({'why': why, 'group': c})
    return {'findings': found, 'dropped': dropped, 'called': True, 'error': None, 'cost': r.get('cost')}


# ---- setting a side aside ----------------------------------------------------------------------------------------------

def _archive_pref_rule(life, match, let_go: dict) -> bool:
    """Move one rule of preferences.json to `rules_archive` (the ladder's archive, same shape) in one swap of the file.
    match(rule) picks it. Unreadable file: left alone (it holds what the owner told Zeus to stop raising)."""
    import ladder as LD
    p = Path(life) / 'preferences.json'
    try:
        d = json.loads(p.read_text())
    except OSError:
        return False
    except ValueError:
        raise RuntimeError('preferences file unreadable; left alone')
    rules = [r for r in (d.get('rules') or []) if isinstance(r, dict)]
    going = [r for r in rules if match(r)]
    if not going:
        return False
    archive = [r for r in (d.get('rules_archive') or []) if isinstance(r, dict)] if isinstance(d.get('rules_archive'), list) else []
    d['rules'] = [r for r in rules if r not in going]
    d['rules_archive'] = (archive + [{**r, 'id': LD.rule_id(r), 'let_go': let_go} for r in going])[-LD.ARCHIVE_KEEP:]
    _write(p, d)
    return True


def set_aside(life, it: dict, key: str, why: str, winner: list, his=None, now=None) -> str:
    """Set one item aside, never deleting it: the reason, the owner's words (if they answered) and a pointer to what won are kept
    with it. -> what happened, in a word or two."""
    import together as T
    now_dt = _now_dt(now)
    pointer = {'conflict': key, 'kept': [w['label'] for w in winner], 'his_words': his, 'at': now_dt.isoformat(timespec='seconds')}
    let_go = {'reason': 'settled', 'by': key, 'why': why, 'kept': pointer['kept'], 'his_words': his, 'at': pointer['at']}
    k, ref = it['kind'], it.get('ref') or {}
    if k in ('boundary', 'do_not_raise'):
        return 'set aside' if T.retire(life, ref['kind'], ref['text'], ref.get('message_id'), why, pointer, now_dt) else 'already set aside'
    if k == 'rule':
        import ladder as LD
        return 'set aside' if _archive_pref_rule(life, lambda r: LD.rule_id(r) == ref.get('rule_id'), let_go) else 'not found'
    if k == 'plan':
        import reflect as R
        R.Rules(life).retire(ref.get('key'), f'{why} ({key})')
        if ref.get('pref_text'):
            _archive_pref_rule(life, lambda r: r.get('text') == ref['pref_text'], let_go)
        return 'set aside'
    if k == 'grant':
        g = _read(Path(life) / 'grants.json', None)
        if not isinstance(g, dict) or ref.get('family') not in g:
            return 'not found'
        arch = _read(Path(life) / GRANTS_ARCHIVE, {})
        arch = arch if isinstance(arch, dict) else {}
        arch.setdefault('grants', []).append({'family': ref['family'], **g[ref['family']], 'let_go': let_go})
        _write(Path(life) / GRANTS_ARCHIVE, arch)     # the archive first: a crash in between leaves both, never neither
        del g[ref['family']]
        _write(Path(life) / 'grants.json', g)
        return 'set aside'
    return 'recorded only'                            # a note, a memory line, the refill dates, the lock: not changed here


def settle(life, f: dict, by_id: dict, winner_ids: list, loser_ids: list, why: str, his=None, now=None) -> dict:
    winner = [by_id[x] if x in by_id else {'label': next((d['says'] for d in DECISIONS if d['id'] == x), x)} for x in winner_ids]
    done = {}
    for x in loser_ids:
        if x in by_id:
            done[x] = set_aside(life, by_id[x], f['key'], why, winner, his, now)
    f['outcome'] = {'kept': [w['label'] for w in winner], 'set_aside': {by_id[x]['label']: v for x, v in done.items()},
                    'why': why, 'his_words': his, 'at': _now_dt(now).isoformat(timespec='seconds')}
    _log(life, 'settled', f['key'], kept=winner_ids, set_aside=done, why=why, his_words=his)
    return done


# ---- the one question ---------------------------------------------------------------------------------------------------

def is_conflict(p: dict) -> bool:
    return isinstance(p.get('conflict'), dict) and bool(p['conflict'].get('key'))


def _rows(con) -> list:
    return [(r, json.loads(r['payload'])) for r in con.execute("SELECT key,state,payload FROM opps WHERE key LIKE 'conflict:%' ORDER BY first_seen")]


def holds_slot(con, day: str) -> bool:
    """For offers.tend: a question here is open, or one goes out on this day. The two share one slot a day."""
    return any(r['state'] in OPEN and not p.get('closed') or (p.get('conflict') or {}).get('day') == day for r, p in _rows(con))


def _slot_free(con, day: str, state: dict, now_dt: datetime) -> tuple:
    import reflect as O   # the offers live in reflect.py
    if any(r['state'] in OPEN for r, _ in O.offer_rows(con)) or any((p.get('offer') or {}).get('day') == day for _, p in O.offer_rows(con)):
        return False, 'an offer has the slot'
    if any(r['state'] in OPEN and not p.get('closed') for r, p in _rows(con)):
        return False, 'a question is still open'
    last = max((float(f.get('asked_at') or 0) for f in state['findings'].values()), default=0)
    if last and now_dt.timestamp() - last < ASK_EVERY_DAYS * 86400:
        return False, 'one question a week'
    return True, ''


def packet_for(f: dict) -> dict:
    a = '; '.join(x['label'] for x in f['items_a'])
    b = '; '.join(x['label'] for x in f['items_b'])
    q = f['question'][:1].lower() + f['question'][1:]     # lower case first, like travel.ask_text: lastsaid matches capitals
    return {'status': 'needs_decision', 'headline': f'Two things {OWNER} asked for pull against each other: {f["subject"]}.',
            'details': (f'One: {a}. The other: {b}. They cannot both be followed. On a yes, Zeus keeps the second and sets the first aside; '
                        'on a no, it keeps the first and sets the second aside. Nothing said is deleted, and this pair is not asked about again.'),
            'decision': {'ask': q + ' It is a plain yes or no.',
                         'ready': f'On a yes, Zeus keeps {b} and sets aside {a}. On a no, Zeus keeps {a} and sets aside {b}.',
                         'default': '', 'by': 'zeus'},
            'followups': [], 'learned': []}


def make_ask(con, life, f: dict, now_dt: datetime, state: dict) -> str | None:
    import policy as P
    key = 'conflict:' + f['key']
    if con.execute('SELECT 1 FROM opps WHERE key=?', (key,)).fetchone():
        return None
    step = f'Ask {OWNER} one yes-or-no question: which of two things they asked for should stand.'
    opp = {'title': ('Which one stands: ' + f['subject'])[:120], 'why': f'Two things {OWNER} asked for pull against each other.',
           'evidence': f['why'], 'urgency': 'later', 'dedupe_key': key, 'confidence': 1.0, 'owner_involved': 'yes',
           'action_class': 'notify_owner_now', 'proposed_action': step, 'steps': [{'do': step, 'action_class': 'notify_owner_now'}]}
    decision = P.decide(opp, None, now_dt)          # routine item: the 9:00am note, never in quiet hours
    if decision.get('autonomy') == 'skip':
        raise RuntimeError('policy would not raise the question item')
    day = (datetime.fromisoformat(decision['surface_at']).astimezone(PACIFIC).date() if decision.get('surface_at') else now_dt.date()).isoformat()
    free, why = _slot_free(con, day, state, now_dt)
    if not free:
        f['waiting_why'] = why
        return None
    f.update(state='asked', asked_at=now_dt.timestamp(), item=key)    # marked first: a crash means never asked, not asked twice
    _write(Path(life) / STATE, state)
    now = time.time()
    payload = {**opp, 'decision': decision, '_source': 'conflicts', 'packet': packet_for(f), 'ready_at': now,
               'conflict': {'key': f['key'], 'day': day, 'question': f['question'], 'side_a': f['items_a'], 'side_b': f['items_b'],
                            'subject': f['subject']}}
    con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key, 'ready', decision['autonomy'], decision['surface_at'], 'later', 'notify_owner_now', json.dumps(payload, default=str), '[]', now, now))
    con.commit()
    _log(life, 'asked', f['key'], item=key, day=day)
    return key


# ---- the nightly pass ----------------------------------------------------------------------------------------------------

def _due(life, now_dt: datetime, state: dict):
    """-> the day to do, or None. After the nightly page for that day (together.py), or LATE_HOURS after 8:30pm anyway."""
    import together as T
    day = T._closed_day(now_dt)
    if state.get('last_day') and state['last_day'] >= day.isoformat():
        return None
    page = _read(Path(life) / T.STATE, {})
    page_day = page.get('last_day') if isinstance(page, dict) else None
    closed_at = datetime(day.year, day.month, day.day, *T.REVIEW_AT, tzinfo=PACIFIC)
    if (page_day and page_day >= day.isoformat()) or now_dt >= closed_at + timedelta(hours=LATE_HOURS):
        return day
    return None


def _close_item(con, key, state_word, why):
    r = con.execute('SELECT payload FROM opps WHERE key=?', (key,)).fetchone()
    p = json.loads(r[0])
    p['closed'] = {'state': state_word, 'at': time.time(), 'by': 'conflicts', 'why': why}
    con.execute('UPDATE opps SET state=?, payload=?, updated=? WHERE key=?', (state_word, json.dumps(p, default=str), time.time(), key))
    con.commit()


def _tidy_asked(con, life, state: dict, now_dt: datetime, report: dict):
    """An asked question closed any way but a yes or no: dropped = both stay, never asked again; told and not answered
    in ANSWER_DAYS = closed, never asked again."""
    for r, p in _rows(con):
        f = state['findings'].get((p.get('conflict') or {}).get('key'))
        if not f or f.get('state') != 'asked':
            continue
        if r['state'] == 'surfaced' and not p.get('closed') and now_dt.timestamp() - float((p.get('told') or {}).get('at') or now_dt.timestamp()) > ANSWER_DAYS * 86400:
            _close_item(con, r['key'], 'moot', f'no answer in {ANSWER_DAYS} days')
            f.update(state='unanswered', closed_at=now_dt.timestamp())
            _log(life, 'unanswered', f['key'])
            report.setdefault('closed', []).append(r['key'])
        elif r['state'] not in OPEN and f.get('state') == 'asked':
            f.update(state='dropped' if r['state'] == 'declined' else 'unanswered', closed_at=now_dt.timestamp())
            _log(life, f['state'], f['key'], item_state=r['state'])


def run(con, life, now=None, complete=None, memory=None, dry: bool = False, force: bool = False) -> dict:
    """Call from every tick (after together.run). Works at most once a night. Never sends.
    -> {'status': 'off' | 'early' | 'done', ...counts}. dry: work everything out, change nothing, return what it would do."""
    life = Path(life)
    if not on(life):
        return {'status': 'off'}
    now_dt = _now_dt(now)
    state = _state(life)
    day = now_dt.date() if force else _due(life, now_dt, state)
    if day is None:
        return {'status': 'early'}
    report = {'status': 'done', 'day': day.isoformat()}
    if not dry:
        _tidy_asked(con, life, state, now_dt, report)
    items = collect(life, now_dt, memory)
    by_id = {it['id']: it for it in items}
    report['items'] = len(items)
    new = []
    for f in code_findings(items, life):
        if f['key'] in state['findings']:
            continue
        f['items_a'] = [{'id': x, 'label': by_id[x]['label'], 'fp': by_id[x]['fp'], 'text': by_id[x]['text'][:300]} for x in f['side_a']]
        if f['state'] == 'settled' and not dry:
            settle(life, f, by_id, f['side_b'], f['side_a'] if f['action'] == 'set aside' else [], f['why'], None, now_dt)
        new.append(f)
    settled = {x['fp'] for f in list(state['findings'].values()) + new if f.get('how') == 'code' and f.get('state') == 'settled' and f.get('action') == 'set aside'
               for x in f.get('items_a') or []}
    prose = [it for it in items if it['fp'] not in settled]     # a line a later rule already decided is not judged again
    digest = hashlib.sha1((PROMPT_VERSION + json.dumps([[it['fp'], it['day']] for it in prose])).encode()).hexdigest()
    if digest != state.get('judged') or force:
        got = judge(prose, life, day, complete or _real_complete())
        report.update(judge_called=got['called'], judge_dropped=len(got['dropped']))
        if got['error']:
            report['judge_error'] = got['error']
        else:
            state['judged'] = digest
            for f in got['findings']:
                if f['key'] in state['findings'] or any(f['key'] == n['key'] for n in new):
                    continue
                f['items_a'] = [{'id': x, 'label': by_id[x]['label'], 'fp': by_id[x]['fp'], 'kind': by_id[x]['kind'], 'text': by_id[x]['text'][:300]} for x in f['side_a']]
                f['items_b'] = [{'id': x, 'label': by_id[x]['label'], 'fp': by_id[x]['fp'], 'kind': by_id[x]['kind'], 'text': by_id[x]['text'][:300]} for x in f['side_b']]
                new.append(f)
        if got.get('dropped'):
            report['judge_dropped_why'] = [d['why'] for d in got['dropped']][:5]
    else:
        report['judge_called'] = False
    for f in new:
        f['found_at'] = now_dt.timestamp()
        f['items'] = {x: by_id[x]['fp'] for x in f['side_a'] + f['side_b'] if x in by_id}
        state['findings'][f['key']] = {k: v for k, v in f.items() if k != 'items'}
    report['settled'] = [f['key'] for f in new if f['state'] == 'settled']
    report['seen'] = [f['key'] for f in new if f['state'] == 'seen']
    waiting = sorted((f for f in state['findings'].values() if f.get('state') == 'waiting'), key=lambda f: -float(f.get('found_at') or 0))
    report['waiting'] = [f['key'] for f in waiting]
    if dry:
        report['findings'] = new
        report['would_ask'] = waiting[0]['question'] if waiting else None
        return report
    if waiting:
        asked = make_ask(con, life, waiting[0], now_dt, state)
        if asked:
            report['asked'] = asked
        else:
            report['ask_waits'] = waiting[0].get('waiting_why')
    state['last_day'] = day.isoformat()
    state['ran_at'] = now_dt.timestamp()
    _write(life / STATE, state)
    return report


# ---- the owner's answer -------------------------------------------------------------------------------------------------

def _chat_store(life):
    store = os.environ.get('LIFE_CHAT_DB') or None
    if store is None and Path(life).resolve() != LIVE_LIFE.resolve():
        return None                                   # a sandbox never reads the owner's real chat
    import chats as C
    return store or C.STORE


def show_extra(p: dict, key: str, state: str) -> dict:
    if state not in ('surfaced', 'ready') or p.get('closed'):
        return {}
    return {'when_he_says_yes': f'Run: life approve {key} "<their exact words>". Code checks the words; a yes keeps the second thing and sets the '
                                'first aside, a no keeps the first. A clear yes or no in any words counts; an "ok" or a thank-you alone does not.',
            'when_he_says_no': f'Also: life approve {key} "<their exact words>" (code reads a no too). If they say both should stay, or not to ask: '
                               f'life drop {key} "<their words>".'}


def _vocab(p: dict) -> set:
    import hisreply as H
    c = p['conflict']
    return H.vocabulary(p.get('title'), c.get('question'), c.get('subject'), [x['label'] for x in c['side_a'] + c['side_b']],
                        ['keep', 'one', 'first', 'second', 'that', 'plan', 'rule', 'stand', 'stands', 'way'])


def _seen_text(con, p: dict) -> str:
    told = p.get('told') or {}
    if told.get('note'):
        row = con.execute('SELECT text FROM outbox WHERE key=?', (told['note'],)).fetchone()
        if row and row[0]:
            return str(row[0])
    return str(told.get('said') or p['conflict'].get('question') or '')


def _marks(question: str) -> set:
    """Words the text the owner answered must share with the question: its names, or failing that its long words."""
    names = {w.lower() for w in re.findall(r"\b[A-Z][a-z]{2,}\b", question[1:])} - COMMON - {'your'}
    return names or {w for w in _content(question) if len(w) >= 6}


def approve(con, r, p: dict, words: str, life, close, read_chat=None, now: float | None = None) -> int:
    """`life approve <conflict item> "<the owner's words>"` (lifecli). A yes keeps side B, a no keeps side A."""
    import chats as C
    import hisreply as H
    import lastsaid as LS
    life = Path(life)
    key, c = r['key'], p['conflict']
    now = time.time() if now is None else float(now)
    if not on(life):
        print('Settling clashes is switched off, so nothing was changed.')
        return 2
    if p.get('closed') or r['state'] not in ('surfaced', 'ready'):
        print(f'This question is not waiting on {OWNER} any more, so nothing was changed.')
        return 3
    if r['state'] == 'ready' or not (p.get('told') or {}).get('at'):
        print(f'This question has not been put to {OWNER} yet, so their words cannot be an answer to it. Nothing was changed.')
        return 3
    if len(str(words or '').strip()) < 1:
        print(f'Quote what {OWNER} said: life approve {key} "<their exact words>". Nothing was changed.')
        return 2
    told_at = float(p['told']['at'])
    store = _chat_store(life)
    try:
        if read_chat is None:
            if store is None:
                raise RuntimeError('a sandbox has no chat store of its own')
            read_chat = lambda since: C.messages(since, path=store)
        msgs = [m for m in read_chat(told_at - 1) if m['who'] == 'owner' and float(m['at']) <= now + 1]
    except Exception as e:
        print(f'Could not check the words against {OWNER}\'s real messages ({type(e).__name__}), so nothing was changed.')
        return 1
    msg = C.quoted_in(words, msgs) or (msgs[-1] if msgs else None)
    if msg is None:
        print(f'{OWNER} has sent no message since being asked, so nothing was changed. If they did answer, ask them once more.')
        return 3
    for m in msgs:
        if (m['at'], m['id']) > (msg['at'], msg['id']):
            later = H.read(m['text'])
            if later['answer'] in ('no', 'changed') or later['why'] == 'he has not decided yet':
                print(f'After that {OWNER} wrote: "{m["text"][:200]}". So that answer does not stand, and nothing was changed. Go by their newest message.')
                return 9
    others = {k: v for k, v in LS.open_asks(con).items() if k != key}
    reading = H.bind(H.read(msg['text']), _vocab(p), [H.vocabulary(*v) for v in others.values()])
    try:
        last = LS.last(con, float(msg['at']), store=store) if store else None
    except Exception:
        last = None
    fresh_this = bool(last and last['asks'] == [key] and float(msg['at']) - last['at'] <= JUST_ASKED)
    answer = reading['answer']
    if answer == 'other' and H.agrees(msg['text']):
        between = [m for m in msgs if last and last['at'] <= m['at'] and (m['at'], m['id']) < (msg['at'], msg['id'])]
        answer = 'yes' if fresh_this and last['question'] and not between else 'other'
    elif answer in ('yes', 'no') and others and not reading.get('points') and not fresh_this:
        print(f'{OWNER} has more than one thing waiting on their word and this answer does not say which. Ask them once, in a few words, whether they mean '
              f'{c["subject"]}. Nothing was changed.')
        return 2
    if answer == 'changed':
        print(f'The answer has a change in it ({reading.get("why")}), so neither side was set aside. Ask {OWNER} in one short line what they want instead, '
              'or note what they said. Nothing was changed.')
        return 2
    if answer not in ('yes', 'no'):
        print('That is not a yes or a no to this question, so nothing was changed. Leave it open.')
        return 2
    seen = _seen_text(con, p)
    marks = _marks(c['question'])
    if marks and not any(m in seen.lower() for m in marks):
        print(f'The message {OWNER} answered did not name what the question is about, so nothing was changed. Ask them once more, naming both things.')
        return 3
    state = _state(life)
    f = state['findings'].get(c['key'])
    if not f:
        print('The record of this question is missing, so nothing was changed. Tell the builder.')
        return 1
    items = {x['id']: {**x, 'ref': None} for x in c['side_a'] + c['side_b']}
    current = {it['fp']: it for it in collect(life, now)}
    by_id = {}
    for x, it in items.items():
        cur = current.get(it['fp'])
        by_id[x] = cur | {'id': x} if cur else {**it, 'kind': 'gone', 'label': it['label']}
    a_ids, b_ids = [x['id'] for x in c['side_a']], [x['id'] for x in c['side_b']]
    win, lose = (b_ids, a_ids) if answer == 'yes' else (a_ids, b_ids)
    why = f'{OWNER} chose this on {_words_day(datetime.fromtimestamp(float(msg["at"]), PACIFIC).date())}'
    done = settle(life, f, by_id, win, lose, why, msg['text'][:300], now)
    f.update(state='decided', answer=answer, answered_at=float(msg['at']), message_id=msg.get('id'))
    _write(life / STATE, state)
    close(con, r, 'done', 'zeus chat, the owner\'s answer to a clash', f'{OWNER} said {answer}', {'conflict': {'answer': answer, 'set_aside': done}})
    kept = '; '.join(by_id[x]['label'] for x in win)
    print(f'Done. Zeus keeps {kept}. The other side is set aside, not deleted. Tell {OWNER} that in one short line.')
    return 0


# ---- what the pipes are told, and a look for the chat ---------------------------------------------------------------------

def prompt_text(life) -> str:
    """Switch conflicts-on: one plain line per rule of the owner's that won over an older line Zeus could not change
    itself (a note, its own memory). '' when there is nothing, or any trouble."""
    if not in_prompts(life):
        return ''
    try:
        state = _state(life)
    except Exception:
        return ''
    lines = []
    for f in state['findings'].values():
        if f.get('state') == 'settled' and f.get('decision') and f.get('action') == 'override':
            says = next((d['says'] for d in DECISIONS if d['id'] == f['decision']), None)
            if says and says not in lines:
                lines.append(says)
        elif f.get('state') == 'decided' and (f.get('outcome') or {}).get('kept'):
            line = f'{OWNER} chose {"; ".join(f["outcome"]["kept"])} ({_words_day(_day(f.get("answered_at")))}).'
            if line not in lines:
                lines.append(line)
    if not lines:
        return ''
    out = 'Settled, where an older note of Zeus says otherwise:'
    for ln in lines:
        if len(out) + len(ln) + 3 > PROMPT_CAP:
            break
        out += '\n- ' + ln
    return out


def cli(a: list, life) -> int:
    """life conflicts: what was found, what was settled and why, and the question if one is open."""
    state = _state(life)
    rows = sorted(state['findings'].values(), key=lambda f: -float(f.get('found_at') or 0))
    if not rows:
        print('No clashes found between the things Zeus keeps.')
        return 0
    for f in rows[:20]:
        a_ = '; '.join(x['label'] for x in f.get('items_a') or [])
        b_ = '; '.join(x['label'] for x in f.get('items_b') or []) or next((d['says'] for d in DECISIONS if d['id'] == f.get('decision')), '')
        print(f"- [{f['state']}] {f.get('subject')}: {a_} vs {b_}. {f.get('why')}"
              + (f" Kept: {'; '.join(f['outcome']['kept'])}." if f.get('outcome') else ''))
    return 0

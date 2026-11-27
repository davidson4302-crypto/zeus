"""people: one small page per person in the owner's life.

A page per person (who they are, what was said, what is open, how close) is read before Zeus answers about them. The
noticer has a `known_people` slot that this module fills, so when an email about someone the owner has said is not
their concern comes in, Zeus knows that before raising it.

  <life_home>/people/index.json   slug -> person (the one source of truth)
  <life_home>/people/<slug>.md    the same person as a page a human can read (always rebuilt from the index)
  <life_home>/people/state.json   which learned rows are already folded in, and names that could not be filed

Where the facts come from: only `learned.jsonl`, the rows the quiet-moment pass wrote from the owner's own messages. Each
row already carries the owner's exact words. A model is asked three things about each new statement (who the person is to
the owner, how close, and whether Zeus should bring the owner things about that person), and code keeps an answer only
when the words the model points at are really in what the owner said. A wrong page is worse than a thin one.

Two spellings of one person are joined only when someone says so (add_alias, merge). A first name shared by two
people is never guessed.

pages.py adds what the owner's mail and contact sources say (people/sources.json, companies/) without ever touching
this index. With the switch file pages-on, the pages here are written in pages.py's summary-and-timeline layout
(_page_text); without it they are the plain render(). The noticer's notes (known_people) never change.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chats as C  # noqa: E402
import settings  # noqa: E402

OWNER = settings.OWNER_FIRST     # how pages and prompts name the owner

ENGINE = os.environ.get('LIFE_ENGINE_DIR', str(settings.ENGINE_DIR))
DEFAULT_CLOSENESS = 2           # named once, little known: below anyone the owner has described, above nobody
THREAD_GRACE = 2 * 86400        # a dated thread with nothing still owed is over this long after its date
THREAD_UNDATED = 14 * 86400     # a thread the owner gave no date is over this long after the owner said it (tracking_context.INTEREST_DAYS)
LEDGER_OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')   # the ledger's own open states (tracking_context.OPEN)
TITLES = {'dr', 'doctor', 'mr', 'mrs', 'ms', 'miss', 'prof', 'professor', 'coach', 'pastor', 'officer'}
# The owner (every configured spelling of their name) and Zeus itself are never filed as people.
OWNER_NAMES = ({str(settings.OWNER_NAME).lower(), str(settings.OWNER_FIRST).lower()} - {'the owner', ''}) | \
              {str(n).lower() for n in (settings.get('owner.nicknames', []) or []) if str(n).strip()}
NOT_PEOPLE = {'me', 'i', 'myself', 'you', 'zeus', str(settings.AGENT_NAME).lower(), 'us', 'we', 'the owner'} | OWNER_NAMES
# Names that are also everyday words. "I will call" must not be read as the owner's friend Will, so these need a capital.
EVERYDAY = {'will', 'mark', 'bill', 'grace', 'may', 'april', 'june', 'august', 'rose', 'art', 'pat', 'don', 'sue', 'rich',
            'frank', 'jack', 'hope', 'faith', 'joy', 'dawn', 'penny', 'chase', 'drew', 'guy', 'rob', 'nick', 'ray', 'max',
            'miles', 'skip', 'gene', 'dean', 'carol', 'cliff', 'lane', 'wade', 'wren', 'bob', 'sandy', 'summer', 'ivy',
            'red', 'ty', 'al', 'ed', 'jo', 'christian', 'angel', 'star', 'sky', 'page', 'price', 'young', 'long', 'cash'}
# Short and long ways the owner might say the same relation. Only used to check a relation against the owner's own words.
SAME_WORD = [{'dr', 'doc', 'doctor'}, {'mom', 'mum', 'mother', 'mama'}, {'father', 'pops', 'papa'}, {'bro', 'brother'},
             {'sis', 'sister'}, {'wife', 'wifey'}, {'husband', 'hubby'}, {'grandma', 'grandmother', 'nana'},
             {'grandpa', 'grandfather'}, {'kid', 'child', 'son', 'daughter'}, {'rep', 'representative'},
             {'accountant', 'cpa'}, {'lawyer', 'attorney'}, {'boss', 'manager'}]
FILLER = {'his', 'her', 'their', 'my', 'the', 'a', 'an', 'of', 'at', 'in', 'for', 'and', 'to', 'with',
          'from', 'on', 'is', 'who', 'that', 'one', 's', 'own', 'our', 'owner', 'owners'} | \
         {w for n in OWNER_NAMES for w in (n, n + 's')}
SAYS_NO = re.compile(r"n't\b|\b(not|no|never|stop|ignore|disregard|drop|quit|none)\b", re.I)
HOW_CLOSE = {5: 'very close', 4: 'close', 3: 'someone they work with or see often', 2: 'someone they deal with now and then',
             1: 'not part of their world'}

SCHEMA = {'type': 'object', 'required': ['people'], 'properties': {'people': {'type': 'array', 'items': {
    'type': 'object', 'required': ['row', 'name', 'relation', 'relation_quote', 'closeness', 'closeness_quote', 'his', 'his_quote'],
    'properties': {
        'row': {'type': 'string', 'description': 'the row id of the statement, copied exactly'},
        'name': {'type': 'string', 'description': 'the person, spelled as in the statement\'s people list'},
        'relation': {'type': ['string', 'null'], 'description': f'who the person is to {OWNER}, in their own words; null if the quote does not say'},
        'relation_quote': {'type': ['string', 'null'], 'description': 'the exact piece of the quote that says so'},
        'closeness': {'type': ['integer', 'null'], 'description': '1 to 5, only if the quote shows it; else null'},
        'closeness_quote': {'type': ['string', 'null']},
        'his': {'enum': ['yes', 'no', 'unknown'], 'description': f'should Zeus bring {OWNER} things about this person?'},
        'his_quote': {'type': ['string', 'null']},
    }}}}}

SYSTEM = """You keep a small page about each person in the owner's life for the owner's assistant Zeus. The owner is
called {owner}. You are given new statements {owner} made. `quote` is {owner}'s exact words; `people` is who the statement
names. For each statement and each person it names, return one entry saying only what THAT quote supports:

- relation: who the person is to {owner}, using {owner}'s own words from the quote ("sister", "cardiologist at Lakeside
  Heart Clinic", "sales rep"). null when the quote does not say. Never guess from the name, the topic, the `text`
  summary or anything you know from elsewhere.
- closeness: 5 immediate family or partner; 4 close friend or daily business partner; 3 someone {owner} works with or
  sees often; 2 someone {owner} deals with now and then (a doctor, a vendor, a contact); 1 someone {owner} says is not
  part of their world. null unless the quote itself shows it.
- his (is this person {owner}'s concern): should Zeus bring {owner} things about this person? "no" ONLY when {owner}
  says the person themself is not their concern ("X isn't for me", "X is not mine to deal with", "stop telling me about
  X"). "yes" only when {owner} says they want to hear about the person or plainly calls them their own (their family,
  their doctor, their customer). Otherwise "unknown". Careful: "I already spoke to X", "I've got it", "disregard that",
  "ignore for now" about ONE item is not a "no" about the person. That is "unknown".

For each of the three give the shortest exact piece of the quote that supports it (relation_quote, closeness_quote,
his_quote), copied character for character from `quote`. When a statement names more than one person, the supporting
piece must name that person and nobody else. No supporting words means null (or "unknown"). Code throws away anything
whose supporting words are not in the quote, so never stretch. A missed detail is fine; a wrong one is not.""".replace(
    'The owner is\ncalled the owner. ', '').replace('{owner}', OWNER)


class Unreadable(Exception):
    """The index file is there but cannot be read. Never treated as "nobody known" when writing: that would wipe it."""


# ---------- small helpers ----------

def _home(life_home) -> Path:
    """The state folder. No silent default: a sandbox module must never land in the live folder by accident."""
    life_home = life_home or os.environ.get('LIFE_HOME')
    if not life_home:
        raise ValueError('people: no life_home given and LIFE_HOME is not set')
    return Path(life_home)


def _dir(life_home) -> Path:
    return _home(life_home) / 'people'


def _flat(s) -> str:
    """Same forgiveness as chats.quoted_in: curly quotes, extra spaces and capitals do not matter."""
    s = str(s).replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    return re.sub(r'\s+', ' ', s).strip().lower()


def _core(name) -> str:
    """A name as it is compared: lower case, no dots, no leading title. "Dr. Nazal", "dr nazal" and "Nazal" are one."""
    words = [w for w in re.sub(r'[.,]', ' ', _flat(name)).split(' ') if w]
    while len(words) > 1 and words[0] in TITLES:
        words.pop(0)
    return ' '.join(words)


def _names(person: dict) -> list:
    return [person.get('name') or ''] + [a for a in (person.get('aliases') or []) if a]


def _first(person: dict) -> str | None:
    """The first name of someone known by two or more names ("Taylor Smith" -> "taylor"); None for a one-word name."""
    words = _core(person.get('name') or '').split(' ')
    return words[0] if len(words) > 1 else None


def _slug(name: str, taken=()) -> str:
    base = unicodedata.normalize('NFKD', str(name)).encode('ascii', 'ignore').decode().lower()
    base = re.sub(r'[^a-z0-9]+', '-', base).strip('-') or 'p-' + hashlib.sha1(str(name).encode()).hexdigest()[:8]
    slug, n = base, 2
    while slug in taken:
        slug, n = f'{base}-{n}', n + 1
    return slug


def _show(name: str) -> str:
    """How the name is written on the page: as the owner typed it, but "jordan" becomes "Jordan"."""
    name = re.sub(r'\s+', ' ', str(name)).strip()
    return ' '.join(w.capitalize() for w in name.split(' ')) if name == name.lower() else name


def _day(at, year=False) -> str:
    try:
        t = time.localtime(float(at))
    except (TypeError, ValueError):
        return 'an unknown day'
    return time.strftime('%b ', t) + str(t.tm_mday) + (time.strftime(', %Y', t) if year else '')


def _plain(s) -> str:
    """Text for a page a person reads: one line, no long dashes."""
    return re.sub(r'\s+', ' ', str(s).replace('—', ' - ').replace('–', '-')).strip()


def _sentence(s) -> str:
    s = _plain(s)
    return s if not s or s[-1] in '.!?' else s + '.'


def _write(path: Path, text: str):
    """Whole file or nothing: a crash mid-write must not leave half a page or half an index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)


def _write_quiet(path: Path, text: str):
    """Write a page that already exists without moving its change time. The hourly research wakes the model
    for every people page whose file changed since its last run (research.World.people). A line added from mail it has
    already been shown as new mail, or a thread closed by code, is not a second piece of news, so it must not cost a
    model call in an otherwise quiet hour. A page that is new keeps its real time (someone new is news once)."""
    was = path.stat().st_mtime if path.exists() else None
    _write(path, text)
    if was is not None:
        os.utime(path, (was, was))


def text_of(thing) -> str:
    """Every piece of text inside an observation (subject, sender, body, attendees), as one string to look for names in."""
    if thing is None:
        return ''
    if isinstance(thing, str):
        return thing
    if isinstance(thing, dict):
        return '\n'.join(t for t in (text_of(v) for v in thing.values()) if t)
    if isinstance(thing, (list, tuple, set)):
        return '\n'.join(t for t in (text_of(v) for v in thing) if t)
    return ''


# ---------- reading and writing the pages ----------

def load(life_home) -> dict:
    """slug -> person. No file yet means nobody yet. A damaged file raises Unreadable."""
    p = _dir(life_home) / 'index.json'
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise Unreadable(str(e)[:120])
    if not isinstance(d, dict) or not all(isinstance(v, dict) and v.get('name') for v in d.values()):
        raise Unreadable('index is not a list of people')
    return d


def _load_state(life_home) -> dict:
    try:
        d = json.loads((_dir(life_home) / 'state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault('done', {})       # learned row -> when it was folded in. Each row counts once.
    d.setdefault('unfiled', [])    # statements naming someone we could not tell apart from a namesake
    return d


def _save(life_home, index: dict, state: dict):
    """Index first, pages next, bookmark last: if this stops halfway the rows are simply looked at again, and a
    statement already on a page is never added twice."""
    d = _dir(life_home)
    _write(d / 'index.json', json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True))
    for slug, person in index.items():
        page_text = _page_text(life_home, slug, person)
        f = d / f'{slug}.md'
        if not f.exists() or f.read_text(encoding='utf-8') != page_text:
            _write(f, page_text)
    _write(d / 'state.json', json.dumps(state, indent=2, ensure_ascii=False))


def _page_text(life_home, slug, person) -> str:
    """With the switch file pages-on, the page gets pages.py's layout (a short summary on top, then <!-- timeline -->
    and dated, sourced lines, the owner's own words beside what email and contact sources say). Without it, or on any
    trouble, the page is what render() writes."""
    try:
        if (_home(life_home) / 'pages-on').exists():
            import pages
            return pages.person_page(life_home, slug, person)
    except Exception:
        pass
    return render(person)


def _due(t: dict):
    try:
        return datetime.fromisoformat(str(t.get('due')).replace('Z', '+00:00')).timestamp() if t.get('due') else None
    except ValueError:
        return None


def _thread_over(t: dict, now: float) -> str | None:
    """Why a thread is over by the calendar alone, or None. A dated one: its date passed two days ago. One with no date
    gets one expiry: 14 days after the owner said it (so a `due: null` thread never stays open for good).
    A thread with neither a date nor a time the owner said it is never closed by the clock."""
    due = _due(t)
    if due is not None:
        return 'its date passed with nothing still owed' if now - due > THREAD_GRACE else None
    said = t.get('at')
    return 'no date was given and it is two weeks old' if said and now - float(said) > THREAD_UNDATED else None


# Agents get pages too, noted as agents. A page whose person carries kind 'agent' (set once, from the configured team:
# settings.TEAM members with kind = "agent") says so wherever it is shown, and is never handed on as someone to draft
# to, wait on a reply from, or keep up a relationship with.
AGENT_LINE = ("{name} is one of " + OWNER.replace('{', '{{').replace('}', '}}') + "'s own agents, not an outside person: "
              "nobody to draft to, wait on a reply from or keep up a relationship with.")


ROSTER = None                   # {name as compared: roster name}; read once from the configured team (settings.TEAM), or set by a test


def is_agent(person) -> bool:
    return isinstance(person, dict) and person.get('kind') == 'agent'


def roster() -> dict:
    """The owner's own agents by name and by every other name the team list gives them: the members of settings.TEAM
    (config `team.members`) with kind = "agent". A sandbox may name a pretend list with LIFE_TEAM_FILE (a JSON list of
    members, or {"members": [...]}). {} when none is configured or the list cannot be read: then nobody is marked, which
    is the safe side (a page is never called an agent on a guess)."""
    global ROSTER
    if ROSTER is None:
        try:
            rows = settings.TEAM
            named = os.environ.get('LIFE_TEAM_FILE')
            if named:
                got = json.loads(Path(named).read_text(encoding='utf-8'))
                rows = (got.get('members') or got.get('agents') or []) if isinstance(got, dict) else got
            rows = [a for a in rows or [] if isinstance(a, dict) and str(a.get('kind') or '').lower() == 'agent']
            ROSTER = {_core(n): str(a.get('name')) for a in rows if isinstance(a, dict) and a.get('name')
                      for n in [a['name']] + [x for x in a.get('aliases') or [] if isinstance(x, str)]}
        except Exception:
            ROSTER = {}
    return ROSTER


def mark_agent(person: dict, now=None) -> bool:
    """Mark a person's record as one of the owner's agents when the roster names them (by the page's name or an alias of it).
    -> True when the mark was put on now. Never removes a mark and never touches who the owner said they are."""
    hit = next((roster()[_core(n)] for n in _names(person) if _core(n) in roster()), None)
    if not hit or is_agent(person):
        return False
    person['kind'] = 'agent'
    person['agent'] = {'roster_name': hit, 'at': float(now or time.time()), 'source': 'the configured team list'}
    return True


def _in_his_words(support: dict) -> str:
    """How a page says where "who they are" comes from. When the owner's words answered a question (kept as `asked`),
    the question is shown too, because the answer alone may not say it ("alex is 8" answers "which are yours?")."""
    quote = _plain(support.get('quote', ''))
    if support.get('asked'):
        return f"Asked \"{_plain(support['asked'])}\", {OWNER} said: \"{quote}\""
    return f"{OWNER}'s words: \"{quote}\""


def not_same(a: dict | None, b_slug: str) -> bool:
    """Record `a` carries a durable mark (set when the owner says two pages are two people) that it is NOT the person
    filed as `b_slug`. Kept inside the record itself, so every later join has to pass it."""
    return isinstance(a, dict) and b_slug in (a.get('not_same') or {})


def _open_threads(person: dict, now=None) -> list:
    """The threads that are open. settle_threads() below is the one place a thread ends (it runs on every
    tick and moves a finished thread into the person's history), so every reader shows exactly what is on file as
    open. A thread is never hidden from the page while it stays open on file."""
    return [{**t, '_due': _due(t)} for t in person.get('open_threads') or []]


def ledger_rows(life_home, sql: str, args=()) -> list | None:
    """One read-only question to the ledger. [] when there is no ledger yet, None when it is there and could
    not be read (so "could not read" is never taken for "nothing there")."""
    ledger = _home(life_home) / 'ledger.sqlite3'
    if not ledger.is_file():
        return []
    import sqlite3
    try:
        con = sqlite3.connect(f'file:{ledger}?mode=ro', uri=True, timeout=2)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _bound_items(life_home) -> dict | None:
    """The owner's chat message id -> [(item key, state)] for the items the quiet pass made from that message
    (quietpass.add_life_item writes payload.from_chat.message_id; the phone page finds a promise's item the same way).
    {} when there is no ledger, None when it could not be read (then nothing is closed)."""
    rows = ledger_rows(life_home, "SELECT key, state, json_extract(payload,'$.from_chat.message_id') FROM opps "
                                  "WHERE json_extract(payload,'$.from_chat.message_id') IS NOT NULL AND key NOT LIKE '%#fu%'")
    if rows is None:
        return None
    out = {}
    for key, state, mid in rows:
        out.setdefault(str(mid), []).append((key, state))
    return out


def settle_threads(life_home, now=None, items=None, dry=False) -> list:
    """Open threads close. One code check per thread, no model, and no second judge of "did it happen": the engine already
    decides that when it closes the ledger item (commit.py, outcome.py: closed only with evidence), so that is read.
      the item made from the owner's message is closed   -> the thread closes, with that item and how it ended as the reason
      that item is still open                    -> the thread stays open, whatever the date (something is still owed)
      no item was made from it                   -> it closes by the calendar (_thread_over)
    A closed thread moves to the person's `closed_threads` with when and why; nothing is deleted. The page is written
    with _write_quiet (tidying is not news about the person). -> [{'person', 'message_id', 'why', 'ref'}]. The caller
    holds the tick's lock."""
    now = float(now or time.time())
    try:
        index = load(life_home)
    except Unreadable:
        return []
    if not any(p.get('open_threads') for p in index.values()):
        return []
    items = _bound_items(life_home) if items is None else items
    if items is None:
        return []
    closed, changed = [], set()
    for slug, person in index.items():
        keep = []
        for t in person.get('open_threads') or []:
            bound = items.get(str(t.get('message_id'))) or []
            still = [k for k, state in bound if state in LEDGER_OPEN]
            if bound and not still:
                why, ref = 'the item made from it was closed as ' + ', '.join(sorted({state for _, state in bound})), bound[0][0]
            elif still:
                why, ref = None, None
            else:
                why, ref = _thread_over(t, now), None
            if why is None:
                keep.append(t)
                continue
            person.setdefault('closed_threads', []).append({**t, 'closed': {'at': now, 'why': why, 'ref': ref}})
            closed.append({'person': slug, 'message_id': t.get('message_id'), 'why': why, 'ref': ref})
            changed.add(slug)
        person['open_threads'] = keep
    if closed and not dry:
        d = _dir(life_home)
        _write(d / 'index.json', json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True))
        for slug in changed:
            _write_quiet(d / f'{slug}.md', _page_text(life_home, slug, index[slug]))
    return closed


def render(person: dict, now=None) -> str:
    """The readable page. Plain words only: no field names, no ids, no long dashes."""
    name = person['name']
    sup = person.get('support') or {}
    lines = [f'# {name}', '']
    if person.get('aliases'):
        lines.append('Also called: ' + ', '.join(person['aliases']) + '.')
    if is_agent(person):
        lines.append(AGENT_LINE.format(name=name))
    if person.get('relation'):
        lines.append(f"Who: {_plain(person['relation'])}. {_in_his_words(sup.get('relation') or {})}")
    else:
        lines.append(f'Who: {OWNER} has not said yet.')
    if sup.get('closeness'):
        c = int(person.get('closeness') or DEFAULT_CLOSENESS)
        lines.append(f"How close: {c} of 5, {HOW_CLOSE.get(c, '')}. {OWNER}'s words: \"{_plain(sup['closeness'].get('quote', ''))}\"")
    else:
        lines.append('How close: not known yet.')
    his = person.get('his', 'unknown')
    if his in ('yes', 'no') and sup.get('his'):
        lines.append(f"Bring {OWNER} things about {name}: {his}. On {_day(sup['his'].get('at'))} {OWNER} said: \"{_plain(sup['his'].get('quote', ''))}\"")
    else:
        lines.append(f'Bring {OWNER} things about {name}: {OWNER} has not said either way.')
    lines += ['', f'## What {OWNER} has said']
    facts = sorted(person.get('facts') or [], key=lambda f: -(f.get('at') or 0))
    for f in facts[:25]:
        lines.append(f"- {_day(f.get('at'))}: {_sentence(f.get('text', ''))} {OWNER}'s words: \"{_plain(f.get('quote', ''))}\"")
    if not facts:
        lines.append('- Nothing yet.')
    lines += ['', '## Open threads']
    threads = _open_threads(person, now)
    for t in threads:
        when = f" (by {_day(t['_due'])})" if t.get('_due') else ''
        lines.append(f"- {_sentence(t.get('text', ''))}{when} {OWNER}'s words: \"{_plain(t.get('quote', ''))}\"")
    if not threads:
        lines.append('- Nothing open.')
    if person.get('last_seen'):
        lines += ['', f"Last mentioned by {OWNER}: {_day(person['last_seen'], year=True)}."]
    return '\n'.join(lines) + '\n'


# ---------- who is meant ----------

def _find(index: dict, name: str):
    """Which known person a name means.
    -> (slug, [])      exactly one person carries this name or alias, or it is a first name only one person has
       (None, [slugs]) two or more people could be meant: never guessed
       (None, [])      nobody known by it"""
    key = _core(name)
    if not key:
        return None, []
    exact = sorted(s for s, p in index.items() if key in {_core(n) for n in _names(p)})
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact
    if ' ' not in key:
        first = sorted(s for s, p in index.items() if _first(p) == key)
        if len(first) == 1:
            return first[0], []
        if len(first) > 1:
            return None, first
    return None, []


def _word_re(name: str):
    """Finds the name as whole words only ("Jordan" is not in "Jordanson"). Capitals do not matter, except for very short
    names and names that are also everyday words, which must be written as a name."""
    words = name.split()
    as_name = len(words) == 1 and (len(words[0]) < 3 or words[0].lower() in EVERYDAY)
    if as_name:
        words = [words[0][:1].upper() + words[0][1:]]
    pat = r'(?<![^\W_])' + r'[\s.]+'.join(re.escape(w) for w in words) + r'(?![^\W_])'
    return re.compile(pat, 0 if as_name else re.I)


def _variants(person: dict) -> list:
    seen, out = set(), []
    for n in _names(person):
        for v in (re.sub(r'[.,]', ' ', str(n)).strip(), _core(n)):
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out.append(v)
    return out


def _names_in(text: str, person: dict) -> bool:
    text = str(text).replace('’', "'")
    return any(_word_re(v).search(text) for v in _variants(person))


def _match(index: dict, text: str) -> list:
    text = str(text or '').replace('’', "'").replace('‘', "'")
    if not text.strip() or not index:
        return []
    hits = [(m.start(), m.end(), slug) for slug, p in index.items() for v in _variants(p) for m in _word_re(v).finditer(text)]
    # The longest name wins a stretch of text: "Taylor Smith" is not also a mention of someone called just "Taylor".
    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
    taken = []
    for h in hits:
        if not any(h[0] < t[1] and t[0] < h[1] and t[2] != h[2] for t in taken):
            taken.append(h)
    found = {}
    for start, _, slug in taken:
        found[slug] = min(found.get(slug, start), start)
    # A bare first name counts only when exactly one known person has it, nobody is known by that word alone, and
    # the next word is not a different surname ("Taylor Jones" is not our Taylor Smith).
    firsts = {}
    for slug, p in index.items():
        if _first(p):
            firsts.setdefault(_first(p), []).append(slug)
    everyone = {_core(n) for p in index.values() for n in _names(p)}
    for first, slugs in firsts.items():
        if len(slugs) != 1 or slugs[0] in found or first in everyone:
            continue
        for m in _word_re(first).finditer(text):
            if any(m.start() < t[1] and t[0] < m.end() for t in taken):
                continue
            if re.match(r"[ \t]+[A-Z][\w'-]+", text[m.end():]):
                continue
            found[slugs[0]] = m.start()
            break
    return [s for s, _ in sorted(found.items(), key=lambda kv: kv[1])]


def match(life_home, text) -> list:
    """Slugs of the known people named in a piece of text, in the order they appear. Whole words, aliases included,
    capitals ignored. Never raises: an unreadable index means nobody."""
    try:
        return _match(load(life_home), text_of(text))
    except Exception:
        return []


# ---------- what the noticer is told ----------

def _note(person: dict, now=None) -> str:
    """One or two plain sentences about this person, newest and most useful first."""
    name, sup = person['name'], person.get('support') or {}
    his, said = person.get('his', 'unknown'), sup.get('his') or {}
    parts = [AGENT_LINE.format(name=name)] if is_agent(person) else []
    if his == 'no' and said:
        parts.append(f"{OWNER} said on {_day(said.get('at'))} that things about {name} are not for them. Their words: \"{_plain(said.get('quote', ''))}\"")
    elif his == 'yes' and said:
        parts.append(f"{OWNER} said on {_day(said.get('at'))} that they want to hear about {name}.")
    facts = sorted(person.get('facts') or [], key=lambda f: -(f.get('at') or 0))
    def already_told(f):                      # the statement the first sentence already quotes is not said twice
        return (his in ('yes', 'no') and said and str(f.get('message_id')) == str(said.get('message_id'))
                and _flat(said.get('quote', '')) in _flat(f.get('quote', '')))
    fresh = [f for f in facts if not already_told(f)]
    if fresh:
        parts.append(f"From what {OWNER} said on {_day(fresh[0].get('at'))}: {_sentence(fresh[0].get('text', ''))}")
    threads = _open_threads(person, now)
    if threads:
        parts.append('Still open: ' + _plain(threads[0].get('text', '')))
    if not parts:
        parts.append(f"{OWNER} mentioned {name} on {_day(person.get('last_seen'))}.")
    return ' '.join(parts)[:300]


def known_people(life_home, text=None, limit=12, named_only=False) -> list:
    """What the noticer should know about people before it reads an item: [{'name', 'relation', 'note'}].
    People named in `text` (a string, or a whole observation) come first, then the closest, then the most recent.
    named_only=True: only the people the text really names. The tick uses this: the full list would put a person who
    "is not for the owner" in front of every email, named or not.
    Never raises: any trouble gives an empty list, because noticing must go on without it."""
    try:
        index = load(life_home)
        named = _match(index, text_of(text)) if text else []
        if named_only:
            index = {s: index[s] for s in named}
        def rank(slug):
            p = index[slug]
            return (-(p.get('closeness') or DEFAULT_CLOSENESS), -(p.get('last_seen') or 0), slug)
        order = sorted(named, key=rank) + sorted((s for s in index if s not in named), key=rank)
        return [{'name': index[s]['name'], 'relation': index[s].get('relation') or (f"one of {OWNER}'s own agents" if is_agent(index[s]) else 'not known'), 'note': _note(index[s])}
                for s in order[:max(0, int(limit))]]
    except Exception:
        return []


def page(life_home, name) -> str:
    """The readable page for one person, found by name, alias or slug. Empty when nobody (or more than one person)
    is known by that name."""
    try:
        index = load(life_home)
    except Unreadable:
        return ''
    slug, _ = _find(index, str(name))
    if slug is None and str(name) in index:
        slug = str(name)
    return _page_text(life_home, slug, index[slug]) if slug else ''


# ---------- joining two spellings: only ever on purpose ----------

def add_alias(life_home, name, alias) -> dict:
    """Say that `alias` is another way of writing the person known as `name`. Refused when the alias already belongs
    to somebody else (use merge for that) or when `name` is not exactly one person."""
    index = load(life_home)
    slug, _ = _find(index, str(name))
    if slug is None and str(name) in index:
        slug = str(name)
    if slug is None:
        return {'ok': False, 'why': f'nobody (or more than one person) is known as {name}'}
    if _core(alias) in {_core(n) for s, p in index.items() if s != slug for n in _names(p)}:
        return {'ok': False, 'why': f'{alias} is already someone else: merge the two pages if they are one person'}
    try:                                           # a name the owner said belongs to a different person is never made an alias
        import pages
        other = next((s for s, e in pages.load_sources(life_home).items() if _core(alias) in {_core(e.get('name') or '')} | {_core(a) for a in e.get('aliases') or []}
                      and (not_same(index[slug], s) or not_same(e, slug))), None)
    except Exception:
        other = None
    if other:
        return {'ok': False, 'why': f'the owner said {index[slug]["name"]} and the person on page {other} are two different people'}
    alias = _show(alias)
    if _core(alias) not in {_core(n) for n in _names(index[slug])}:
        index[slug].setdefault('aliases', []).append(alias)
        _save(life_home, index, _load_state(life_home))
    return {'ok': True, 'person': slug}


def merge(life_home, keep, other) -> dict:
    """Two pages turned out to be one person. Everything on `other` moves onto `keep`; `other`'s name becomes an alias.
    Where both pages say something different (who they are, whether to bring things to the owner), `keep` wins."""
    index = load(life_home)
    a = keep if keep in index else _find(index, str(keep))[0]
    b = other if other in index else _find(index, str(other))[0]
    if not a or not b or a == b:
        return {'ok': False, 'why': 'need two different known people'}
    if not_same(index[a], b) or not_same(index[b], a):
        return {'ok': False, 'why': 'the owner said these are two different people'}
    pa, pb = index[a], index.pop(b)
    have = {_core(n) for n in _names(pa)}
    for n in _names(pb):
        if _core(n) not in have:
            pa.setdefault('aliases', []).append(n)
            have.add(_core(n))
    for kind in ('facts', 'open_threads', 'closed_threads'):
        for item in pb.get(kind) or []:
            _add_once(pa.setdefault(kind, []), item)
    for field in ('relation', 'closeness', 'his'):
        if (pb.get('support') or {}).get(field) and not (pa.get('support') or {}).get(field):
            pa[field] = pb[field]
            pa.setdefault('support', {})[field] = pb['support'][field]
    pa['last_seen'] = max(pa.get('last_seen') or 0, pb.get('last_seen') or 0)
    _save(life_home, index, _load_state(life_home))
    stale = _dir(life_home) / f'{b}.md'       # our own page for the name that no longer exists
    if stale.exists():
        stale.unlink()
    try:                                       # what email and contact sources said about the folded page moves too
        if (_dir(life_home) / 'sources.json').exists():
            import pages
            if pages.rekey(life_home, b, a):
                _write(_dir(life_home) / f'{a}.md', _page_text(life_home, a, pa))
    except Exception:
        pass
    return {'ok': True, 'person': a, 'folded_in': b}


# ---------- folding new learned rows into the pages ----------

def _read_learned(life_home) -> list:
    """[(key, row)] in file order. A line that is not finished or not valid is left for a later run."""
    try:
        lines = (_home(life_home) / 'learned.jsonl').read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append((str(row.get('id') or '') or 'h:' + hashlib.sha1(line.encode()).hexdigest()[:16], row))
    return out


def _clean_names(people) -> list:
    """The people a row names, tidied. The quiet pass may hand over the owner's family words as the owner said them ("my sister",
    "Mom and Grandpa"): those become "sister", "Mom" and "Grandpa", each their own page until a real name is joined on purpose."""
    parts = []
    for raw in people if isinstance(people, list) else []:
        if isinstance(raw, str):
            parts += re.split(r'\s+(?:and|&)\s+|\s*,\s*', raw)
    out, seen = [], set()
    for n in parts:
        n = re.sub(r'^(my|our|his|her|the)\s+', '', re.sub(r'\s+', ' ', n).strip(), flags=re.I)
        key = _core(n)
        if not key or len(n) > 60 or not re.search(r'[^\W\d_]', n) or key in NOT_PEOPLE or key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def _add_once(items: list, item: dict) -> bool:
    """The same words from the same message are one statement, however a later pass re-worded the summary."""
    mark = (str(item.get('message_id')), _flat(item.get('quote', '')))
    if any((str(i.get('message_id')), _flat(i.get('quote', ''))) == mark for i in items):
        return False
    items.append(item)
    return True


def let_go(life_home, row: dict) -> list:
    """The owner said (from the phone page, switch file knows-page-on) a learned statement is wrong or asked
    Zeus to forget it, so it comes off every person's page it was folded into: the fact, or the open thread for a
    promise. Matched by its message and its sentence, never by name. The row stays marked as folded in state.json, so
    update() never adds it back; who someone is, how close and "not for the owner" are left as they are (each has its own
    words). -> the names of the people whose pages changed. The caller holds the tick's lock."""
    try:
        index = load(life_home)
    except Unreadable:
        return []
    mark = (str(row.get('message_id')), str(row.get('text') or '').strip())
    changed = []
    for person in index.values():
        hit = False
        for field in ('facts', 'open_threads', 'closed_threads'):
            items = person.get(field) or []
            keep = [i for i in items if (str(i.get('message_id')), str(i.get('text') or '').strip()) != mark]
            if len(keep) != len(items):
                person[field], hit = keep, True
        if hit:
            changed.append(person['name'])
    if changed:
        _save(life_home, index, _load_state(life_home))
    return changed


def _backed(quote, row: dict, person: dict, others: list) -> str | None:
    """Why these supporting words cannot be used, or None when they can. They must be in what the owner really said; and
    when the statement names several people, they must name this one and none of the others."""
    if not isinstance(quote, str) or not quote.strip():
        return 'no supporting words were given'
    if C.quoted_in(quote, [{'who': 'owner', 'id': row.get('message_id'), 'text': row.get('quote', '')}]) is None:
        return "the supporting words are not in what the owner said"
    if others:
        if not _names_in(quote, person):
            return 'the statement names several people and these words do not name this one'
        if any(_names_in(quote, o) for o in others):
            return 'the supporting words also name someone else'
    return None


def _his_words(piece: str, quote: str) -> str:
    """The same piece exactly as the owner typed it (the owner's capitals, the owner's apostrophes, the owner's spacing), cut from the full quote."""
    def ch(c):
        if c in "'’‘":
            return "['’‘]"
        if c in '"“”':
            return '["“”]'
        return re.escape(c)
    m = re.search(r'\s+'.join(''.join(ch(c) for c in w) for w in piece.split()), quote, re.I)
    return m.group(0) if m else piece.strip()


def _relation_backed(relation: str, quote: str) -> bool:
    """A relation is kept only when it is made of the owner's own words: every real word in it must be in the supporting
    words ("cardiologist" for "cardiology" and "doctor" for "dr" are close enough; "wife" for "Taylor" is not)."""
    have = set(re.findall(r"[a-z0-9]+", _flat(quote).replace("'", '')))
    words = [w for w in re.findall(r"[a-z0-9]+", _flat(relation).replace("'", '')) if w not in FILLER]
    if not words:
        return False

    def said(w):
        if w in have or any(w in g and g & have for g in SAME_WORD):
            return True
        return len(w) >= 5 and any(len(h) >= 5 and h[:5] == w[:5] for h in have)
    return all(said(w) for w in words)


def _ask(complete, items: list, index: dict):
    """One model call for every new statement of this run. -> (answers list, cost) or None when the model failed."""
    statements = {}
    for it in items:
        row = it['row']
        s = statements.setdefault(it['key'], {'row': it['key'], 'kind': row.get('kind'), 'said_on': _day(row.get('said_at'), year=True),
                                              'text': row.get('text'), 'quote': row.get('quote'), 'people': []})
        p = index[it['slug']]
        s['people'].append({'name': it['name'], 'already_known': {'relation': p.get('relation') or None, 'his': p.get('his', 'unknown')}})
    prompt = json.dumps({'statements': list(statements.values())}, ensure_ascii=False, default=str)[:24000]
    r = complete(SYSTEM, prompt, SCHEMA, 150)
    if r is None or not isinstance(r.get('out'), dict) or not isinstance(r['out'].get('people'), list):
        return None
    answers = [a for a in r['out']['people'] if isinstance(a, dict)]
    expected = {(it['key'], _core(it['name'])) for it in items}
    returned = {(str(a.get('row')), _core(str(a.get('name') or ''))) for a in answers}
    # An explicitly empty extraction means there are no inferred attributes.
    # Keep the owner's quoted statement. A partial nonempty answer is incomplete.
    if r['out']['people'] and not expected <= returned:
        return None
    return answers, r.get('cost')


def _real_complete():
    """The live engine's own model call (noticer.complete). Fetched only when no function was handed in, and without
    leaving compiled files next to the engine's code."""
    eng = HERE if (HERE / 'noticer.py').exists() else Path(ENGINE)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        if str(eng) not in sys.path:
            sys.path.append(str(eng))
        import noticer
    finally:
        sys.dont_write_bytecode = old
    return noticer.complete


def update(life_home=None, complete=None, dry=False, now=None, max_rows=20) -> dict:
    """Fold the NEW rows of learned.jsonl into the people pages. -> a report (counts, what was kept, what was thrown out).
    No new statement about anyone means no model call. A failed model call writes nothing about those rows and
    leaves them for the next run. `dry` writes nothing at all and puts the would-be pages in report['preview']."""
    now = now or time.time()
    report = {'dry': bool(dry), 'rows_new': 0, 'rows_folded': 0, 'rows_waiting': 0, 'calls': 0, 'cost': 0.0,
              'people_new': [], 'people_changed': [], 'facts_added': 0, 'threads_added': 0, 'kept': [], 'dropped': [],
              'unfiled': [], 'skipped': [], 'errors': []}
    try:
        live_index = load(life_home)
    except Unreadable as e:
        report['errors'].append(f'the people index cannot be read, nothing was changed: {e}')
        return report
    if not dry:
        try:                                       # every tick, no model, free when nobody has an open thread
            done = settle_threads(life_home, now)
            if done:
                report['threads_closed'] = done
                live_index = load(life_home)
        except Exception as e:                     # never an alarm: the threads are simply looked at again next tick
            report['threads_error'] = type(e).__name__
    state = _load_state(life_home)
    report['retry_pending'] = len(state.get('retry_order', {}))
    fresh = [(k, r) for k, r in _read_learned(life_home) if k not in state['done']]
    report['rows_new'] = len(fresh)
    if not fresh:
        return report

    work = []
    for key, row in fresh:
        names = _clean_names(row.get('people'))
        if not names:
            state['done'][key] = now              # names nobody: nothing for these pages, and never looked at again
            continue
        if not (isinstance(row.get('quote'), str) and len(_flat(row['quote'])) >= 3 and row.get('message_id') is not None and row.get('text')):
            state['done'][key] = now              # no words of the owner's, or no message to point at: no item
            report['skipped'].append({'row': key, 'why': 'no quote, message or sentence to keep'})
            continue
        if row.get('passing'):
            state['done'][key] = now              # true only for now (the quiet pass marked it): never a line on someone's page
            report['skipped'].append({'row': key, 'why': 'a passing matter, not a lasting fact'})
            continue
        work.append((key, row, names))
    work.sort(key=lambda w: (w[1].get('said_at') or 0))
    # A failed batch is split on the next tick. Rotate failed sources so one
    # unreadable row cannot keep unrelated corrections behind it indefinitely.
    retry = state.get('retry_order', {})
    work.sort(key=lambda w: retry.get(w[0], 0))
    if retry:
        max_rows = min(max_rows, 1)
    report['rows_waiting'] = max(0, len(work) - max_rows)
    work = work[:max_rows]

    # Stage 1, no model: who is meant, and is this statement new? Done on a copy so a failed model call changes nothing.
    index = copy.deepcopy(live_index)
    staged_done, items, unfiled = {}, [], []
    for key, row, names in work:
        said_at = row.get('said_at') or row.get('at') or now
        entry = {'text': str(row['text']).strip(), 'quote': row['quote'], 'message_id': row['message_id'], 'at': said_at,
                 'kind': row.get('kind') or 'fact'}
        entry.update({k: row[k] for k in ('source_ref', 'speaker', 'verified') if row.get(k)})   # a statement the owner made outside the chat keeps its own source label (claims.fields)
        __import__('claims').stamp(_home(life_home), 'person', entry)   # claims.py (switch claims-on): who said it, from where, when, how sure; without the switch the statement is untouched
        placed, unclear = [], []
        for name in names:
            slug, several = _find(index, name)
            if several:
                unfiled.append({'row': key, 'name': name, 'could_be': several, **entry})
                unclear.append({'name': name})
                continue
            if slug is None:
                slug = _slug(_show(name), index)
                index[slug] = {'name': _show(name), 'aliases': [], 'relation': '', 'closeness': DEFAULT_CLOSENESS, 'his': 'unknown',
                               'support': {}, 'facts': [], 'open_threads': [], 'last_seen': said_at}
                report['people_new'].append(index[slug]['name'])
            placed.append((name, slug))
        for name, slug in placed:
            person = index[slug]
            if entry['kind'] == 'promise':
                added = _add_once(person.setdefault('open_threads', []), {**entry, 'due': row.get('due')})
                report['threads_added'] += added
            else:
                added = _add_once(person.setdefault('facts', []), dict(entry))
                report['facts_added'] += added
            person['last_seen'] = max(person.get('last_seen') or 0, said_at)
            if added:
                items.append({'key': key, 'row': row, 'name': name, 'slug': slug, 'at': said_at,
                              'others': [index[s] for _, s in placed if s != slug] + unclear})
        staged_done[key] = now

    # Stage 2, one model call: who they are to the owner, how close, and whether to bring the owner things about them.
    if items:
        if complete is None:
            complete = _real_complete()
        report['calls'] = 1
        got = _ask(complete, items, index)
        if got is None:
            # Model failed: the rows that named people stay un-bookmarked and are tried again next run.
            report['errors'].append('model call failed: these statements wait for the next run')
            report.update(people_new=[], facts_added=0, threads_added=0, unfiled=[])
            if not dry:
                state.setdefault('retry_order', {}).update({key: now for key, _, _ in work})
                report['retry_pending'] = len(state['retry_order'])
                _dir(life_home).mkdir(parents=True, exist_ok=True)
                _write(_dir(life_home) / 'state.json', json.dumps(state, indent=2, ensure_ascii=False))
            return report
        answers, cost = got
        report['cost'] = cost or 0.0
        by = {(it['key'], _core(it['name'])): it for it in items}
        for a in answers:
            it = by.get((str(a.get('row')), _core(a.get('name') or '')))
            if it is None:
                report['dropped'].append({'person': a.get('name'), 'field': 'all', 'why': 'not a person named in that statement'})
                continue
            _apply(index[it['slug']], a, it, report)

    changed = sorted({index[s]['name'] for s in index if index[s] != live_index.get(s)})
    report['people_changed'] = [n for n in changed if n not in report['people_new']]
    report['rows_folded'] = len(staged_done)
    report['unfiled'] = [{'name': u['name'], 'could_be': u['could_be']} for u in unfiled]
    state['done'].update(staged_done)
    if state.get('retry_order'):
        state['retry_order'] = {k:v for k,v in state['retry_order'].items() if k not in state['done']}
    report['retry_pending'] = len(state.get('retry_order', {}))
    for u in unfiled:
        if not any(x.get('row') == u['row'] and x.get('name') == u['name'] for x in state['unfiled']):
            state['unfiled'].append(u)
    if dry:
        report['preview'] = {'index': index, 'pages': {s: render(p, now) for s, p in index.items() if p != live_index.get(s)}}
        return report
    _save(life_home, index, state)
    return report


def _apply(person: dict, answer: dict, item: dict, report: dict):
    """Keep each thing the model said only when the owner's own words back it. The newest statement wins."""
    row, name = item['row'], person['name']
    sup = person.setdefault('support', {})

    def keep(field, value, quote):
        if (sup.get(field) or {}).get('at', 0) > item['at']:
            return                                  # an older statement never overrides a newer one
        person[field] = value
        sup[field] = {'quote': _his_words(quote, row['quote']), 'message_id': row['message_id'], 'at': item['at']}
        if row.get('asked') and field == 'relation':
            sup[field]['asked'] = str(row['asked'])[:300]
        report['kept'].append({'person': name, 'field': field, 'value': value})

    def drop(field, value, why):
        report['dropped'].append({'person': name, 'field': field, 'value': value, 'why': why})

    relation = answer.get('relation')
    if isinstance(relation, str) and relation.strip():
        relation = re.sub(r"^(" + '|'.join([re.escape(n) + "'s" for n in OWNER_NAMES] + ['his', 'her', 'their', 'my']) + r")\s+", '', _plain(relation), flags=re.I)[:60]
        why = _backed(answer.get('relation_quote'), row, person, item['others'])
        # `asked` is the question the owner's words answered, kept on the row word for word. The owner's answer still has to
        # be the owner's own words naming this person; the relation may then also rest on the question ("which are yours?").
        if why is None and not _relation_backed(relation, answer['relation_quote'] + (' ' + str(row['asked']) if row.get('asked') else '')):
            why = "the relation is not in the owner's own words"
        keep('relation', relation, answer['relation_quote']) if why is None else drop('relation', relation, why)

    close = answer.get('closeness')
    if isinstance(close, int) and not isinstance(close, bool):
        why = _backed(answer.get('closeness_quote'), row, person, item['others'])
        if why is None and not 1 <= close <= 5:
            why = 'closeness must be 1 to 5'
        keep('closeness', close, answer['closeness_quote']) if why is None else drop('closeness', close, why)

    his = answer.get('his')
    if his in ('yes', 'no'):
        why = _backed(answer.get('his_quote'), row, person, item['others'])
        if why is None and his == 'no':
            # "Not for me" hides a person's items from the owner, so it gets the strictest check: the words must name the
            # person and must actually say no. "Disregard that" about one item is not about the person.
            if not _names_in(answer['his_quote'], person):
                why = 'a "not for me" must name the person in the owner\'s own words'
            elif not SAYS_NO.search(answer['his_quote'].replace('’', "'")):
                why = 'the supporting words do not say no'
        keep('his', his, answer['his_quote']) if why is None else drop('his', his, why)


def main(argv=None):
    """Small hand tool: update [--dry] | known [text] | page NAME | match TEXT   (life_home from LIFE_HOME)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ''
    home = os.environ.get('LIFE_HOME')
    if cmd == 'update':
        out = update(home, dry='--dry' in argv)
        out.pop('preview', None)
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    elif cmd == 'known':
        print(json.dumps(known_people(home, ' '.join(argv[1:]) or None), indent=2, ensure_ascii=False))
    elif cmd == 'page' and len(argv) > 1:
        print(page(home, ' '.join(argv[1:])) or 'Nobody is known by that name.')
    elif cmd == 'match' and len(argv) > 1:
        print(json.dumps(match(home, ' '.join(argv[1:]))))
    else:
        print(main.__doc__)


if __name__ == '__main__':
    main()

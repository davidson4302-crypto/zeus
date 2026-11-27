"""quietpass: about 20 minutes after a chat with the owner goes quiet, read it once and keep what matters.

Without it a fact the owner stated in chat would be kept only if the chat model remembered to call a save tool in
that turn, and nothing would spot a promise or a date said in chat. This is one bounded sweep after a conversation goes
quiet. It only ever updates memory, never sends a message.

What it keeps, each with the owner's exact words and the message they came from:
  fact        something the owner stated about themselves, their people, their places or their preferences
  promise     something the owner wants handled, watched or remembered later ("remind me", "keep an eye on this", a visitor)
  correction  the owner telling Zeus it got something wrong, or how to behave from now on ("Taylor's stuff isn't for me")

Where it goes (all inside life_home):
  learned.jsonl        every kept item, one line each (read by the people pages and the nightly page)
  ledger.sqlite3       a promise also becomes a life item, built the same way `life add` builds one in chat
  preferences.json     a correction also becomes a line in a new `rules` list
  facts-outbox.jsonl   a lasting fact also waits here for a later memory writer (this module never writes memory itself)
  learn-state.json     the bookmark: the last message already looked at

Code has the last word over the model: no quote found in a NEW message of the owner's, no item. Zeus's words are never
a source. The pass never sends anything and never asks the owner anything. With no new quiet conversation it makes no
model call at all, so it is free to run on every 5-minute tick.

Switch: a file named learn-off in life_home stops the pass.
Usage: python3 -I quietpass.py --life-home <dir> [--dry] [--hours 48] [--max 2] [--out report.json] [--seed]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chats as C  # noqa: E402
import settings  # noqa: E402
import ladder as LD  # noqa: E402  (switch file ladder-on; without it every LD call below hands back what it was given)

ENGINE = os.environ.get('LIFE_ENGINE_DIR', str(settings.ENGINE_DIR))
from ownertime import PACIFIC, said_where  # the zone the owner is in right now: home time unless they said otherwise (ownertime.py)
QUIET_MINUTES = 20        # minutes of silence before a chat counts as over (1,200 seconds)
WINDOW_HOURS = 48         # older chats are not looked at; also the longest one failing chat can hold up the queue
MIN_CONFIDENCE = 0.75
MAX_ITEMS = 6             # per conversation; a normal chat yields zero or one
MAX_RULES = 40
MIN_WORDS = 3             # "Ping" or "ok" is not worth a model call
TIMEOUT = 120
KINDS = ('fact', 'promise', 'correction')
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
ENGINE_JOBS = ('life-exec:', 'life-work:', 'life-sim:', 'zeus-work:')   # the engine's own background jobs, not something set up for the owner
# A message the chat gateway relayed from the owner starts with this label, e.g. "[Alex, by email to your inbox ...]".
RELAY_LABEL = '[' + str(settings.get('chat.relay_label', settings.OWNER_FIRST)) + ','

SCHEMA = {'type': 'object', 'required': ['items'], 'properties': {'items': {'type': 'array', 'items': {
    'type': 'object',
    'required': ['kind', 'text', 'quote', 'message_id', 'people', 'due', 'confidence'],
    'properties': {
        'kind': {'enum': list(KINDS)},
        'text': {'type': 'string', 'description': 'one short plain sentence that stands on its own without the chat'},
        'quote': {'type': 'string', 'description': "the owner's exact words, copied from one NEW line of theirs"},
        'message_id': {'type': 'integer', 'description': 'the id of the line the quote was copied from'},
        'people': {'type': 'array', 'items': {'type': 'string'}, 'description': 'names or family words the owner personally used'},
        'due': {'type': ['string', 'null'], 'description': 'ISO 8601 time with the local UTC offset, or null'},
        'confidence': {'type': 'number'},
        'durable': {'type': 'boolean', 'description': 'fact only: true if it will still be true and useful in a month'},
        'covered_by': {'type': ['string', 'null'], 'description': 'promise only: the exact entry from already_on_the_list that is this same thing, else null'},
    }}}}}

SYSTEM = """You are the quiet look-back for {AGENT}, {OWNER}'s personal assistant. A conversation between {OWNER} and
{AGENT} ended a while ago. Read it once and decide whether anything {OWNER} said is worth keeping after the chat is gone.

Most conversations are questions, small talk, tests, or a request {AGENT} already finished in that same chat. For those the
right answer is an empty list. Returning nothing is the normal, correct answer. Never keep something just to have an answer.

Keep only these three kinds, and only from lines marked new that {OWNER} personally said:

fact: something {OWNER} stated about themselves, their people, their places or their preferences (a name, a
  relationship, a birthday, an address, how they like things). Also a plain statement about where one of their matters
  stands that will matter later ("I already spoke to Jordan"). Set durable to true when it will still be true in a
  month, false when it is about one passing matter.

promise: something {OWNER} wants handled, watched or remembered later that is NOT finished when the chat ends:
  "remind me", "keep an eye on this", a visitor, a trip, a deadline, a date they mentioned that needs preparing for.
  Write text as the job to do, complete enough to act on without the chat ("Remind {OWNER} to call their sister Casey on
  her birthday"). already_on_the_list holds what {AGENT} is already tracking or has already scheduled. If one entry is
  clearly this same thing, still return the promise and copy that entry exactly into covered_by.

correction: {OWNER} telling {AGENT} it got something wrong, or how {AGENT} should behave from now on ("Taylor's stuff
  isn't for me", "I asked YOU to do it", "quiet hours are 9pm to 9am"). Write text as the rule {AGENT} should follow next
  time, in plain words ("Taylor's matters are not {OWNER}'s, do not bring them to {OWNER}").

Hard rules:
- {AGENT}'s lines are context only. They may tell you what "this" or "that" points at. Never take a fact, a name, a number
  or a date from {AGENT}'s words, and never keep something {AGENT} said, offered or suggested.
- quote is {OWNER}'s exact words, copied character for character from ONE new line of theirs, and message_id is the id
  of that line. If you cannot quote them, there is no item.
- text says only what {OWNER}'s own words support. Do not add details they did not say.
- Do not keep a one-off instruction that {AGENT} already carried out in this same conversation ("add the address",
  "keep looking", "send it", "stop that reminder" followed by {AGENT} doing it). Do not keep questions, greetings, test
  messages, thanks, or answers of theirs that only matter to the task in front of them.
- Do not repeat anything in already_learned.
- people: only the names or family words {OWNER} personally used for the people this is about ("Casey", "Mom", "Jordan").
  An empty list if there are none.
- due: for a promise or a dated fact, the real time it points at, as ISO 8601 with the local UTC offset. Work it out
  from the day the line was said (every line shows when it was said), not from today. "Saturday" said on Monday
  November 9 is Saturday November 14. A day with no time is 09:00. null when they gave no time.
- confidence: 0 to 1, how sure you are that this is worth keeping and correctly understood. If you are below 0.75,
  leave it out.
- text is one short sentence in plain everyday English. No dashes as punctuation, no technical words, no file names.
- At most 6 items. Usually zero or one.""".replace('{OWNER}', settings.OWNER_FIRST).replace('{AGENT}', settings.AGENT_NAME)


# ---- small helpers -------------------------------------------------------------------------------------------------

def _flat(s) -> str:
    """Same forgiving comparison chats.quoted_in uses: curly quotes straightened, spaces squeezed, lower case."""
    s = str(s).replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    return re.sub(r'\s+', ' ', s).strip().lower()


def _key(s) -> str:
    """For spotting the same thing said twice: letters and digits only."""
    return re.sub(r'[^a-z0-9]+', ' ', _flat(s)).strip()


def _plain(s, limit=300) -> str:
    """Text a person may read: one line, no long dashes."""
    s = re.sub(r'\s*[—–]\s*', ', ', str(s or ''))
    return re.sub(r'\s+', ' ', s).strip()[:limit]


def _stamp(now: float) -> str:
    return datetime.fromtimestamp(now, PACIFIC).strftime('%Y-%m-%dT%H:%M:%S%z')


def own_words(text: str) -> str:
    """Only what the owner typed. A delivery label (RELAY_LABEL, e.g. "[Alex, by email to your inbox ...]"), the relay
    note behind it, an email footer, a voice-message stub or a pasted chat thread from someone else are in the stored
    message but are not the owner's words, so nothing in them may be quoted. An unknown bracket wrapper counts as not
    theirs: a missed item is cheaper than a wrong one."""
    t = str(text or '').strip()
    if not t.startswith('['):
        return t
    if not t.startswith(RELAY_LABEL):
        return ''
    if t.startswith(RELAY_LABEL + ' by email'):
        i = t.find('Subject:')
        if i < 0:
            return ''
        return t[i + len('Subject:'):].split('\n-- \n')[0].strip()
    return re.sub(r'^\[[^\]]{0,300}\]\s*', '', t, count=1).strip()


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write_json(p: Path, d):
    """Whole file swapped in one step, so a crash never leaves half a file."""
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    tmp.replace(p)


def _read_lines(p: Path) -> list:
    out = []
    try:
        for line in p.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue           # one damaged line must not hide the rest
            if isinstance(row, dict):
                out.append(row)
    except OSError:
        pass
    return out


def _append_lines(p: Path, rows: list):
    if rows:
        with open(p, 'a') as fh:
            fh.write(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


def _real_complete():
    """The live engine's model call. Imported only when no fake was passed in."""
    try:
        import noticer
    except ImportError:
        sys.path.append(ENGINE)
        import noticer
    return noticer.complete


# ---- the model step ------------------------------------------------------------------------------------------------

def _said(at: float) -> str:
    return datetime.fromtimestamp(at, PACIFIC).strftime('%A %B %d %Y, %I:%M %p').replace(' 0', ' ')


def build_prompt(conv: list, bookmark: int, now: float, learned: list, on_list: list) -> str:
    """The conversation as lines. Earlier lines of a chat that grew are shown for context but only new lines may be
    quoted. Zeus's long answers are cut: they are context, never a source."""
    lines = []
    for m in conv[-80:]:
        if m['who'] == 'owner':
            text = own_words(m['text'])[:2000]
            if text:
                lines.append({'id': m['id'], 'who': settings.OWNER_FIRST, 'said': _said(m['at']), 'new': m['id'] > bookmark, 'text': text})
        else:
            lines.append({'who': 'Zeus (context only, never a source)', 'said': _said(m['at']), 'text': m['text'][:500]})
    return json.dumps({'today': _said(now) + said_where(now), 'conversation': lines,
                       'already_learned': [r.get('text') for r in learned[-30:]],
                       'already_on_the_list': on_list[:40]}, ensure_ascii=False, indent=1)


def _due(raw):
    """-> ISO time on the owner's clock, or None when there is no time or it cannot be read (the item is kept without one).
    A bare date means 9am that day, never midnight."""
    raw = str(raw or '').strip()
    try:
        t = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    if len(raw) <= 10:
        t = t.replace(hour=9)
    return (t if t.tzinfo else t.replace(tzinfo=PACIFIC)).astimezone(PACIFIC).isoformat()


def _loose_numbers(text: str, his_text: str, due, said_at: float) -> list:
    """Numbers in the sentence that the owner never said. A flight number or an address that only Zeus mentioned must
    not ride into a learned fact on the back of a real quote. The date they spoke and the worked-out due date are allowed."""
    said = datetime.fromtimestamp(said_at, PACIFIC)
    ok = {int(n) for n in re.findall(r'\d+', his_text)} | {said.year, said.month, said.day}
    if due:
        ok |= {int(n) for n in re.findall(r'\d+', str(due)[:10])}
    return [n for n in re.findall(r'\d+', text) if int(n) not in ok]


def _he_said(name: str, his_text: str) -> bool:
    """A person counts only if the owner used that name personally (whole word), not if only Zeus did."""
    n = _flat(name)
    return bool(n) and re.search(r'(?<![a-z0-9])' + re.escape(n) + r'(?![a-z0-9])', his_text) is not None


def _same(a: dict, b: dict) -> bool:
    """The same thing learned twice: the same sentence, or the same words of the owner's for the same kind of item."""
    if _key(a.get('text')) == _key(b.get('text')):
        return True
    if a.get('kind') != b.get('kind'):
        return False     # one sentence may hold a fact and a promise ("Casey's birthday is Saturday, remind me")
    qa, qb = _key(a.get('quote')), _key(b.get('quote'))
    short, long_ = sorted((qa, qb), key=len)
    return bool(short) and (short == long_ or (len(short) >= 12 and short in long_))


def check(items: list, conv: list, bookmark: int, known: list, now: float, life=None) -> tuple:
    """Code has the last word. -> (kept rows, dropped [{why, ...}]). A row is kept only when its quote is really in a
    NEW message from the owner, it clears the confidence floor, its details are theirs, and it is not already learned."""
    # with ladder-on in `life`, "the same thing" and "a number the owner never said" are read by ladder.py
    same, loose_numbers = LD.same_test(life, _same), LD.number_test(life, _loose_numbers)
    cid = C.conv_id(conv)
    his = [{**m, 'text': own_words(m['text'])} for m in conv if m['who'] == 'owner']
    his = [m for m in his if m['text']]
    his_new = [m for m in his if m['id'] > bookmark]
    his_all_text = _flat(' '.join(m['text'] for m in his))
    zeus_text = _flat(' '.join(m['text'] for m in conv if m['who'] == 'zeus'))
    kept, dropped = [], []

    def drop(why, it):
        dropped.append({'why': why, 'kind': it.get('kind'), 'text': _plain(it.get('text')), 'quote': str(it.get('quote') or '')[:300],
                        'message_id': it.get('message_id')})

    def sure(it):
        return float(it['confidence']) if isinstance(it.get('confidence'), (int, float)) else 0.0

    for it in sorted((it for it in items if isinstance(it, dict)), key=sure, reverse=True):   # surest first, so the limit cuts the weakest
        text, quote = _plain(it.get('text')), str(it.get('quote') or '').strip()
        if it.get('kind') not in KINDS or len(text) < 8:
            drop('not a usable item', it)
            continue
        src = C.quoted_in(quote, his_new, it.get('message_id')) if len(_flat(quote)) >= 6 else None
        if src is None:
            q = _flat(quote)
            if len(q) >= 6 and q in zeus_text:
                drop("Zeus's words, not the owner's", it)
            elif len(q) >= 6 and q in his_all_text:
                drop('from a message already looked at', it)
            else:
                drop("quote not found in the owner's messages", it)
            continue
        if sure(it) < MIN_CONFIDENCE:
            drop('not sure enough', it)
            continue
        due = _due(it.get('due'))
        if it['kind'] != 'promise':      # a promise needs Zeus's context to say what "this" is; a fact or a rule does not
            loose = loose_numbers(text, his_all_text, due, src['at'])
            if loose:
                drop('has a detail the owner did not say (%s)' % ', '.join(loose[:4]), it)
                continue
        row = {'kind': it['kind'], 'text': text, 'quote': quote[:600], 'message_id': src['id'], 'said_at': src['at'],
               'people': [p.strip() for p in (it.get('people') or []) if isinstance(p, str) and _he_said(p, his_all_text)][:8],
               'due': due, 'conversation': cid, 'at': now,
               '_durable': it.get('durable') is not False, '_covered_by': it.get('covered_by')}
        if any(_same(row, k) for k in known) or any(same(row, k) for k in kept):
            drop('already learned', it)
            continue
        if len(kept) >= MAX_ITEMS:
            drop('over the limit for one conversation', it)
            continue
        kept.append(row)
    return kept, dropped


# ---- effects -------------------------------------------------------------------------------------------------------

def _ledger(life: Path, write: bool):
    """The life ledger, or None when there is none. Never created here: the tick owns it."""
    p = life / 'ledger.sqlite3'
    if not p.exists():
        return None
    con = sqlite3.connect(p, timeout=15) if write else sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def open_items(life: Path) -> list:
    """What is already on the owner's list: [{'key', 'title'}]. An unreadable ledger reads as an empty list here; the promise
    step below reports it properly."""
    try:
        con = _ledger(life, write=False)
        if con is None:
            return []
        with closing(con):
            marks = ','.join('?' * len(OPEN))
            return [{'key': r['key'], 'title': str(json.loads(r['payload']).get('title') or '')[:120]}
                    for r in con.execute(f'SELECT key,payload FROM opps WHERE state IN ({marks}) ORDER BY updated DESC LIMIT 80', OPEN)]
    except (sqlite3.Error, ValueError):
        return []


def scheduled_jobs(jobs_file) -> list:
    """What Zeus itself has already scheduled: [{'key', 'title'}]. A reminder or a watch it set up in chat ("I set two
    checks for Saturday") lives in its job list, not in the ledger, and is real proof the promise is in hand. The
    engine's own background jobs and anything finished or paused are left out."""
    d = _read_json(Path(jobs_file), {})
    jobs = d.get('jobs') if isinstance(d, dict) else d
    out = []
    for j in jobs if isinstance(jobs, list) else []:
        if not isinstance(j, dict):
            continue
        name = str(j.get('name') or '').strip()
        kind = j['schedule'].get('kind') if isinstance(j.get('schedule'), dict) else None
        if name and j.get('state') == 'scheduled' and j.get('enabled', True) and kind != 'interval' and not name.startswith(ENGINE_JOBS):
            out.append({'key': 'job:' + str(j.get('id') or name), 'title': name[:120]})
    return out


def _yearish(w: str) -> bool:
    """A word that starts with a year near now (2026, 2027, 20261120) is a date, never a record id."""
    import time as _t
    y = _t.localtime().tm_year
    return w[:4].isdigit() and y - 1 <= int(w[:4]) <= y + 2


def _words(t) -> set:
    stop = {'the', 'and', 'for', 'with', 'your', settings.OWNER_FIRST.lower(), 'owner', 'his', 'him', 'their', 'them', 'check', 'remind', 'about', 'that', 'this', 'keep'}
    return {w for w in re.findall(r'[a-z0-9]{3,}', str(t).lower()) if w not in stop}


def _covering(what: str, covered_by, items: list):
    """An open item or a scheduled job that is already this same thing -> its key, else None. The model may name one,
    but only a title that is really on the list counts. Otherwise the same test the tick uses: mostly the same words,
    or the same flight / confirmation / record number."""
    named = _key(covered_by) if covered_by else ''
    a = _words(what)
    for o in items:
        if named and named == _key(o['title']):
            return o['key']
        b = _words(o['title']) | _words(o['key'])
        ids = {w for w in a & b if any(ch.isdigit() for ch in w) and len(w) >= 4 and not _yearish(w)}
        if (a and b and len(a & b) / len(a | b) >= 0.45) or ids:
            return o['key']
    return None


def add_life_item(life: Path, row: dict, now: float, dry: bool, jobs: list | None = None) -> dict:
    """A promise becomes an item of its own, built exactly as `life add` builds one in chat (lifecli.add), with the
    owner's words as the why. Skipped when the key exists, or an open item or a job Zeus already scheduled covers it."""
    what = row['text']
    now_dt = datetime.fromtimestamp(now, PACIFIC)
    due = datetime.fromisoformat(row['due']).astimezone(PACIFIC) if row.get('due') else now_dt
    due = max(due, now_dt)       # a time already gone means: work it now
    slug = re.sub(r'[^a-z0-9]+', '-', what.lower()).strip('-')[:48]
    key = f"chat:{slug}:{due.strftime('%Y-%m-%d')}"
    con = _ledger(life, write=not dry)
    if con is None:
        return {'effect': 'life item', 'status': 'no ledger', 'key': key}
    with closing(con):
        if con.execute('SELECT 1 FROM opps WHERE key=?', (key,)).fetchone():
            return {'effect': 'life item', 'status': 'already on the list', 'key': key}
        cover = _covering(what, row.get('_covered_by'), open_items(life) + (jobs or []))
        if cover:
            return {'effect': 'life item', 'status': 'already covered', 'key': cover}
        if dry:
            return {'effect': 'life item', 'status': 'would add', 'key': key, 'work_at': due.isoformat()}
        step = {'do': what, 'action_class': 'prepare_or_research', 'autonomy': 'auto'}
        payload = {'title': what[:120], 'why': row['quote'], 'evidence': row['quote'], 'urgency': 'today', 'dedupe_key': key, 'confidence': 1.0,
                   'owner_involved': 'yes', '_source': 'conversation', 'steps': [step], 'proposed_action': what,
                   'decision': {'autonomy': 'auto', 'surface_at': due.isoformat(), 'channel': 'text', 'steps': [step], 'granted': False,
                                'reason': 'the owner asked in chat'},
                   'from_chat': {'message_id': row['message_id'], 'said_at': row['said_at'], 'found_by': 'quiet-moment pass'}}
        con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)', (key, 'pending', 'auto', due.isoformat(), 'today', 'prepare_or_research',
                                                                   json.dumps(payload), '[]', time.time(), time.time()))
        con.commit()
    return {'effect': 'life item', 'status': 'added', 'key': key, 'work_at': due.isoformat()}


def rules(life) -> list:
    """The owner's standing rules, oldest first (the new `rules` list in preferences.json)."""
    d = _read_json(Path(life) / 'preferences.json', {})
    return [r for r in (d.get('rules') or []) if isinstance(r, dict)] if isinstance(d, dict) else []


def add_rule(life, text: str, quote: str, message_id, now: float, dry: bool = False) -> dict:
    """A correction becomes a standing rule, once. Everything else in the file (muted, notes) is left as it is. If the
    file is there but cannot be read, nothing is written: saving over it would wipe what the owner told Zeus to stop."""
    p = Path(life) / 'preferences.json'
    d = {}
    if p.exists():
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            d = None
        if not isinstance(d, dict):
            return {'effect': 'rule', 'status': 'preferences file unreadable, left alone'}
    have = [r for r in (d.get('rules') or []) if isinstance(r, dict)]
    new = {'text': _plain(text), 'quote': quote, 'message_id': message_id, 'at': _stamp(now)}
    if any(_same({**new, 'kind': 'r'}, {**r, 'kind': 'r'}) for r in have):
        return {'effect': 'rule', 'status': 'already a rule'}
    if dry:
        return {'effect': 'rule', 'status': 'would add', 'text': new['text']}
    __import__('claims').rule(life, new)   # claims.py (switch claims-on): who said it, from where, when, how sure; without the switch the rule is untouched
    d['rules'] = (have + [new])[-MAX_RULES:]
    _write_json(p, d)
    return {'effect': 'rule', 'status': 'added', 'text': new['text']}


def queue_fact(life: Path, row: dict, statement: str, dry: bool) -> dict:
    """A lasting fact waits in facts-outbox.jsonl for a later memory writer. It carries the owner's whole message, because
    the memory store only admits a fact with the complete statement it came from."""
    p = life / 'facts-outbox.jsonl'
    if any(LD.same_test(life, _same)(row, r) for r in _read_lines(p)):
        return {'effect': 'fact for memory', 'status': 'already waiting'}
    if dry:
        return {'effect': 'fact for memory', 'status': 'would queue'}
    _append_lines(p, [{**{k: v for k, v in row.items() if not k.startswith('_')}, 'statement': statement}])
    return {'effect': 'fact for memory', 'status': 'queued'}


# ---- the pass ------------------------------------------------------------------------------------------------------

def seed(life_home, now=None, path=None) -> int:
    """Start from here: mark everything said so far as looked at, so the first live run does not sweep two old days."""
    now = float(now if now is not None else time.time())
    msgs = C.messages(now - WINDOW_HOURS * 3600, now, path=path)
    last = max([m['id'] for m in msgs] or [0])
    life = Path(life_home)
    state = _read_json(life / 'learn-state.json', {})
    state = state if isinstance(state, dict) else {}
    state.update({'last_message_id': max(last, int(state.get('last_message_id') or 0)), 'at': _stamp(now)})
    _write_json(life / 'learn-state.json', state)
    return state['last_message_id']


def run(now=None, life_home=None, complete=None, dry=False, max_conversations=2, path=None, hours=WINDOW_HOURS, jobs_file=None) -> dict:
    """One sweep. Looks only at conversations that have been quiet for 20 minutes and hold a message from the owner newer
    than the bookmark, oldest first, at most max_conversations per run. One model call per conversation. A model
    failure stops the sweep with the bookmark where it was, so nothing is lost and the next run tries again.
    dry=True reads the same things and reports what it would do, and writes nothing at all."""
    if isinstance(now, datetime):
        now = now.timestamp()
    now = float(now if now is not None else time.time())
    life_home = life_home or os.environ.get('LIFE_HOME')
    if not life_home:
        raise ValueError('life_home is needed (or set LIFE_HOME)')
    life = Path(life_home)
    report = {'at': _stamp(now), 'dry': bool(dry), 'calls': 0, 'cost': 0.0, 'learned': 0, 'dropped': {}, 'waiting': [], 'left_for_next_run': 0,
              'errors': [], 'results': []}
    if (life / 'learn-off').exists():
        return report | {'status': 'off'}
    swept = LD.sweep_quietly(life, now, dry)   # no model: what was replaced, ran out or went stale is written down
    if swept:
        report['ladder'] = swept
    state = _read_json(life / 'learn-state.json', {})
    state = state if isinstance(state, dict) else {}
    bookmark = int(state.get('last_message_id') or 0)
    report['bookmark_before'] = bookmark

    msgs = C.messages(now - hours * 3600, now, path=path)
    todo = []
    for conv in C.conversations(msgs, QUIET_MINUTES):
        if not any(m['who'] == 'owner' and m['id'] > bookmark for m in conv):
            continue                                   # nothing new from the owner in this one
        if not C.gone_quiet(conv, now, QUIET_MINUTES):
            report['waiting'].append(C.conv_id(conv))  # the owner may still be mid-chat: left whole for a later run
            break
        todo.append(conv)
    report['left_for_next_run'] = max(0, len(todo) - max_conversations)
    known = _read_lines(life / 'learned.jsonl') if todo else []       # grows as this run learns, so one sweep never learns a thing twice
    # Zeus's own job list sits beside the life folder: <profile>/cron/jobs.json and <profile>/workspace/life-engine
    jobs_file = jobs_file or os.environ.get('LIFE_JOBS_FILE') or life.parent.parent / 'cron' / 'jobs.json'

    for conv in todo[:max_conversations]:
        cid = C.conv_id(conv)
        new_his = [own_words(m['text']) for m in conv if m['who'] == 'owner' and m['id'] > bookmark]
        entry = {'conversation': cid, 'from': _stamp(conv[0]['at']), 'to': _stamp(conv[-1]['at']),
                 'his_new_messages': len(new_his), 'kept': [], 'dropped': [], 'effects': []}
        jobs = []
        if len(' '.join(new_his).split()) < MIN_WORDS:
            entry['skipped'] = 'too little said to be worth a look'
            items = []
        else:
            if complete is None:
                complete = _real_complete()
            jobs = scheduled_jobs(jobs_file)
            res = complete(*LD.ask(life, SYSTEM, build_prompt(conv, bookmark, now, known, [o['title'] for o in open_items(life) + jobs]), SCHEMA, now), timeout=TIMEOUT)
            report['calls'] += 1
            items = (res or {}).get('out', {}).get('items') if isinstance((res or {}).get('out'), dict) else None
            if not isinstance(items, list):
                # Model failed or answered nonsense: keep the bookmark, try again next run, and say how long it has been stuck.
                report['errors'].append('model gave no answer for conversation ' + cid)
                stuck = state.get('stuck') if isinstance(state.get('stuck'), dict) and state['stuck'].get('conversation') == cid else \
                    {'conversation': cid, 'since': _stamp(now), 'tries': 0}
                stuck['tries'] += 1
                report['stuck'] = stuck
                if not dry:
                    _write_json(life / 'learn-state.json', state | {'stuck': stuck})
                break
            report['cost'] += res.get('cost') or 0
            entry['model_items'] = len(items)
            entry['kept'], entry['dropped'] = check(items, conv, bookmark, LD.live(life, known, now), now, life)
            LD.after_check(life, entry, items, conv, bookmark, known, now, dry, complete)   # the judge uses the same model door
            report['calls'] += (entry.get('judge') or {}).get('calls', 0)

        try:
            # Order matters if this is cut short: the ledger, the rule and the outbox each refuse a second copy, so a
            # rerun repeats nothing. The learned line and the bookmark go last.
            by_id = {m['id']: m for m in conv}
            n0 = sum(1 for r in known if r.get('conversation') == cid)
            entry['kept'] = [{'id': '%s:%d' % (cid, n0 + i + 1)} | row for i, row in enumerate(entry['kept'])]
            for row in entry['kept']:
                if row['kind'] == 'promise':
                    entry['effects'].append(add_life_item(life, row, now, dry, jobs) | {'for': row['text']})
                elif row['kind'] == 'correction':
                    entry['effects'].append(LD.rule(add_rule, life, row, now, dry))
                elif row['_durable']:
                    entry['effects'].append(queue_fact(life, row, own_words(by_id[row['message_id']]['text']), dry) | {'for': row['text']})
                else:
                    entry['effects'].append({'effect': 'fact for memory', 'status': 'passing matter, kept out of long-term memory', 'for': row['text']})
            # A fact that only matters for now ("Jordan is picking her up") is written down marked `passing`, so nothing
            # later treats it as a lasting fact about a person (it would otherwise land on that person's page).
            rows = [{k: v for k, v in row.items() if not k.startswith('_')} | ({'passing': True} if row['kind'] == 'fact' and not row['_durable'] else {})
                    for row in entry['kept']]
            if not dry:
                __import__('claims').learned(life, rows)   # claims.py (switch claims-on): each new line says who said it; a corrected one is followed through. Without the switch: untouched
                _append_lines(life / 'learned.jsonl', rows)
                state = {k: v for k, v in state.items() if k != 'stuck'} | {'last_message_id': conv[-1]['id'], 'at': _stamp(now)}
                _write_json(life / 'learn-state.json', state)
        except (OSError, sqlite3.Error) as e:
            # A write that failed (a locked ledger, a full disk): bookmark stays, the whole conversation is retried.
            report['errors'].append('could not save conversation %s: %s' % (cid, str(e)[:120]))
            entry['kept'] = [{k: v for k, v in row.items() if not k.startswith('_')} for row in entry['kept']]
            report['results'].append(entry | {'saved': False})
            break
        entry['kept'] = rows
        known += rows
        bookmark = conv[-1]['id']
        report['learned'] += len(rows)
        for d in entry['dropped']:
            report['dropped'][d['why'].split(' (')[0]] = report['dropped'].get(d['why'].split(' (')[0], 0) + 1
        report['results'].append(entry)
    if report['learned'] and not dry:
        try:        # ladder-on: a rule this run's own lines replaced leaves the owner's rules at once (switch off: nothing)
            later = LD.tidy_rules(life, now, expired=False)
        except Exception as e:
            later = {'error': f'{type(e).__name__}: {str(e)[:120]}'}
        if later:
            report['ladder_after'] = later
    report['bookmark_after'] = bookmark
    return report


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)

    def opt(name, default=None):
        return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default

    life = opt('--life-home', os.environ.get('LIFE_HOME'))
    if not life:
        print('Give --life-home <dir> or set LIFE_HOME.')
        return 2
    if '--seed' in a:
        print(json.dumps({'last_message_id': seed(life)}))
        return 0
    report = run(life_home=life, dry='--dry' in a, max_conversations=int(opt('--max', 2)), hours=float(opt('--hours', WINDOW_HOURS)))
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if opt('--out'):
        Path(opt('--out')).write_text(text)
        print(json.dumps({k: report.get(k) for k in ('dry', 'calls', 'learned', 'dropped', 'waiting', 'left_for_next_run', 'errors')}, default=str))
    else:
        print(text)
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())

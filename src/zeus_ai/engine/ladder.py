"""ladder: what Zeus learns about the owner stops piling up and contradicting itself.

The quiet-moment pass (quietpass.py) on its own only ever ADDS: a corrected fact would sit beside the wrong one, "I'm
in Chicago this week" would stay true forever, and the owner's standing rules would be shown as "the newest 12" with
no way for one to replace another. This file adds the missing half. Nothing here calls a model.

How sure Zeus is about a thing is WORKED OUT each time from what is stored, never stored as a label:
  mentioned once   the owner said it once, in passing
  learned          the owner said the same thing again in a different conversation, or on another day (or Zeus
                   picked it up from their mail as a habit). Twice in one sitting is one source: "like I said" ten
                   minutes later is still one time they told Zeus
  confirmed        the owner told Zeus to remember it, or said it as a rule or as a correction

What may change what:
- A correction saves the new thing FIRST and the old one is let go with the reason and a pointer to what replaced
  it. For facts both happen in one written line: the new line in learned.jsonl carries `replaces`, and "that old
  line is let go" is read off it, so there is no moment with only half done. For rules both happen in one swap of
  preferences.json. Nothing is ever deleted: a let-go rule moves to `rules_archive`, a let-go fact stays in its file.
- The model only proposes ("this replaces entry X"). Code decides: X must be something known now; a confirmed X is
  only replaced by the owner's own explicit correction; a thing that holds "for a while" never replaces a lasting one.
- A thing Zeus only inferred (a habit from mail, a fact heard once) is replaced by the owner's explicit correction
  whatever the kind: a fact can replace a mail-guess rule, and a correction can replace a heard fact. Mail guesses
  live only as rules, so without this a correction could never reach them. A fact that replaces a rule does it in its
  own written line, like a fact replacing a fact: from that line on the rule is no longer one of the owner's rules
  anywhere, and the sweep then moves it to `rules_archive` with the link.
- A correction with a vague "for a while" ("I'm off caffeine for a while") and none of the words that make a rule
  ("always", "never", "from now on", "stop", "don't" ...) is kept as a fact mentioned once, not a standing rule.
- Background learning can never let go of a confirmed thing any other way.
- A thing can carry a last day ("this week", "until Friday"). The model copies the owner's words; enddate.py counts
  the day. After that day it is let go as expired.
- The meaning of each item (the same as a known one, a new value of one, a rule or a fact, the owner's promise or an
  errand, a maybe, the day an end is counted from) is decided by one model call in ladderjudge.py; code checks the
  facts and applies the steps below. A correction that fails a check is held in ladder.json (`held`), never silently
  dropped. The word rules in this file are its fallback and its cross-checks.
- A fact mentioned once and not said again for 60 days is let go as stale.
  Stale only means faded: it leaves what the model and the prompts are shown, but its line stays in learned.jsonl and
  ladder.json's let_go names it. When the owner later says it again or corrects it, code still finds it and brings it
  back (see after_check); one never mentioned again stays faded.

Files (all in the life folder; every line the quiet pass writes or reads keeps its shape, fields are only added):
  learned.jsonl       new lines may also carry: origin ('stated' | 'heard'), expires, until_words, replaces, again
  preferences.json    a rule may also carry: id, expires, until_words, replaces. New list: rules_archive
  ladder.json         this file's own notes: later messages that said a known fact again, and what was let go and why

Switch: a file named ladder-on in the life folder. Without it every function here hands back what it was given, the
quiet pass writes and asks as it would on its own, and prefs.rules_text is the plain "newest 12".

Not done here: the people pages (people.py) keep what was already folded into them, and a fact waiting in
facts-outbox.jsonl for the shared memory is not pulled back when it is replaced (use `still_true` before writing one).
Usage: python3 -I ladder.py --life-home <dir> [--sweep] [--dry]
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import settings

OWNER = settings.OWNER_FIRST

SWITCH = 'ladder-on'
FILE = 'ladder.json'
STALE_DAYS = 60            # a thing heard once and never again is let go after this long
MAX_REPLACES = 3           # one sentence of the owner's corrects one thing, rarely two
ARCHIVE_KEEP = 200         # let-go rules kept for the record; the oldest fall off after this many
SHOW_LEARNED = 30          # how many known things the quiet pass is shown (same as before)
SHOW_RULES = 20
ADOPTED = 'Learned from his mail'      # the label reflect.py writes on a habit Zeus picked up by itself (shared marker): never "confirmed"
SITTING_HOURS = 2          # said again sooner than this after the first time, it is still the same sitting (one source)
CONFIRMED, LEARNED, ONCE = 'confirmed', 'learned', 'mentioned once'

# The owner's own words asking Zeus to hold on to something. "Remember to call Jordan" is a job, not this.
REMEMBER = re.compile(r"(?<!do you )(?<!don't )(?<!dont )(?<!can't )(?<!cant )(?<!if i )(?<!as i )(?<!\bi )\bremember\b(?!\s+(?:to|when|if|how|what|who|where|why)\b)"
                      r"|\b(?:don'?t|do not) forget\b(?!\s+to\b)|\bkeep in mind\b|\bmake a note\b|\bnote that\b|\bfor (?:the record|future reference)\b"
                      r"|\bwrite (?:this|that|it) down\b|\bsave (?:this|that)\b|\bfrom now on\b|\bgoing forward\b", re.I)
# The owner's words saying something is different now. The least code asks for before a confirmed thing may be replaced.
CORRECTS = re.compile(r"\b(?:no|nope|not|isn'?t|wasn'?t|aren'?t|actually|instead|correction|i meant|scratch that|changed?|changes|moved|no longer|"
                      r"not anymore|any ?more|now it'?s|is now|are now|it'?s now|rather than|wrong|updated?|new|stop)\b", re.I)
# Words for "for some time" that give no end: a state of the owner's for now, not a standing rule.
VAGUE = re.compile(r"\b(?:for a (?:while|bit|little while)|for now|for the time being|for the moment|lately|these days)\b", re.I)
# An "if" plus a hedge in the same sentence is a maybe, not something true about the owner.
HEDGED = re.compile(r"\bif\b[^.!?]{0,120}\b(?:probably|maybe|might|would|could|i'?d|id)\b|\b(?:probably|maybe|might)\b[^.!?]{0,120}\bif\b", re.I)
RULE_WORDS = re.compile(r"\b(?:always|never|from now on|going forward|every time|each time|whenever|make sure|stop|quit|"
                        r"don'?t|do not|dont|no more)\b", re.I)     # the words that make it a rule for Zeus, even for a while
# A question from the owner may still ask Zeus to hold on to something: these words say so ("can you remind me
# Friday?"). Without them, a fact or a date inside the question is never kept.
KEEP_ASK = re.compile(r"\bremind(?:er)?\b|\bforget\b|\bkeep (?:an eye|track|tabs)\b|\bfollow up\b|\b(?:on|to) my list\b", re.I)
# Words that say something is different now. A sentence carrying one is a change, never "the same thing again".
CHANGED = re.compile(r"\b(?:actually|instead|correction|i meant|scratch that|changed?|changes|moved|no longer|not anymore|any ?more|"
                     r"now it'?s|is now|are now|it'?s now|rather than|wrong|updated?|new|used to|left|quit)\b", re.I)
NEG = re.compile(r"\b(?:no|not|never|nobody|nothing|none|don'?t|dont|doesn'?t|isn'?t|aren'?t|wasn'?t|won'?t|can'?t|cant)\b", re.I)
# Words that carry no subject of their own, so two sentences sharing them say nothing about being the same thing.
STOP = set("""a an the and or but so to of in on at by for with from up out about into over after before than then that thats
this these those it its is are was were be been being am do does did done have has had having i im me my mine we our ours us you
your he hes his him she shes her hers they them their there here what when where who whom which why how all any both each few
more most other some such no nor not only own same too very can will would could should just now also as if because while until
till get got gets let lets make made go goes going like said say says one ones zeus ok okay yes yeah ya yep fyi lol
really thing things guy guys anything something everything nothing always never usually please again remember note may might
keep mind between near every each time times day days week weeks""".split()) | {OWNER.lower(), OWNER.lower() + 's'}
DAYS = {'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'}
MONTHS = {'january', 'february', 'march', 'april', 'june', 'july', 'august', 'september', 'october', 'november', 'december'}
CLOCK_WORDS = {'noon': '12', 'midnight': '0'}
# A maybe needs no "if": "might skip it this year, idk". Rules,
# promises and corrections are never touched by this; a hedge inside a reason clause ("probably because ...") is not one.
MAYBE = re.compile(r"\b(?:might|maybe|perhaps|possibly|probably|undecided|on the fence|torn between|back and forth|we'?ll see)\b"
                   r"|\bnot sure\b(?!\s+(?:why|what|how|who|where|when|which)\b)"
                   r"|\b(?:idk|dunno|i (?:don'?t|dont|do not) know)\b(?!\s+(?:why|what|how|who|where|when|which|her|him|them|his|my|the|that)\b)"
                   r"|\b(?:thinking|thought) (?:about|of) \w+ing\b|\bconsidering \w+ing\b", re.I)
REASON = re.compile(r"\b(?:(?:probably|maybe|likely|prob)\s+)?(?:because|cause|cuz|coz|cos|bc)\b[^,;]*"
                    r"|\b(?:which is|that'?s|thats)?\s*(?:probably|maybe|likely)\s+(?:why|cause|because)\b[^,;]*", re.I)
# The owner's own word that they will do something: "ill drop the check off monday". Not "I'm ill", not "I'll be in Denver".
COMMIT = re.compile(r"(?<!\bim )(?<!\bi'm )(?<!\bfeel )(?<!\bfeeling )(?<!\bwas )(?<!\bbeen )(?<!\bgot )(?<!\bif )(?<!\bwhen )"
                    r"\b(?:i'?ll|i will|i'?m gonna|im gonna|imma|i need to|i have to|i gotta|i'?ve got to|ive got to|i promised(?: to)?|"
                    r"i still (?:need|have) to)\s+(?!(?:be|go|head|fly|travel|stay|visit|see|meet|have|try|think|probably|maybe|prob|"
                    r"not|never|always|just be)\b)[a-z]", re.I)
SOON = re.compile(r"\b(?:next|this) (?:week|weekend)\b", re.I)
WORD_NUMBERS = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
                'eleven': 11, 'twelve': 12, 'fifteen': 15, 'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60,
                'seventy': 70, 'eighty': 80, 'ninety': 90, 'hundred': 100}
SCALE = {'k': 1000, 'grand': 1000, 'thousand': 1000, 'mil': 1000000, 'million': 1000000, 'hundred': 100}
DROPPED_KEEP = 50          # corrections code had to drop, kept in ladder.json for a person to look at
# The owner's words that a thing of theirs is different now, on top of CHANGED:
# "im an 11 now", "she traded the rav4", "switched to the night shift". Only read against a fact that faded.
NOW_WORDS = re.compile(r"\bnow\b|\b(?:traded|switched|swapped|upgraded|downsized|replaced)\b|\bgot (?:a|an) new\b", re.I)
REVIVED_KEEP = 200         # faded facts the owner brought back, kept in ladder.json for the record
# A sentence about the owner's plan ("<owner> plans to ...") turned into the reminder job.
PLAN_LEAD = re.compile(r"^(?:" + re.escape(OWNER) + r"|The owner) (?:plans|intends|is planning|is going|wants|needs|has|promised|"
                       r"said (?:he|she|they) would|said (?:he|she|they) will|will)\s+(?:to\s+)?", re.I)

RULE = """

Keeping what the owner said true over time (this comes on top of the rules above):
- already_learned now shows each kept thing with an id and how firmly it is known. owners_rules shows the owner's
  standing rules, each with an id.
- Said again: when a NEW line from the owner says again something that is already in already_learned, return it once
  more as a fact, with text copied exactly from that entry and the owner's new words as the quote. This is the one
  exception to "do not repeat". It is how a thing mentioned once becomes a thing that is known. Never do this for
  owners_rules.
- Changed or corrected: when a new line from the owner explicitly corrects or changes one of those entries (the same
  person or thing, the same kind of statement, and the words say it is different now: "no, it's the 20th", "she moved
  to Austin", "actually stop doing that"), return the new fact or rule and put the id of each entry it replaces in
  replaces. A separate event, or an exception for a while ("skip it this week"), does not replace a lasting entry:
  leave replaces empty. When you are not sure, leave replaces empty.
- For a while only: when the owner's own words say how long something holds ("this week", "until Friday", "for two
  weeks", "through November 20"), copy exactly those words into until_words. Do not work out the date; code does
  that. null when no end was given.
- The same thing in new words: when something you return says the same thing as an entry in already_learned (the
  same person or thing and the same statement, perhaps said more firmly, as a rule, or with "remember this"), put
  that entry's id in same_as. Not for a change (that is replaces), and not for a different fact about the same person
  or thing. null otherwise."""
EXTRA_SCHEMA = {
    'replaces': {'type': 'array', 'items': {'type': 'string'}, 'description': 'ids from already_learned or owners_rules that this explicitly corrects or changes; usually empty'},
    'until_words': {'type': ['string', 'null'], 'description': "the owner's exact words for how long it holds (\"this week\"), or null"},
    'same_as': {'type': ['string', 'null'], 'description': 'id from already_learned that this says again in new words, or null'},
}


# ---- small helpers -------------------------------------------------------------------------------------------------

def on(life) -> bool:
    return life is not None   # always on when there is a life folder


def _q():
    """quietpass, for the helpers that must stay the same in both files (what counts as the same thing, how a file
    is swapped in whole). Looked up late: quietpass calls into this file too."""
    import quietpass
    return quietpass


def _today(now) -> date:
    return datetime.fromtimestamp(float(now), _q().PACIFIC).date()


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _notes(life) -> dict:
    d = _q()._read_json(Path(life) / FILE, {})
    d = d if isinstance(d, dict) else {}
    d['again'] = d['again'] if isinstance(d.get('again'), dict) else {}
    d['let_go'] = d['let_go'] if isinstance(d.get('let_go'), dict) else {}
    return d


def rule_id(rule: dict) -> str:
    """A rule's own short name. Stored on rules written from now on; worked out the same way for older ones."""
    return str(rule.get('id') or 'rule-' + hashlib.sha1(f"{rule.get('text')}|{rule.get('message_id')}".encode()).hexdigest()[:8])


# ---- how sure, and what has been let go -------------------------------------------------------------------------------

def _heard(row: dict, notes: dict) -> list:
    """Every time the owner said it: the line itself, repeats in the same sweep, and later repeats noted in ladder.json."""
    first = {'message_id': row.get('message_id'), 'said_at': row.get('said_at') or row.get('at') or 0, 'stated': row.get('origin') == 'stated',
             'conversation': row.get('conversation'), 'session': row.get('session')}
    later = [a for a in (row.get('again') or []) + (notes['again'].get(str(row.get('id'))) or []) if isinstance(a, dict)]
    return [first] + later


def sources(heard: list) -> int:
    """How many separate times the owner told Zeus: one conversation is one source, however often it was said in it.
    What a conversation is comes from the chat store when it says: every message there
    belongs to a chat session, and the store starts a fresh one each morning (and after a day of quiet), so the same
    session within a day is the same conversation, even with hours between ("like I said" at 11:45 after 7:10 is one
    source). Only when a line carries no session (older lines) does the time rule decide: a new
    source is in another conversation AND either on another day (the owner's clock) or at least SITTING_HOURS after the start
    of the last source. Every message id stays stored as evidence. A repeat with no time on it (an older line) counts
    when its message is a different one, as before."""
    count, start, convs, ids, sess = 0, None, set(), set(), None
    for h in sorted(heard, key=lambda h: float(h.get('said_at') or 0)):
        at, conv, mid, s = float(h.get('said_at') or 0), h.get('conversation'), str(h.get('message_id')), h.get('session')
        if start is not None:
            if mid in ids or (conv and conv in convs):
                same = True
            elif s and sess:            # the chat store names the conversation
                same = s == sess and (not (at and start) or at - start < 86400)
            elif at and start:
                same = _today(at) == _today(start) and at - start < SITTING_HOURS * 3600
            else:
                same = False
            if same:
                ids.add(mid)
                if conv:
                    convs.add(conv)
                sess = sess or s
                continue
        count, start, convs, ids, sess = count + 1, at, {conv} if conv else set(), {mid}, s
    return count


def standing(row: dict, notes: dict | None = None) -> str:
    """confirmed | learned | mentioned once, worked out from what is stored."""
    heard = _heard(row, notes or {'again': {}})
    if row.get('kind') == 'correction' or any(h.get('stated') for h in heard):
        return CONFIRMED
    return LEARNED if sources(heard) >= 2 else ONCE


def facts(life, now=None, rows=None) -> list:
    """Every fact line of learned.jsonl, oldest first, each with `standing` and `let_go` (None while it still holds,
    else {'reason': 'replaced' | 'expired' | 'stale', 'by': the line that replaced it}). Read off the lines
    themselves, so it is right even when a sweep has not written its note yet."""
    life, now = Path(life), float(now if now is not None else time.time())
    rows = _q()._read_lines(life / 'learned.jsonl') if rows is None else rows
    notes, today = _notes(life), _today(now)
    replaced = {}
    for r in rows:
        for old in r.get('replaces') or [] if isinstance(r.get('replaces'), list) else []:
            replaced.setdefault(str(old), r.get('id'))
    out = []
    for r in rows:
        if r.get('kind') != 'fact' or not r.get('id'):
            continue
        rid, level = str(r['id']), standing(r, notes)
        gone = notes['let_go'].get(rid) if isinstance(notes['let_go'].get(rid), dict) else None
        if gone is None and rid in replaced:
            gone = {'reason': 'replaced', 'by': replaced[rid]}
        if gone is None and _day(r.get('expires')) and _day(r['expires']) < today:
            gone = {'reason': 'expired', 'by': None}
        if gone is None and level == ONCE:
            last = max(float(h.get('said_at') or 0) for h in _heard(r, notes))
            if last and now - last > STALE_DAYS * 86400:
                gone = {'reason': 'stale', 'by': None}
        out.append({**r, 'standing': level, 'let_go': gone})
    return out


def live(life, known: list, now) -> list:
    """Hook for the quiet pass: the lines a new item is compared with. A fact that was let go no longer blocks the
    owner saying it again ("I'm in Chicago this week", next month). Switch off: the same list, untouched."""
    if not on(life):
        return known
    try:
        gone = {str(f['id']) for f in facts(life, now, rows=known) if f['let_go']}
    except Exception:
        return known
    return [r for r in known if str(r.get('id')) not in gone] if gone else known


def still_true(life, rows: list, now=None) -> list:
    """For a later memory writer: the given lines (from facts-outbox.jsonl, say) minus the ones let go since."""
    gone = {str(f['id']) for f in facts(life, now) if f['let_go']}
    return [r for r in rows if str(r.get('id')) not in gone]


def replaced_rules(rows: list) -> dict:
    """{rule id: id of the fact line that replaced it}. A fact from the owner that corrects a mail guess (kept as a rule) does
    it in its own line of learned.jsonl, so from that one write on the rule is let go. (A correction that replaces a
    rule does it in one swap of preferences.json instead, in rule() below.)"""
    out = {}
    for r in rows:
        if r.get('kind') == 'fact' and r.get('id') and isinstance(r.get('replaces'), list):
            for old in r['replaces']:
                out.setdefault(str(old), r['id'])
    return out


def rules(d: dict, now, gone=None) -> list:
    """The owner's live standing rules from a loaded preferences file, oldest first, each with `id` and `standing`. A rule
    past its last day, or one a fact line replaced (`gone`, from replaced_rules), is left out even before a sweep has
    moved it to the archive."""
    today = _today(now)
    gone = gone or {}
    out = []
    for r in d.get('rules') if isinstance(d.get('rules'), list) else []:
        if not (isinstance(r, dict) and r.get('text')):
            continue
        if _day(r.get('expires')) and _day(r['expires']) < today:
            continue
        if rule_id(r) in gone:
            continue
        mail = str(r['text']).startswith(ADOPTED)
        level = LEARNED if mail else CONFIRMED if r.get('quote') and r.get('message_id') is not None else ONCE
        out.append({**r, 'id': rule_id(r), 'standing': level})
    return out


# ---- hooks in the quiet pass ----------------------------------------------------------------------------------------

def ask(life, system: str, prompt: str, schema: dict, now) -> tuple:
    """What the quiet pass sends to the model. Switch off, or any trouble: exactly what it was given."""
    if not on(life):
        return system, prompt, schema
    try:
        Q = _q()
        d = json.loads(prompt)
        rows = Q._read_lines(Path(life) / 'learned.jsonl')
        by_id = {str(f['id']): f for f in facts(life, now, rows=rows)}
        shown = []
        for r in rows:
            f = by_id.get(str(r.get('id')))
            if f is not None and f['let_go']:
                continue
            shown.append({'id': str(r.get('id')), 'kind': r.get('kind'), 'text': r.get('text')}
                         | ({'known': f['standing']} if f is not None else {})
                         | ({'holds_until': r['expires']} if r.get('expires') else {}))
        d['already_learned'] = shown[-SHOW_LEARNED:]
        prefs = Q._read_json(Path(life) / 'preferences.json', {})
        d['owners_rules'] = [{'id': r['id'], 'text': r['text']} for r in rules(prefs if isinstance(prefs, dict) else {}, now, replaced_rules(rows))][-SHOW_RULES:]
        new_schema = copy.deepcopy(schema)
        new_schema['properties']['items']['items']['properties'].update(copy.deepcopy(EXTRA_SCHEMA))
        return system + RULE, json.dumps(d, ensure_ascii=False, indent=1), new_schema
    except Exception:
        return system, prompt, schema


def _item_for(row: dict, items: list):
    Q = _q()
    for it in items:
        if isinstance(it, dict) and Q._plain(it.get('text')) == row.get('text') and str(it.get('quote') or '').strip()[:600] == row.get('quote'):
            return it
    return {}


def _covered(quote, said) -> list:
    """The sentences of the owner's message that the quote spans (flat text), or [] when the quote is not in it word for word."""
    Q = _q()
    t, q = Q._flat(said), Q._flat(quote)
    i = t.find(q) if q else -1
    if i < 0:
        return []
    j, start, out = i + len(q), 0, []
    for m in list(re.finditer(r'[.!?]+(?=\s|$)', t)) + [None]:
        end = m.end() if m else len(t)
        if start < j and end > i and t[start:end].strip():
            out.append(t[start:end].strip())
        start = end
        if start >= len(t):
            break
    return out


def in_question(quote, said) -> bool:
    """True when every sentence the quote spans is a question from the owner ("since I'm picking her up on the 26th,
    can you check when her flight lands?") and none asks Zeus to remind, remember or keep track. What sits inside a
    question is the premise of something asked, not something the owner told Zeus to keep."""
    parts = _covered(quote, said)
    return bool(parts) and all(p.endswith('?') for p in parts) and not any(REMEMBER.search(p) or KEEP_ASK.search(p) for p in parts)


def _norm(tok: str) -> str:
    w = re.sub(r"'s$|'$", '', tok.lower().replace('’', "'")).replace("'", '')
    w = CLOCK_WORDS.get(w, w)
    return w[:-1] if len(w) > 3 and w.endswith('s') and not w.endswith('ss') else w


def _terms(text, names: bool = True) -> tuple:
    """-> (names, words, values) of one sentence. names: capitalised words past the start of a sentence (Taylor,
    Jordan's); values: numbers, codes, days and months (noon is 12); words: everything else that carries meaning."""
    out_n, out_w, out_v, first = set(), set(), set(), True
    for m in re.finditer(r"[.!?]|[A-Za-z0-9][A-Za-z0-9'’]*", str(text or '')):
        tok = m.group()
        if tok in '.!?':
            first = True
            continue
        w = _norm(tok)
        if w and tok.lower().replace('’', "'").replace("'", '') not in STOP and w not in STOP:
            if any(c.isdigit() for c in w) or w in DAYS or w in MONTHS:
                out_v.add(w)
            elif names and tok[0].isupper() and not first:
                out_n.add(w)
            else:
                out_w.add(w)
        first = False
    return out_n, out_w, out_v


def _tokens(text) -> list:
    """The meaning words of some text, in order, as _terms reads them."""
    return [w for w in (_norm(t) for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9'’]*", str(text or '')))
            if w and w not in STOP and w.replace("'", '') not in STOP]


def _only_the_subject(new_text, known_text, shared: set, new_words: set) -> bool:
    """True when all two texts share is how they open ("Alex next door ..."), and the known one goes on to
    say something the new one does not ("... has the red pickup" vs "... is a retired electrician"). That is two facts
    about one person, not the same fact twice. A name at the start of a sentence is not read as a name, so "Alex next
    door" would otherwise count as three shared words."""
    a, b = _tokens(new_text), _tokens(known_text)
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    opening = set(a[:n])
    return bool(opening) and shared <= opening and bool(set(b[n:]) - opening - new_words)


def restates(row: dict, said: str, known_facts: dict, hint=None, folded=()):
    """The id of the known fact this new item only says again, or None. Code decides; the
    model's same_as is a hint that lowers the bar, never the decision. Never a link when:
      - the new item names a person, place, number, day or month the known one does not have (a new detail or a change);
      - a sentence of the owner's about it, or the new text, has a change word ("moved", "actually", "instead", "new" ...);
      - a fact and the known fact differ in a "not" (the same words, the opposite statement).
    Then what they share counts: the same names (at most 1 point, so two facts about one person stay two), the same
    content words (1 each), the same numbers or days (at most 2). A link needs 3 points with at least one shared content
    word, or the model's own same_as on that entry plus 1 point. A tie between two known facts links nothing.
    Never a link either when most meaning words of the new text are not in the known fact at all ("Alex next door is
    a retired electrician" shares "Alex next door" with "Alex next door has the red pickup", but what it says is new),
    unless the same message already says that known fact again (`folded`): then a detail from it rides on the repeat
    ("salmon, NOT the chicken one, chicken wrecks my stomach")."""
    Q = _q()
    parts = _covered(row.get('quote'), said) or [Q._flat(row.get('quote'))]
    nn, nw, nv = _terms(row.get('text'))
    _, qw, _ = _terms(' '.join(parts), names=False)
    new_words = nn | nw | qw
    scored = []
    for fid, f in known_facts.items():
        kn, kw, kv = _terms(f.get('text'))
        _, kqw, kqv = _terms(f.get('quote'), names=False)
        known_words = kn | kw | kqw
        if nn - known_words or nv - (kv | kqv):
            continue
        about = [p for p in parts if (_terms(p, names=False)[1] & known_words)]
        if CHANGED.search(str(row.get('text') or '')) or any(CHANGED.search(p) for p in about):
            continue
        if row.get('kind') == 'fact' and bool(NEG.search(str(row.get('text') or ''))) != bool(NEG.search(str(f.get('text') or ''))):
            continue
        shared = new_words & known_words
        names_shared, plain = shared & (nn | kn), shared - nn - kn
        if len(nw - known_words) > len(nw & known_words) and str(fid) not in folded:   # mostly new words
            continue
        if str(hint) != str(fid) and str(fid) not in folded and _only_the_subject(row.get('text'), f.get('text'), shared, new_words):
            continue                                                                    # one person, two facts
        points = min(1, len(names_shared)) + len(plain) + min(2, len(nv & (kv | kqv)))
        if (points >= 3 and plain) or (hint is not None and str(hint) == str(fid) and points >= 1):
            scored.append((2 if hint is not None and str(hint) == str(fid) else 1, points, str(fid)))
    scored.sort(reverse=True)
    if not scored or (len(scored) > 1 and scored[0][:2] == scored[1][:2]):
        return None
    return scored[0][2]


def _values(vs) -> set:
    """Numbers said in the plural are the same number: "the last 10s" is 10."""
    return {re.sub(r'(?<=\d)s$', '', v) for v in vs}


def corrects(row: dict, said: str, faded: dict):
    """The id of the faded fact this new item corrects, or None. A faded fact is not shown to
    the model, so the model cannot name it in replaces; code finds it. Narrow on purpose (a wrong link would put a fact
    the owner never took back into the archive), and every one of these is needed:
      - the owner's words, or the new text, say it is different now (a change word, "now", "traded", "switched" ...);
      - the old fact has a value of its own (a number, a code, a day, a name inside the sentence: 10, RAV4, Acme),
        and the new item has a value the old one does not (11, Highlander);
      - the same thing is talked about: when the owner names the old value ("the last 10s pinched", "traded the rav4"), at
        least one other meaning word is shared ("wear", "drive"); when they do not, at least two are shared and at most
        one meaning word of the old fact is missing from the new (the one the new value took the place of).
    A fact about a person with no value of its own ("Alex next door has the red pickup") is never corrected this way:
    code cannot tell a new fact about Alex from a change. A tie between two faded facts links nothing."""
    Q = _q()
    his = ' '.join(_covered(row.get('quote'), said) or [Q._flat(row.get('quote'))])
    text = str(row.get('text') or '')
    if not any(p.search(text) or p.search(his) for p in (CHANGED, NOW_WORDS)):
        return None
    nn, nw, nv = _terms(text)
    _, qw, qv = _terms(his, names=False)
    new_values = nn | _values(nv)
    new_all = nn | nw | _values(nv) | qw | _values(qv)
    found = []
    for fid, f in faded.items():
        kn, kw, kv = _terms(f.get('text'))
        _, kqw, kqv = _terms(f.get('quote'), names=False)
        old_values = kn | _values(kv) | _values(kqv)
        old_words = (kw | kqw) - old_values
        if not old_values or not (new_values - old_values - old_words):
            continue
        shared = old_words & new_all
        if old_values & new_all:
            ok = len(shared) >= 1
        else:
            ok = len(shared) >= 2 and len((kw - old_values) - new_all) <= 1
        if ok:
            found.append((len(shared), str(fid)))
    found.sort(reverse=True)
    if not found or (len(found) > 1 and found[0][0] == found[1][0]):
        return None
    return found[0][1]


def _content(text) -> set:
    n, w, v = _terms(text)
    return n | w | v


def same(a: dict, b: dict) -> bool:
    """The quiet pass's own test for "the same thing learned twice", but two items quoting the same words of the
    owner's are the same thing only when they also say the same thing. One sentence often holds two facts ("Alex moved
    into Maple Court, also his doctor says he has diabetes"), and the model quotes the whole sentence for
    both. Same text, or a shared quote with at least half the meaning words of the shorter text shared: the same."""
    Q = _q()
    if not Q._same(a, b):
        return False
    if Q._key(a.get('text')) == Q._key(b.get('text')):
        return True
    ta, tb = _content(a.get('text')), _content(b.get('text'))
    return not (ta and tb) or len(ta & tb) * 2 >= min(len(ta), len(tb))


def same_test(life, old):
    """Hook for the quiet pass: which "the same thing" test to use. Switch off (or no life folder): its own."""
    return same if life is not None and on(life) else old


def _amounts(text) -> list:
    """[(as written, value)] for every number in some text, read the way people write money and counts: 5k, 1.5k,
    $5,000, 5 grand, five thousand."""
    s = str(text or '').lower()
    out = []
    for m in re.finditer(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?:\s*(k|grand|thousand|mil|million|hundred)\b)?", s):
        out.append((m.group(0).strip(), round(float(m.group(1).replace(',', '')) * SCALE.get(m.group(2) or '', 1), 4)))
    for m in re.finditer(r"\b(" + '|'.join(WORD_NUMBERS) + r")\s+(grand|thousand|million|hundred)\b", s):
        out.append((m.group(0), float(WORD_NUMBERS[m.group(1)] * SCALE[m.group(2)])))
    return out


def number_test(life, old):
    """Hook for the quiet pass: its check for numbers the owner never said, reading money the way people write it (the
    owner typed "5k", the rule says "$5,000"; a plain digit check sees "000" as a number never said and throws the
    correction away). Only ever kinder than the quiet pass's own check: what it lets through still goes through.
    Switch off: its own."""
    if life is None or not on(life):
        return old

    def loose(text, owners_text, due, said_at):
        first = old(text, owners_text, due, said_at)
        if not first:
            return first
        said = datetime.fromtimestamp(said_at, _q().PACIFIC)
        ok = {v for _, v in _amounts(owners_text)} | {float(n) for n in re.findall(r'\d+', owners_text)} | {float(said.year), float(said.month), float(said.day)}
        if due:
            ok |= {float(n) for n in re.findall(r'\d+', str(due)[:10])}
        return [w for w, v in _amounts(text) if v not in ok]
    return loose


def _clauses(text) -> list:
    """Sentences of the owner's, each cut into its clauses (at commas and "but" / "so"): [[clause, ...], ...]."""
    out = []
    for s in re.split(r'[.!?\n]+', str(text or '')):
        parts = [c.strip() for c in re.split(r'[,;]|\b(?:but|so|and then)\b', s) if c and c.strip()]
        if parts:
            out.append(parts)
    return out


def hedged(row: dict) -> bool:
    """True when what a kept fact rests on is only a maybe, with or without an "if": the
    clauses of the quote that carry the fact (the most meaning words shared with its text) all hedge ("might ...",
    "maybe", "probably", "not sure", "thinking about ...ing"), or sit in a sentence with a bare hedge of its own
    ("..., idk"). A hedge inside a reason clause does not count ("she skipped it, probably because of her allergy"), and
    a clause with rule words ("never", "always", "don't") is never a maybe."""
    want = _content(row.get('text'))
    scored = []
    for sentence in _clauses(row.get('quote')):
        bare = any(MAYBE.search(c) and not _content(MAYBE.sub(' ', c)) for c in sentence)
        for c in sentence:
            main = REASON.sub(' ', c)
            scored.append((len(want & _content(c)), bool(MAYBE.search(main)) or bare, bool(RULE_WORDS.search(c))))
    if not scored:
        return False
    top = max(s for s, _, _ in scored)
    core = [x for x in scored if x[0] == top] if top else scored
    return all(h for _, h, _ in core) and not any(r for _, _, r in core)


def _as_promise(row: dict, said: str, now):
    """A kept fact that is really the owner's promise to do something on a day ("ill drop the check off at the bank
    monday") -> (text, due) for it as a promise, else None. Code decides: in the sentence it was said, the owner's own
    "I'll / I'm gonna / I need to / I have to" with something to do (not "I'll be in Denver") and a day (a weekday,
    today, tomorrow, a date, next week). Due: the model's time when it falls on a day named, else that day at 9am."""
    import enddate as E
    Q = _q()
    said_day = _today(row.get('said_at') or now)
    for s in _covered(row.get('quote'), said) or [Q._flat(row.get('quote'))]:
        days = [d for _, d in E.named_days(s, said_day)]
        if not COMMIT.search(s) or HEDGED.search(s) or MAYBE.search(REASON.sub(' ', s)) or not (days or SOON.search(s)):
            continue
        due = row.get('due')
        if days and (_day(due) is None or _day(due) not in days):
            due = Q._due(days[0].isoformat())
        if not due:
            continue
        text = PLAN_LEAD.sub('Remind %s to ' % OWNER, str(row.get('text') or ''), count=1)
        return text, due
    return None


def after_check(life, entry: dict, items: list, conv: list, bookmark: int, known: list, now, dry: bool = False, complete=None) -> None:
    """Runs right after the quiet pass's own check, on what it kept and dropped for one conversation.
    Kept facts and corrections get: where they came from (stated or heard), a last day when the owner's own words give one,
    and the entries they replace when code allows it. A dropped "already learned" that the owner really said again in a new
    message is noted as a repeat, which is what lifts a thing from mentioned once to learned.
    Switch off: nothing. Any trouble here: the lines are written exactly as the quiet pass alone would, and the report
    says so. Given the quiet pass's model door (`complete`), what each item MEANS is decided by one focused model
    call and code only checks the facts (ladderjudge.py). Everything below this hook is then only the fallback, used
    when that call gives no usable answer."""
    if not on(life):
        return
    if complete is not None:
        import ladderjudge
        if ladderjudge.decide(life, entry, items, conv, bookmark, known, now, dry, complete):
            return
    Q = _q()
    import chats as C
    import enddate as E
    report = {'ends': [], 'replaced': [], 'refused': [], 'said_again': [], 'kept_as_fact': []}
    as_fact = []
    maybes = [k for k in entry.get('kept') or [] if k.get('kind') == 'fact' and (HEDGED.search(str(k.get('quote') or ''))
              and not RULE_WORDS.search(str(k.get('quote') or '')) or hedged(k))]   # a maybe is not a fact
    if maybes:
        entry['kept'] = [k for k in entry['kept'] if k not in maybes]
        entry.setdefault('dropped', []).extend({'why': 'only a maybe, not a fact', 'kind': k.get('kind'), 'text': k.get('text'),
                                                'quote': str(k.get('quote') or '')[:300], 'message_id': k.get('message_id')} for k in maybes)
        report['maybes'] = [k.get('text') for k in maybes]
    kept0, dropped0 = list(entry.get('kept') or []), list(entry.get('dropped') or [])   # put back on trouble
    promised = []                # (row, its fact text, its due) of facts turned into promises, put back on trouble
    try:
        his = [{**m, 'text': Q.own_words(m['text'])} for m in conv if m['who'] == 'owner']
        his = [m for m in his if m['text']]
        owners_new = [m for m in his if m['id'] > bookmark]
        text_of = {m['id']: m['text'] for m in his}
        session_of = {m['id']: m.get('session') for m in conv}
        asked = [k for k in entry.get('kept') or [] if k.get('kind') in ('fact', 'promise')
                 and in_question(k.get('quote'), text_of.get(k.get('message_id'), ''))]
        if asked:      # a fact or a date inside the owner's question is never kept, never a reminder
            entry['kept'] = [k for k in entry['kept'] if k not in asked]
            entry.setdefault('dropped', []).extend({'why': 'inside a question from the owner, not something they told Zeus', 'kind': k.get('kind'),
                                                    'text': k.get('text'), 'quote': str(k.get('quote') or '')[:300],
                                                    'message_id': k.get('message_id')} for k in asked)
            report['questions'] = [k.get('text') for k in asked]
        # a plan of the owner's own with a day is a promise with a due, so Zeus nudges them on the day
        promise_msgs = {k.get('message_id') for k in entry.get('kept') or [] if k.get('kind') == 'promise'}
        for row in entry.get('kept') or []:
            if row.get('kind') != 'fact' or row.get('message_id') in promise_msgs:
                continue
            got = _as_promise(row, text_of.get(row.get('message_id'), ''), now)
            if got:
                promised.append((row, row.get('text'), row.get('due')))
                row['kind'], (row['text'], row['due']) = 'promise', got
                report['as_promise'] = report.get('as_promise', []) + [row['text']]
        now = float(now)
        live_facts = {str(f['id']): f for f in facts(life, now, rows=known) if not f['let_go']}
        prefs = Q._read_json(Path(life) / 'preferences.json', {})
        live_rules = {r['id']: r for r in rules(prefs if isinstance(prefs, dict) else {}, now, replaced_rules(known))}
        cid = C.conv_id(conv) if conv else None

        for row in entry.get('kept') or []:
            if row.get('kind') not in ('fact', 'correction'):
                continue
            it = _item_for(row, items)
            said = text_of.get(row.get('message_id'), '')
            if session_of.get(row.get('message_id')):
                row['session'] = session_of[row['message_id']]       # names the conversation for sources()
            if row['kind'] == 'correction' and VAGUE.search(said) and not RULE_WORDS.search(said):
                # "I'm off caffeine for a while": a state of the owner's for now, said once, not a rule. It is a passing
                # matter (kept out of the shared memory) and, holding only for a while, it replaces nothing.
                as_fact.append((row, row.get('_durable')))
                row['kind'], row['_durable'] = 'fact', False
                it = {**it, 'replaces': []}
                report['kept_as_fact'].append(row['text'])
            explicit = row['kind'] == 'correction' or bool(CORRECTS.search(said))
            words = str(it.get('until_words') or '').strip()
            if words:
                if Q._flat(words) not in Q._flat(said):
                    report['refused'].append({'for': row['text'], 'why': "the words for how long are not the owner's"})
                else:
                    day = E.end_day(words, _today(row.get('said_at') or now), True, said)   # True: "till the 20th"; said: "10 days after thursday"
                    if day is None:
                        report['refused'].append({'for': row['text'], 'why': 'could not count a last day from "%s"' % words[:60]})
                    else:
                        row['expires'], row['until_words'] = day.isoformat(), words[:80]
                        report['ends'].append({'for': row['text'], 'last_day': row['expires']})
            own, other = (live_facts, live_rules) if row['kind'] == 'fact' else (live_rules, live_facts)
            allowed = []
            for target in [str(x) for x in (it.get('replaces') if isinstance(it.get('replaces'), list) else []) if isinstance(x, (str, int))][:MAX_REPLACES]:
                old, cross = own.get(target), False
                if old is None and target in other:
                    # Something Zeus only inferred may sit in the other store: a mail guess is always a rule.
                    old, cross = other[target], True
                why = None
                if old is None:
                    why = 'that entry is not something known now'
                elif row.get('expires'):
                    why = 'a thing that holds only for a while never replaces a lasting one'
                elif cross and old['standing'] == CONFIRMED:
                    why = 'the owner confirmed that one; only a correction of the same kind replaces it'
                elif cross and not explicit:
                    why = "only the owner's own explicit correction replaces something Zeus inferred"
                elif old['standing'] == CONFIRMED and not explicit:
                    why = "the owner confirmed that one; only their own explicit correction replaces it"
                if why:
                    report['refused'].append({'for': row['text'], 'would_replace': target, 'why': why})
                else:
                    allowed.append(target)
                    report['replaced'].append({'old': str(old.get('text'))[:200], 'old_id': target, 'by': row['text']})
            if allowed:
                row['replaces'] = allowed
            if row['kind'] == 'fact':
                # a fact that code let replace a known one is the owner's correction, and a correction is confirmed
                row['origin'] = 'stated' if REMEMBER.search(said) or allowed else 'heard'

        notes, changed = _notes(life), False
        # A new item that only says a known fact again is that fact, not a second one. A fact
        # moves the known one up (a new conversation: learned; "remember" or rule words: confirmed) and is not stored.
        # A rule takes the known fact's place with a link, so it still reaches the owner's rules; never a confirmed fact.
        folded = {}         # message id -> the known facts that message already says again (dropped as known)
        for d in entry.get('dropped') or []:
            if d.get('why') == 'already learned' and d.get('kind') == 'fact':
                probe = {'kind': 'fact', 'text': d.get('text'), 'quote': d.get('quote')}
                folded.setdefault(d.get('message_id'), set()).update(str(f['id']) for f in live_facts.values() if Q._same(probe, f))
        for row in [r for r in entry.get('kept') or [] if r.get('kind') in ('fact', 'correction')]:
            if row.get('replaces') or row.get('expires') or row in [a for a, _ in as_fact]:
                continue
            said = text_of.get(row.get('message_id'), '')
            if VAGUE.search(said):
                continue
            target = restates(row, said, live_facts, _item_for(row, items).get('same_as'), folded.get(row.get('message_id'), set()))
            if target is None:
                continue
            if row['kind'] == 'correction':
                if live_facts[target]['standing'] == CONFIRMED:
                    continue
                row['replaces'] = [target]
                report['replaced'].append({'old': str(live_facts[target].get('text'))[:200], 'old_id': target, 'by': row['text'], 'same_thing': True})
                continue
            spans = ' '.join(_covered(row.get('quote'), said)) or str(row.get('quote') or '')
            again = {'message_id': row['message_id'], 'said_at': row.get('said_at'), 'quote': str(row.get('quote') or '')[:300],
                     'conversation': cid, 'session': session_of.get(row['message_id'])} | \
                ({'stated': True} if REMEMBER.search(spans) or RULE_WORDS.search(spans) else {})
            if str(row['message_id']) not in {str(h.get('message_id')) for h in _heard(live_facts[target], notes)}:
                notes['again'].setdefault(target, []).append(again)
                changed = True
            entry['kept'] = [k for k in entry['kept'] if k is not row]
            entry.setdefault('dropped', []).append({'why': 'says again something already known', 'kind': 'fact', 'text': row['text'],
                                                    'quote': str(row.get('quote') or '')[:300], 'message_id': row['message_id'], 'same_as': target})
            report['said_again'].append(str(live_facts[target].get('text')))
        # A fact that faded (said once, then not again for STALE_DAYS) is only kept out of
        # what the model is shown; it is never forgotten for linking. When the owner says it again or corrects it, code still
        # finds it with the same narrow checks (the same thing: the quiet pass's own test, then restates; a change:
        # corrects), brings it back (its "stale" note in ladder.json comes off, the revival is noted) and then it moves
        # up the ladder or is replaced exactly as a live fact would be. One never mentioned again stays faded.
        faded = {str(f['id']): f for f in facts(life, now, rows=known) if (f['let_go'] or {}).get('reason') == 'stale'}
        for row in [r for r in entry.get('kept') or [] if r.get('kind') in ('fact', 'correction')] if faded else []:
            said = text_of.get(row.get('message_id'), '')
            if row.get('replaces') or row.get('expires') or row in [a for a, _ in as_fact] or VAGUE.search(said):
                continue
            target = next((fid for fid, f in faded.items() if Q._same({**row, 'kind': 'fact'}, f)), None) or \
                restates(row, said, faded, None, folded.get(row.get('message_id'), set()))
            how = 'said again' if target else None
            if target is None:
                target = corrects(row, said, faded)
                how = 'corrected' if target else None
            if target is None:
                continue
            notes['let_go'].pop(target, None)
            revived = notes['revived'] if isinstance(notes.get('revived'), dict) else {}
            revived[target] = {'at': Q._stamp(now), 'how': how, 'message_id': row.get('message_id'), 'text': str(faded[target].get('text'))[:200]}
            notes['revived'] = dict(list(revived.items())[-REVIVED_KEEP:])
            changed = True
            report['revived'] = report.get('revived', []) + [{'old': str(faded[target].get('text'))[:200], 'old_id': target, 'how': how}]
            if how == 'corrected' or row['kind'] == 'correction':
                row['replaces'] = [target]
                if row['kind'] == 'fact':
                    row['origin'] = 'stated'          # the owner's correction is confirmed, as for a live fact
                report['replaced'].append({'old': str(faded[target].get('text'))[:200], 'old_id': target, 'by': row['text'], 'faded': True})
                continue
            spans = ' '.join(_covered(row.get('quote'), said)) or str(row.get('quote') or '')
            again = {'message_id': row['message_id'], 'said_at': row.get('said_at'), 'quote': str(row.get('quote') or '')[:300],
                     'conversation': cid, 'session': session_of.get(row['message_id'])} | \
                ({'stated': True} if REMEMBER.search(spans) or RULE_WORDS.search(spans) else {})
            if str(row['message_id']) not in {str(h.get('message_id')) for h in _heard(faded[target], notes)}:
                notes['again'].setdefault(target, []).append(again)
            entry['kept'] = [k for k in entry['kept'] if k is not row]
            entry.setdefault('dropped', []).append({'why': 'says again something already known', 'kind': 'fact', 'text': row['text'],
                                                    'quote': str(row.get('quote') or '')[:300], 'message_id': row['message_id'], 'same_as': target})
            report['said_again'].append(str(faded[target].get('text')))
        for dropped in entry.get('dropped') or []:
            if dropped.get('why') != 'already learned' or dropped.get('kind') != 'fact':
                continue
            src = C.quoted_in(dropped.get('quote') or '', owners_new, dropped.get('message_id'))
            if src is None:
                continue
            probe = {'kind': 'fact', 'text': dropped.get('text'), 'quote': dropped.get('quote')}
            again = {'message_id': src['id'], 'said_at': src['at'], 'quote': str(dropped.get('quote') or '')[:300], 'conversation': cid,
                     'session': src.get('session')} | \
                ({'stated': True} if REMEMBER.search(src['text']) or RULE_WORDS.search(src['text']) else {})   # a repeat said as a rule
            target = next((f for f in live_facts.values() if Q._same(probe, f)), None)    # the known item first
            same_sweep = None if target is not None else \
                next((k for k in entry.get('kept') or [] if k.get('kind') == 'fact' and Q._same(probe, k)), None)
            if same_sweep is not None:
                if src['id'] != same_sweep.get('message_id') and all(a.get('message_id') != src['id'] for a in same_sweep.get('again') or []):
                    same_sweep.setdefault('again', []).append(again)
                    report['said_again'].append(same_sweep['text'])
                continue
            if target is None:
                continue
            heard = {str(h.get('message_id')) for h in _heard(target, notes)}
            if str(src['id']) not in heard:
                notes['again'].setdefault(str(target['id']), []).append(again)
                changed = True
                report['said_again'].append(str(target.get('text')))
        # A correction code had to drop leaves the old thing in place. Never silently: it is written
        # down, with what it would have replaced and why it was dropped, in the report and in ladder.json.
        stamp = Q._stamp(now)
        for d in entry.get('dropped') or []:
            if d.get('why') == 'says again something already known':
                continue
            it = next((i for i in items if isinstance(i, dict) and Q._plain(i.get('text')) == d.get('text')
                       and str(i.get('quote') or '')[:300] == d.get('quote')), {})
            olds = [str(x) for x in (it.get('replaces') if isinstance(it.get('replaces'), list) else []) if isinstance(x, (str, int))]
            olds = [(x, (live_facts.get(x) or live_rules.get(x))) for x in olds if x in live_facts or x in live_rules]
            if not olds:
                continue
            note = {'at': stamp, 'text': d.get('text'), 'quote': d.get('quote'), 'message_id': d.get('message_id'), 'why': d.get('why'),
                    'would_replace': [{'id': x, 'text': str(o.get('text'))[:200]} for x, o in olds]}
            report['corrections_dropped'] = report.get('corrections_dropped', []) + [note]
            held = notes.get('corrections_dropped') if isinstance(notes.get('corrections_dropped'), list) else []
            notes['corrections_dropped'] = (held + [note])[-DROPPED_KEEP:]
            changed = True
        if changed and not dry:
            Q._write_json(Path(life) / FILE, notes)
    except Exception as e:
        # Half-decorated lines would be worse than plain ones: take every added field off again.
        entry['kept'], entry['dropped'] = kept0, dropped0
        for row in entry.get('kept') or []:
            for k in ('expires', 'until_words', 'replaces', 'origin', 'again', 'session'):
                row.pop(k, None)
        for row, durable in as_fact:
            row['kind'], row['_durable'] = 'correction', durable
        for row, text, due in promised:
            row['kind'], row['text'], row['due'] = 'fact', text, due
        entry['ladder'] = {'error': f'{type(e).__name__}: {str(e)[:120]}'}
        return
    if any(report.values()):
        entry['ladder'] = {k: v for k, v in report.items() if v}


def rule(add_rule, life, row: dict, now, dry: bool = False) -> dict:
    """A kept correction becomes a standing rule. Switch off: the quiet pass's own add_rule, called as before.
    Switch on: the new rule is written and every rule it replaces moves to `rules_archive`, with the reason and a
    pointer to the new one, in ONE swap of the file: both happen or neither does. Over the size limit, the weakest
    rule (not confirmed first, then the oldest) moves to the archive too. Nothing is deleted."""
    if not on(life):
        return add_rule(life, row['text'], row['quote'], row['message_id'], now, dry)
    Q = _q()
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
    new = {'text': Q._plain(row['text']), 'quote': row['quote'], 'message_id': row['message_id'], 'at': Q._stamp(now)}
    new['id'] = rule_id(new)
    if any(same({**new, 'kind': 'r'}, {**r, 'kind': 'r'}) for r in have):      # on meaning, not only the quote
        return {'effect': 'rule', 'status': 'already a rule'}
    for k in ('expires', 'until_words'):
        if row.get(k):
            new[k] = row[k]
    going = [r for r in have if rule_id(r) in set(row.get('replaces') or [])]
    if going:
        new['replaces'] = [rule_id(r) for r in going]
    if dry:
        return {'effect': 'rule', 'status': 'would add', 'text': new['text']} | ({'would_replace': [r.get('text') for r in going]} if going else {})
    __import__('claims').rule(life, new)   # claims.py (switch claims-on): who said it, and what it replaces is followed through; without the switch the rule is untouched
    stamp = Q._stamp(now)
    moved = [{**r, 'id': rule_id(r), 'let_go': {'reason': 'replaced', 'by': new['id'], 'at': stamp}} for r in going]
    keep = [r for r in have if r not in going] + [new]
    while len(keep) > Q.MAX_RULES:
        ranked = rules({'rules': keep[:-1]}, now)                       # never the one just added
        weakest = next((r for r in ranked if r['standing'] != CONFIRMED), ranked[0] if ranked else None)
        if weakest is None:
            break
        out = next(r for r in keep if rule_id(r) == weakest['id'])
        keep.remove(out)
        moved.append({**out, 'id': rule_id(out), 'let_go': {'reason': 'crowded out', 'by': None, 'at': stamp}})
    archive = [r for r in (d.get('rules_archive') or []) if isinstance(r, dict)] if isinstance(d.get('rules_archive'), list) else []
    d['rules'] = keep
    if moved or archive:
        d['rules_archive'] = (archive + moved)[-ARCHIVE_KEEP:]
    Q._write_json(p, d)
    return {'effect': 'rule', 'status': 'added', 'text': new['text']} | ({'replaced': [r.get('text') for r in going]} if going else {})


def sweep(life, now, dry: bool = False) -> dict:
    """No model. Writes down what the lines already say: facts that were replaced, are past their last day or went
    stale get a note in ladder.json (reason, what replaced them, when), and rules past their last day, or replaced by
    a fact from the owner, move to `rules_archive`. Safe to run any number of times. Switch off: nothing. -> counts, or {}
    when nothing changed."""
    if not on(life):
        return {}
    Q = _q()
    life, now = Path(life), float(now)
    out = {}
    stamp = Q._stamp(now)
    notes = _notes(life)
    fresh = [f for f in facts(life, now) if f['let_go'] and str(f['id']) not in notes['let_go']]
    if fresh:
        for f in fresh:
            notes['let_go'][str(f['id'])] = {**f['let_go'], 'at': stamp, 'text': str(f.get('text'))[:200]}
        out['facts_let_go'] = {}
        for f in fresh:
            out['facts_let_go'][f['let_go']['reason']] = out['facts_let_go'].get(f['let_go']['reason'], 0) + 1
        if not dry:
            Q._write_json(life / FILE, notes | {'swept_at': stamp})
    return out | tidy_rules(life, now, dry)


def tidy_rules(life, now, dry: bool = False, expired: bool = True) -> dict:
    """The rules half of the sweep: a rule past its last day (expired=True) or replaced by a fact line from the owner moves to
    `rules_archive` with the reason and the link, in one swap of preferences.json. Also run by the quiet pass right
    after it writes what it learned (expired=False), so a mail guess the owner corrected leaves every reader of the file
    (the reflection reads it directly) in that same run. Switch off: nothing."""
    if not on(life):
        return {}
    Q = _q()
    life, now = Path(life), float(now)
    out = {}
    stamp = Q._stamp(now)
    p = life / 'preferences.json'
    try:
        d = json.loads(p.read_text()) if p.exists() else None
    except (OSError, ValueError):
        d = None                 # unreadable: left alone, like everywhere else
    if isinstance(d, dict) and isinstance(d.get('rules'), list):
        today = _today(now)
        by_fact = replaced_rules(Q._read_lines(life / 'learned.jsonl'))
        over = [r for r in d['rules'] if expired and isinstance(r, dict) and _day(r.get('expires')) and _day(r['expires']) < today]
        gone = [r for r in d['rules'] if isinstance(r, dict) and r not in over and rule_id(r) in by_fact]
        # A note past the last day it names moves to `notes_archive` in the same swap (prefs.note_last_day)
        import prefs as F
        stale = [n for n in d.get('notes') or [] if expired and isinstance(n, dict) and F.note_last_day(n) and F.note_last_day(n) < today]
        if stale:
            out['notes_expired'] = len(stale)
            if not dry:
                d['notes'] = [n for n in d['notes'] if n not in stale]
                d['notes_archive'] = ([n for n in d.get('notes_archive') or [] if isinstance(n, dict)]
                                      + [{**n, 'let_go': {'reason': 'expired', 'last_day': F.note_last_day(n).isoformat(), 'at': stamp}} for n in stale])[-ARCHIVE_KEEP:]
                if not (over or gone):
                    Q._write_json(p, d)
        if over:
            out['rules_expired'] = len(over)
        if gone:
            out['rules_replaced'] = len(gone)
        if (over or gone) and not dry:
            archive = [r for r in d.get('rules_archive') or [] if isinstance(r, dict)] if isinstance(d.get('rules_archive'), list) else []
            d['rules'] = [r for r in d['rules'] if r not in over and r not in gone]
            d['rules_archive'] = (archive + [{**r, 'id': rule_id(r), 'let_go': {'reason': 'expired', 'by': None, 'at': stamp}} for r in over]
                                  + [{**r, 'id': rule_id(r), 'let_go': {'reason': 'replaced', 'by': by_fact[rule_id(r)], 'at': stamp}} for r in gone])[-ARCHIVE_KEEP:]
            Q._write_json(p, d)
    return out


def sweep_quietly(life, now, dry: bool = False) -> dict:
    """The sweep as the quiet pass calls it: it never stops the pass. Trouble comes back as one line."""
    try:
        return sweep(life, now, dry)
    except Exception as e:
        return {'error': f'{type(e).__name__}: {str(e)[:120]}'}


# ---- the owner lets a thing go from their phone page ------------------------------------------------------------
# The page of what Zeus knows (signin-handoff/knowspage.py, switch file knows-page-on) has "That's wrong" and "Forget
# this" on each learned thing. They end here, in the same two ways this file already lets things go, so every reader
# (facts, live, still_true, rules, rules_text, the reflection) leaves the thing out from that one write on:
#   a fact  -> a note in ladder.json `let_go`, exactly the shape the sweep writes (reason, by, at, text). Its line
#              stays in learned.jsonl. If the owner says it again later, after_check brings it back, as for a faded fact.
#   a rule  -> moved from `rules` to `rules_archive` with `let_go`, in one swap of preferences.json (tidy_rules' shape).
# Nothing is deleted; zeus-snapshot keeps the files from before. The caller holds the tick's lock while calling these.
BY_HIM = {'wrong': 'the owner said it was wrong', 'forgotten': 'the owner asked to forget it'}


def let_go_fact(life, fact_id, reason: str, now=None) -> dict | None:
    """-> the note written, or None when no fact has this id or it was already let go. reason: 'wrong' | 'forgotten'."""
    if reason not in BY_HIM:
        raise ValueError(reason)
    Q = _q()
    life, now = Path(life), float(now if now is not None else time.time())
    f = next((f for f in facts(life, now) if str(f['id']) == str(fact_id)), None)
    if f is None or f['let_go']:
        return None
    notes = _notes(life)
    note = {'reason': reason, 'by': None, 'at': Q._stamp(now), 'text': str(f.get('text'))[:200], 'why': BY_HIM[reason]}
    notes['let_go'][str(f['id'])] = note
    Q._write_json(life / FILE, notes)
    _followed(life, f['id'])
    return note


def _followed(life, claim_id) -> None:
    """A thing the owner let go from their phone page is followed through like any other correction (claims.follow),
    so every other store is told too. Never stops the let-go itself."""
    try:
        C = __import__('claims')
        if C.on(life):
            C.follow(life, claim_id, by={'id': None})
    except Exception:
        pass


def let_go_rule(life, rid: str, reason: str, now=None) -> dict | None:
    """-> the archived rule, or None when no live rule has this id (or the file is unreadable: left alone)."""
    if reason not in BY_HIM:
        raise ValueError(reason)
    Q = _q()
    life, now = Path(life), float(now if now is not None else time.time())
    p = life / 'preferences.json'
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not (isinstance(d, dict) and isinstance(d.get('rules'), list)):
        return None
    gone = replaced_rules(Q._read_lines(life / 'learned.jsonl'))
    if not any(r['id'] == rid for r in rules(d, now, gone)):
        return None
    hit = next(r for r in d['rules'] if isinstance(r, dict) and rule_id(r) == rid)
    archive = [r for r in d.get('rules_archive') or [] if isinstance(r, dict)] if isinstance(d.get('rules_archive'), list) else []
    out = {**hit, 'id': rid, 'let_go': {'reason': reason, 'by': None, 'at': Q._stamp(now), 'why': BY_HIM[reason]}}
    d['rules'] = [r for r in d['rules'] if r is not hit]
    d['rules_archive'] = (archive + [out])[-ARCHIVE_KEEP:]
    Q._write_json(p, d)
    _followed(life, rid)
    return out


# ---- hook in prefs.rules_text -----------------------------------------------------------------------------------------

def rules_text(life_home, d: dict, limit: int = 1500, now=None):
    """The owner's standing rules for the noticer and the worker. Switch off: None, and prefs.py uses its own list.
    Switch on: confirmed rules first, the newest of them first in line for the space, then habits picked up from
    mail. Never one that was let go or is past its last day. Whole lines only, inside the same length limit."""
    if not on(life_home):
        return None
    import enddate as E
    now = float(now if now is not None else time.time())
    today = _today(now)
    ranked = list(enumerate(rules(d, now, replaced_rules(_q()._read_lines(Path(life_home) / 'learned.jsonl')))))
    order = sorted(ranked, key=lambda x: ({CONFIRMED: 0, LEARNED: 1}.get(x[1]['standing'], 2), -x[0]))
    picked, used = [], 0
    for i, r in order:
        line = f"- His standing rule: {r['text']}"     # shared marker: noticer.py and prefs.py read this prefix
        if _day(r.get('expires')):
            line += ' (only until %s)' % E.plain_day(_day(r['expires']), today)
        if used + len(line) + (1 if picked else 0) > limit:
            continue
        picked.append((r['standing'] != CONFIRMED, i, line))
        used += len(line) + (1 if len(picked) > 1 else 0)
    return '\n'.join(line for _, _, line in sorted(picked))


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    life = a[a.index('--life-home') + 1] if '--life-home' in a and a.index('--life-home') + 1 < len(a) else os.environ.get('LIFE_HOME')
    if not life:
        print('Give --life-home <dir> or set LIFE_HOME.')
        return 2
    now = time.time()
    out = {'switch_on': on(life)}
    if '--sweep' in a:
        out['sweep'] = sweep(life, now, dry='--dry' in a)
    fs = facts(life, now)
    out['facts'] = [{'id': f['id'], 'text': f.get('text'), 'standing': f['standing'], 'holds_until': f.get('expires'), 'let_go': f['let_go']} for f in fs]
    prefs = _q()._read_json(Path(life) / 'preferences.json', {})
    prefs = prefs if isinstance(prefs, dict) else {}
    out['rules'] = [{'id': r['id'], 'text': r['text'], 'standing': r['standing'], 'holds_until': r.get('expires')}
                    for r in rules(prefs, now, replaced_rules(_q()._read_lines(Path(life) / 'learned.jsonl')))]
    out['rules_let_go'] = len(prefs.get('rules_archive') or [])
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == '__main__':
    HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE))
    sys.exit(main())

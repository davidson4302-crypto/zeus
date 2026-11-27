"""ladderjudge: the model decides what a thing the owner said MEANS, code checks the facts.

Why: word matching cannot answer the judgments the memory ladder needs: "is this the same thing as one Zeus already
knows?", "is this a rule or a fact?", "is this the owner's own promise or an errand for Zeus?", "what day is 'the week
after the move on friday'?", "is this only a maybe?". Each new word list opens a new hole, and a cross-check (a number
match) that silently drops a correct correction leaves the old rule in force.

The split, for one quiet-pass conversation:
  model  ONE focused call, made only when the look-back proposed something. For each proposed item: is it the same as
         a known item (or as another proposal), a new value of one, or different; how it was said (in passing,
         "remember", as a rule for Zeus, as a correction); what kind it is (fact, rule, the owner's own promise, an
         errand for Zeus right then, a maybe, the premise of a question, not the owner's words); and for an end or due
         date, the day it is counted from and how many days later.
  code   checks facts, never meaning: the quote is word for word in a NEW message from the owner and not inside a
         forwarded block; a relation names a real known id; the day counted from is one the owner named (or the day
         they spoke, or what enddate.py reads in their own words) and the weekday or date the model wrote for it
         agrees; code does the adding; numbers in a kept sentence are the owner's own or the day/month of a day they
         named; standing moves use the ladder's own rules (said again in another conversation: learned; remember, rule
         or correction: confirmed).
  never lost  a correction or rule that fails one of those checks is not stored and whatever it would have replaced
         stays in force, but it is written to ladder.json under `held` with the reason ("needs check"): never in a
         prompt, never acted on, never silently gone.
  word rules  the older word rules (maybe, inside a question, a plan with a day, "for a while", restates, rule words,
         change words, the word-based end-date reading) are still worked out, but only written to the pass report as
         disagreements. None of them decides.
  fallback  no answer, a broken answer, or a verdict missing for any proposal: the ladder runs its own word-rule steps
         (ladder.after_check) for that conversation, and the report says why.

Switch: the existing ladder-on. Without it this file is never imported and nothing changes.
"""
from __future__ import annotations

import copy
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import settings

OWNER = settings.OWNER_FIRST
TIMEOUT = 120
SHOW_KNOWN = 120           # facts shown to the judge (newest first, faded ones included); every live rule is shown too
HELD_KEEP = 50             # held corrections kept in ladder.json for a person to look at
KINDS = ('fact', 'rule', 'his_promise', 'errand_for_zeus', 'maybe', 'inside_question', 'not_his_words')
RELATIONS = ('same_as', 'new_value_of', 'different', 'none')
HOW = ('passing', 'remember', 'rule', 'correction')
CONFIRMING = ('remember', 'rule', 'correction')
AS_KIND = {'fact': 'fact', 'rule': 'correction', 'his_promise': 'promise'}
FROM_KIND = {'fact': 'fact', 'correction': 'rule', 'promise': 'his_promise'}
NOT_KEPT = {'errand_for_zeus': 'an errand the owner gave Zeus in the chat, not something to keep',
            'maybe': 'only a maybe, not a fact',
            'inside_question': 'inside a question from the owner, not something they told Zeus',
            'not_his_words': "not the owner's own words"}
# Drop reasons written by quietpass.check and by this file (prefix match; both wordings accepted).
DETAIL = ('has a detail he did not say', 'has a detail the owner did not say')
NOT_FOUND = ('quote not found in his messages', "quote not found in the owner's messages")
REBUILD = ('already learned',) + DETAIL                           # drops the judge gets a second look at
HOLD_IF_NOT_FOUND = NOT_FOUND + DETAIL
SHORTHAND = ((r'\b(?:tmrw|tmr|tmw|tmrow|2morrow|tomoz)\b', 'tomorrow'), (r'\btonite\b', 'tonight'), (r'\btdy\b', 'today'))
FORWARD = re.compile(r'^\s*(?:-{2,}\s*(?:forwarded|original) message|begin forwarded message|fwd?:|from:\s.+\bsent:)', re.I)

SYSTEM = """You check what Zeus's quiet look-back wants to keep from a chat with {owner}. Another reader already picked
the items (proposals). For each one, decide what it means. Code checks your answer against {owner}'s words, so be exact.

You get: the conversation ({owner}'s lines and Zeus's lines, each with when it was said; only {owner}'s lines marked
new are a source), the proposals (ids p1, p2, ...), and known: what Zeus already knows about {owner}, facts and
standing rules, each with an id, {owner}'s words, how many days ago, how firmly it is known, and faded=true for an
old fact said once.

Return one verdict for every proposal (the field names below say "his": they mean {owner}'s):
- kind:
  fact: something true about {owner}, their people, places or preferences, or where one of their matters stands.
  rule: how Zeus should act from now on, said to Zeus as an instruction ("never book me before 10", "if the alarm
    goes off, call Jordan"). A state of theirs ("I'm off caffeine for a while") is a fact, even if Zeus could act on it.
  his_promise: something {owner} will do themselves on a day, or asked to be reminded of or to have watched later.
  errand_for_zeus: something {owner} asked Zeus to do or pass on right then in the chat ("tell the painter ...",
    "send it").
  maybe: {owner} is not sure or only thinking about it ("might", "probably", "if she retires I'd ...", "idk",
    "leaning").
  inside_question: it only sits inside a question {owner} asked ("since I'm getting her on the 26th, when does she
    land?").
  not_his_words: Zeus said it, or it is pasted or forwarded text, not {owner}'s own words.
- relation, with known_id:
  same_as: it says the same thing as a known item (the same statement, maybe in other words, more firmly, as a rule,
    or with "remember this"), or the same as an earlier proposal (give that proposal's id). A small added color (how
    long it has been so, how much they like it) does not make it different: if it mostly says the known thing again,
    it is same_as.
  new_value_of: {owner}'s words change or correct a known item: the same person or thing, a different value now (a
    new car, a new size, a new amount, a new rule in place of the old one, a guess from their mail they say is wrong).
  different: it is about a known item's person or thing but says something else. none: nothing known is about it.
  A one-off exception ("just this once", "skip it this week") is never a new value of a lasting item.
  known_id: the id for same_as and new_value_of, else null.
- how_said: passing; remember ({owner} asked Zeus to remember or note it); rule (said as a rule for Zeus: always,
  never, from now on, every time); correction ({owner} said Zeus had it wrong, or that it changed).
- end, for a fact or rule that only holds for a while (else all four null):
  end_words: {owner}'s exact words for how long, copied character for character from their line.
  end_from: the day the time is counted from, YYYY-MM-DD. end_from_words: that day in plain words, with its weekday
  ("the move, Friday May 7"). end_plus_days: days after end_from that it still holds (its last day).
  "until Friday" = that Friday plus 0. "for a week" = the day it was said plus 7. "the week after the move on Friday" =
  that Friday plus 7. "till two weeks after the demo on Tuesday" = that Tuesday plus 14. "for a while" has no end.
- due, for a promise or a dated fact: due_from, due_from_words, due_plus_days the same way ("the day before the trip
  on the 18th" = the 18th plus -1). All three null when no day was given.
- reworded: only when your kind differs from the proposal's kind: its sentence rewritten for your kind (a promise as
  the job to do, "Remind {owner} to ..."; a rule as what Zeus should do). Only {owner}'s details. Otherwise null.
Work every date out from the day that line was said, not from today. A weekday means the next one on or after that
day, unless {owner} says "last" or speaks of it as already past.""".replace('{owner}', OWNER)

# A look-back sentence about the owner's plan ("<owner> plans to ...") turned into the reminder job.
PLAN_LEAD = re.compile(r"^(?:" + re.escape(OWNER) + r"|The owner) (?:plans|intends|is planning|is going|wants|needs|has|promised|"
                       r"said (?:he|she|they) would|said (?:he|she|they) will|will)\s+(?:to\s+)?", re.I)

_DAYFIELD = {'type': ['string', 'null']}
_INT = {'type': ['integer', 'null']}
SCHEMA = {'type': 'object', 'required': ['verdicts'], 'properties': {'verdicts': {'type': 'array', 'items': {
    'type': 'object',
    'required': ['proposal', 'kind', 'relation', 'known_id', 'how_said', 'end_words', 'end_from', 'end_from_words', 'end_plus_days',
                 'due_from', 'due_from_words', 'due_plus_days', 'reworded'],
    'properties': {
        'proposal': {'type': 'string'},
        'kind': {'enum': list(KINDS)},
        'relation': {'enum': list(RELATIONS)},
        'known_id': {'type': ['string', 'null']},
        'how_said': {'enum': list(HOW)},
        'end_words': _DAYFIELD, 'end_from': _DAYFIELD, 'end_from_words': _DAYFIELD, 'end_plus_days': _INT,
        'due_from': _DAYFIELD, 'due_from_words': _DAYFIELD, 'due_plus_days': _INT,
        'reworded': {'type': ['string', 'null']},
    }}}}}


# ---- small helpers -------------------------------------------------------------------------------------------------

def _mods():
    import quietpass as Q
    import ladder as LD
    import enddate as E
    import chats as C
    return Q, LD, E, C


def _iso_day(value):
    try:
        return date.fromisoformat(str(value or '')[:10])
    except ValueError:
        return None


def _short(text) -> str:
    t = str(text or '')
    for pat, word in SHORTHAND:
        t = re.sub(pat, word, t, flags=re.I)
    return t


def his_part(msg: str) -> str:
    """The owner's own lines of a message: a forwarded or pasted block ("> ..." lines, or everything after a "Forwarded
    message" or "From: ... Sent:" header) is not theirs, even though it sits in a message from them."""
    out = []
    for line in str(msg or '').splitlines():
        if FORWARD.search(line):
            break
        if not line.lstrip().startswith('>'):
            out.append(line)
    return '\n'.join(out)


def _said_day(Q, at) -> date:
    return datetime.fromtimestamp(float(at), Q.PACIFIC).date()


# ---- what the judge is shown ---------------------------------------------------------------------------------------

def known_for_judge(life, known: list, now: float) -> tuple:
    """-> (shown list, {id: ('fact'|'rule', the entry, faded?)}). Every fact still known, plus the ones that faded (said
    once, long ago: kept out of every other prompt, but the owner may say them again or correct them), and every live rule."""
    Q, LD, _, _ = _mods()
    shown, index = [], {}
    fs = LD.facts(life, now, rows=known)
    notes = LD._notes(life)
    keep = [f for f in fs if not f['let_go'] or f['let_go'].get('reason') == 'stale'][-SHOW_KNOWN:]
    for f in keep:
        last = max(float(h.get('said_at') or 0) for h in LD._heard(f, notes))
        faded = bool(f['let_go'])
        index[str(f['id'])] = ('fact', f, faded)
        shown.append({'id': str(f['id']), 'kind': 'fact', 'text': f.get('text'), 'his_words': str(f.get('quote') or '')[:160],
                      'days_ago': int((now - last) // 86400) if last else None, 'known': f['standing']}
                     | ({'faded': True} if faded else {}) | ({'holds_until': f['expires']} if f.get('expires') else {}))
    prefs = Q._read_json(Path(life) / 'preferences.json', {})
    for r in LD.rules(prefs if isinstance(prefs, dict) else {}, now, LD.replaced_rules(known)):
        index[r['id']] = ('rule', r, False)
        try:
            ago = int((now - datetime.fromisoformat(str(r.get('at'))).timestamp()) // 86400)
        except (TypeError, ValueError):
            ago = None
        shown.append({'id': r['id'], 'kind': 'rule', 'text': r.get('text'), 'his_words': str(r.get('quote') or '')[:160],
                      'days_ago': ago, 'known': r['standing']} | ({'holds_until': r['expires']} if r.get('expires') else {}))
    return shown, index


def build_prompt(conv: list, bookmark: int, now: float, proposals: list, shown: list) -> str:
    Q, _, _, _ = _mods()
    lines = []
    for m in conv[-80:]:
        if m['who'] == 'owner':
            text = Q.own_words(m['text'])[:2000]
            if text:
                lines.append({'id': m['id'], 'who': OWNER, 'said': Q._said(m['at']), 'new': m['id'] > bookmark, 'text': text})
        else:
            lines.append({'who': 'Zeus (context only)', 'said': Q._said(m['at']), 'text': m['text'][:400]})
    props = [{'id': p['pid'], 'kind': FROM_KIND.get(p['row']['kind'], p['row']['kind']), 'text': p['row']['text'],
              'his_words': p['row']['quote'][:400], 'line': p['row']['message_id']} for p in proposals]
    return json.dumps({'today': Q._said(now), 'conversation': lines, 'proposals': props, 'known': shown}, ensure_ascii=False, indent=1)


# ---- the proposals -------------------------------------------------------------------------------------------------

def _row_like_check(Q, C, it: dict, his_new: list, his_all_text: str, cid, now: float):
    """The row quietpass.check would have built for a model item it dropped after the quote check passed."""
    src = C.quoted_in(str(it.get('quote') or '').strip(), his_new, it.get('message_id'))
    if src is None:
        return None
    quote = str(it.get('quote') or '').strip()
    return {'kind': it['kind'], 'text': Q._plain(it.get('text')), 'quote': quote[:600], 'message_id': src['id'], 'said_at': src['at'],
            'people': [p.strip() for p in (it.get('people') or []) if isinstance(p, str) and Q._he_said(p, his_all_text)][:8],
            'due': Q._due(it.get('due')), 'conversation': cid, 'at': now,
            '_durable': it.get('durable') is not False, '_covered_by': it.get('covered_by')}


def _item_of(Q, d: dict, items: list) -> dict:
    return next((i for i in items if isinstance(i, dict) and Q._plain(i.get('text')) == d.get('text')
                 and str(i.get('quote') or '')[:300] == d.get('quote')), {})


def proposals_of(entry: dict, items: list, conv: list, bookmark: int, now: float) -> list:
    """Everything the look-back proposed that passed the quote check: what check() kept, plus what it dropped only as
    "the same as something" or for a number (both are judgments or checks the judge gets a second look at)."""
    Q, _, _, C = _mods()
    his = [{**m, 'text': Q.own_words(m['text'])} for m in conv if m['who'] == 'owner']
    his = [m for m in his if m['text']]
    his_new = [m for m in his if m['id'] > bookmark]
    his_all_text = Q._flat(' '.join(m['text'] for m in his))
    cid = C.conv_id(conv)
    out = []
    LD = _mods()[1]
    for row in entry.get('kept') or []:
        out.append({'row': copy.deepcopy(row), 'dropped': None, 'item': LD._item_for(row, items) or {}})
    for d in entry.get('dropped') or []:
        if not str(d.get('why') or '').startswith(REBUILD):
            continue
        it = _item_of(Q, d, items)
        row = _row_like_check(Q, C, it, his_new, his_all_text, cid, now) if it.get('kind') in Q.KINDS else None
        if row is not None:
            out.append({'row': row, 'dropped': d, 'item': it})
    for i, p in enumerate(out, 1):
        p['pid'] = 'p%d' % i
    return out


def _valid(res, pids) -> tuple:
    """-> ({pid: verdict}, None) or (None, why)."""
    out = (res or {}).get('out') if isinstance(res, dict) else None
    verdicts = out.get('verdicts') if isinstance(out, dict) else None
    if not isinstance(verdicts, list):
        return None, 'no answer'
    got = {}
    for v in verdicts:
        if not isinstance(v, dict) or v.get('kind') not in KINDS or v.get('relation') not in RELATIONS or v.get('how_said') not in HOW:
            return None, 'a verdict that does not fit the form'
        got[str(v.get('proposal'))] = v
    missing = [p for p in pids if p not in got]
    if missing:
        return None, 'no verdict for ' + ', '.join(missing)
    return got, None


# ---- code checks the facts -----------------------------------------------------------------------------------------

def named(E, his: list) -> set:
    """Every day the owner named in their lines, each counted from the day that line was said, plus a named weekday's previous
    week too ("the surgery was monday"), and the day itself."""
    Q = _mods()[0]
    out = set()
    for m in his:
        day = _said_day(Q, m['at'])
        out.add(day)
        for _, d in E.named_days(_short(m['text']), day):
            out.add(d)
            if timedelta(0) <= d - day < timedelta(days=7):
                out.add(d - timedelta(days=7))
    return out


def counted(E, start, plus, from_words, ok_days: set):
    """The model's day to count from, checked, plus its count, added by code. -> (day, None) or (None, why)."""
    d = _iso_day(start)
    if d is None:
        return None, 'no day to count from'
    if isinstance(plus, bool) or not isinstance(plus, int) or abs(plus) > 366:
        return None, 'the count is not a whole number of days'
    if d not in ok_days:
        return None, 'the day it counts from (%s) is not a day the owner named' % d.isoformat()
    fw = E._flat(from_words)
    for i, name in enumerate(E.WEEKDAYS):
        if re.search(r'\b' + name + r'\b', fw) and d.weekday() != i:
            return None, '"%s" is not a %s' % (d.isoformat(), name.capitalize())
    for m in re.finditer(r'\b(' + E._MONTH + r')\s+(\d{1,2})(?:st|nd|rd|th)?\b', fw):
        mo = next(i for i, x in enumerate(E.MONTHS) if x.startswith(m.group(1)[:3])) + 1
        if (d.month, d.day) != (mo, int(m.group(2))):
            return None, '"%s" is not %s %s' % (d.isoformat(), m.group(1), m.group(2))
    return d + timedelta(days=plus), None


def loose_numbers(LD, Q, text: str, his_all_text: str, said_at, days: set) -> list:
    """Numbers in a sentence that are not the owner's: not typed by them (5k is 5,000), not the day they spoke, and not
    the day, month or year of a day they named or the model's checked dates ("tuesday" -> 11)."""
    first = Q._loose_numbers(text, his_all_text, None, said_at)
    if not first:
        return []
    said = datetime.fromtimestamp(float(said_at), Q.PACIFIC)
    ok = {v for _, v in LD._amounts(his_all_text)} | {float(n) for n in re.findall(r'\d+', his_all_text)} | \
        {float(said.year), float(said.month), float(said.day)}
    for d in days:
        ok |= {float(d.year), float(d.month), float(d.day)}
    return [w for w, v in LD._amounts(text) if v not in ok]


# ---- the step itself -----------------------------------------------------------------------------------------------

def decide(life, entry: dict, items: list, conv: list, bookmark: int, known: list, now, dry: bool, complete) -> bool:
    """Called by ladder.after_check (switch on) with the quiet pass's own model door. True: this conversation was decided
    here. False: fall back to the ladder's earlier steps (entry['judge'] says why); nothing was changed."""
    Q, LD, E, C = _mods()
    now = float(now)
    life = Path(life)
    props = proposals_of(entry, items, conv, bookmark, now)
    unplaced = [d for d in entry.get('dropped') or [] if str(d.get('why') or '').startswith(HOLD_IF_NOT_FOUND)
                and (d.get('kind') == 'correction' or (_item_of(Q, d, items).get('replaces') or []))]
    if not props and not unplaced:
        return False                                       # nothing proposed: no call, nothing to decide
    t0 = time.time()
    try:
        shown, index = known_for_judge(life, known, now)
        prompt = build_prompt(conv, bookmark, now, props, shown)
    except Exception as e:
        entry['judge'] = {'calls': 0, 'fallback': f'could not build the question: {type(e).__name__}: {str(e)[:100]}'}
        return False
    if not props:                                          # only a correction to hold: no question to ask
        try:
            return _apply(life, entry, items, conv, bookmark, known, now, dry, [], {}, index, {'calls': 0, 'proposals': 0})
        except Exception as e:
            entry['judge'] = {'calls': 0, 'fallback': f'{type(e).__name__}: {str(e)[:120]}: the earlier word rules decided'}
            return False
    try:
        res = complete(SYSTEM, prompt, SCHEMA, timeout=TIMEOUT)
    except Exception as e:                                 # the door itself broke: same as no answer
        res = None
        entry['judge'] = {'error': f'{type(e).__name__}: {str(e)[:100]}'}
    seconds = round(time.time() - t0, 1)
    verdicts, why = _valid(res, [p['pid'] for p in props])
    if verdicts is None:
        entry['judge'] = (entry.get('judge') or {}) | {'calls': 1, 'seconds': seconds, 'fallback': why + ': the earlier word rules decided'}
        return False
    try:
        return _apply(life, entry, items, conv, bookmark, known, now, dry, props, verdicts, index,
                      {'calls': 1, 'seconds': seconds, 'cost': (res or {}).get('cost'), 'model': (res or {}).get('model'), 'proposals': len(props)})
    except Exception as e:
        entry['judge'] = {'calls': 1, 'seconds': seconds, 'fallback': f'{type(e).__name__}: {str(e)[:120]}: the earlier word rules decided'}
        return False


def _apply(life, entry, items, conv, bookmark, known, now, dry, props, verdicts, index, judge) -> bool:
    Q, LD, E, C = _mods()
    his = [{**m, 'text': Q.own_words(m['text'])} for m in conv if m['who'] == 'owner']
    his = [m for m in his if m['text']]
    text_of = {m['id']: m['text'] for m in his}
    session_of = {m['id']: m.get('session') for m in conv}
    his_all_text = Q._flat(' '.join(m['text'] for m in his))
    ok_days = named(E, his)
    cid = C.conv_id(conv)
    stamp = Q._stamp(now)
    notes, changed = LD._notes(life), False
    report = {'kept': [], 'said_again': [], 'replaced': [], 'revived': [], 'ends': [], 'refused': [], 'held': [], 'not_kept': []}
    disagree = []
    kept, dropped, held = [], [], []
    by_pid = {}

    def note_drop(row, why, **more):
        dropped.append({'why': why, 'kind': row.get('kind'), 'text': row.get('text'), 'quote': str(row.get('quote') or '')[:300],
                        'message_id': row.get('message_id')} | more)

    def hold(row, why, target=None):
        rec = {'at': stamp, 'status': 'needs check', 'why': why, 'kind': row.get('kind'), 'text': row.get('text'),
               'quote': str(row.get('quote') or '')[:300], 'message_id': row.get('message_id'), 'said_at': row.get('said_at'),
               'conversation': cid}
        if target and target in index:
            rec['would_replace'] = [{'id': target, 'text': str(index[target][1].get('text'))[:200]}]
        held.append(rec)
        report['held'].append({'text': row.get('text'), 'why': why} | ({'old_stays': target} if target else {}))
        note_drop(row, 'held: needs check (%s)' % why)

    def cross(check, words, model, row):
        """Logs where the old word rules and the model part ways. Never decides anything."""
        if (words or None) != (model or None):
            disagree.append({'for': row.get('text'), 'check': check, 'word_rules': words or None, 'model': model or None})

    for p in props:
        row, v, src_drop = p['row'], verdicts[p['pid']], p['dropped']
        said = text_of.get(row.get('message_id'), '')
        said_day = _said_day(Q, row.get('said_at') or now)
        kind = v['kind']
        p['verdict'] = v
        # -- cross-checks only: what the old word rules would have said (never decides) --------------------------------
        try:
            if row['kind'] == 'fact':
                cross('maybe', bool(LD.HEDGED.search(row['quote']) and not LD.RULE_WORDS.search(row['quote']) or LD.hedged(row)), kind == 'maybe', row)
                cross('plan with a day', bool(LD._as_promise(row, said, now)), kind == 'his_promise', row)
            if row['kind'] in ('fact', 'promise'):
                cross('inside a question', LD.in_question(row['quote'], said), kind == 'inside_question', row)
            if row['kind'] == 'correction':
                cross('for a while, not a rule', bool(LD.VAGUE.search(said) and not LD.RULE_WORDS.search(said)), kind == 'fact', row)
        except Exception as e:
            disagree.append({'for': row.get('text'), 'check': 'word rules', 'error': f'{type(e).__name__}: {str(e)[:80]}'})
        # -- safety: the owner's own words, not a forwarded block ------------------------------------------------------------
        if Q._flat(row['quote']) not in Q._flat(his_part(said)):
            note_drop(row, "forwarded or pasted text, not the owner's words")
            continue
        if kind in NOT_KEPT:
            note_drop(row, NOT_KEPT[kind])
            report['not_kept'].append({'text': row['text'], 'why': NOT_KEPT[kind]})
            continue
        new_kind = AS_KIND[kind]
        text_changed = False
        if new_kind != row['kind']:
            old_kind, old_text = row['kind'], row['text']
            row['kind'] = new_kind
            words = Q._plain(v.get('reworded')) if v.get('reworded') else ''
            if words and len(words) >= 8 and (LD._content(words) & (LD._content(old_text) | LD._content(row['quote']))) \
                    and not loose_numbers(LD, Q, words, his_all_text, row['said_at'], ok_days):
                row['text'], text_changed = words, True
            elif new_kind == 'promise':
                row['text'] = PLAN_LEAD.sub('Remind %s to ' % OWNER, old_text, count=1)
            if old_kind == 'correction' and new_kind == 'fact':
                row['_durable'] = False                     # a state of the owner's for now, not a lasting fact
            report.setdefault('kind_changed', []).append({'from': old_kind, 'to': new_kind, 'text': row['text']})
        # -- dates: the model names the day to count from and the count, code checks and adds --------------------------
        checked_days = set()
        end_failed = None
        if row['kind'] in ('fact', 'correction') and (v.get('end_from') or v.get('end_plus_days') is not None):
            words = str(v.get('end_words') or '').strip()
            old_read = E.end_day(words, said_day, True, said) if words and Q._flat(words) in Q._flat(said) else None
            if not words or Q._flat(words) not in Q._flat(said):
                day, end_failed = None, "the words for how long are not the owner's"
            else:
                day, end_failed = counted(E, v.get('end_from'), v.get('end_plus_days'), v.get('end_from_words'), ok_days | {old_read} - {None})
                if day is not None and (day < said_day or (day - said_day).days > E.MAX_DAYS):
                    day, end_failed = None, 'the last day %s is before the owner spoke or over a year away' % day.isoformat()
            if day is None and old_read is not None:
                # the model's day did not check out, but code can read the owner's own words for how long: that day is used
                report['refused'].append({'for': row['text'], 'why': end_failed + "; the owner's own words were read by code instead"})
                day, end_failed = old_read, None
            if day is not None:
                row['expires'], row['until_words'] = day.isoformat(), words[:80]
                checked_days |= {day, _iso_day(v.get('end_from'))}
                report['ends'].append({'for': row['text'], 'last_day': row['expires'], 'from': v.get('end_from_words')})
                cross('end date', old_read and old_read.isoformat(), row['expires'], row)
            else:
                report['refused'].append({'for': row['text'], 'why': end_failed})
        elif row['kind'] in ('fact', 'correction'):
            ex = str(p['item'].get('until_words') or '').strip()
            old_read = E.end_day(ex, said_day, True, said) if ex and Q._flat(ex) in Q._flat(said) else None
            cross('end date', old_read and old_read.isoformat(), None, row)
        if v.get('due_from') or v.get('due_plus_days') is not None:
            day, bad = counted(E, v.get('due_from'), v.get('due_plus_days') or 0, v.get('due_from_words'), ok_days)
            if day is None:
                report['refused'].append({'for': row['text'], 'why': 'due: ' + bad + ' (the look-back\'s own day kept)'})
            else:
                checked_days |= {day, _iso_day(v.get('due_from'))}
                if _iso_day(row.get('due')) != day:
                    row['due'] = Q._due(day.isoformat())
        checked_days |= {_iso_day(row.get('due'))}
        checked_days.discard(None)
        # -- numbers in what is kept must be the owner's (a check, never a meaning). Only where the sentence is new to this step:
        # one check() dropped for a number, or one reworded here; what check() kept already passed. ----------------------
        recheck = text_changed or str((src_drop or {}).get('why') or '').startswith(DETAIL)
        loose = loose_numbers(LD, Q, row['text'], his_all_text, row['said_at'], ok_days | checked_days) if recheck and row['kind'] != 'promise' else []
        if loose:
            if row['kind'] == 'correction' or v['relation'] == 'new_value_of':
                hold(row, 'has a detail the owner did not say (%s)' % ', '.join(loose[:4]), v.get('known_id') if v['relation'] == 'new_value_of' else None)
            else:
                note_drop(row, 'has a detail the owner did not say (%s)' % ', '.join(loose[:4]))
            continue
        target = str(v.get('known_id')) if v.get('known_id') is not None else None
        if end_failed and row['kind'] == 'correction':
            # a rule the owner said holds only for a while must never go live as a lasting one
            hold(row, end_failed + ', and a rule given for a while is never kept as lasting', target if v['relation'] == 'new_value_of' else None)
            continue
        if row['kind'] in ('fact', 'correction') and session_of.get(row.get('message_id')):
            row['session'] = session_of[row['message_id']]
        p['row'] = row
        p['state'] = 'open'
        by_pid[p['pid']] = p

    # -- relations: the model says what it is to what is known; code checks the id and applies the ladder's own rules ---
    def again_of(row, how):
        return {'message_id': row['message_id'], 'said_at': row.get('said_at'), 'quote': str(row.get('quote') or '')[:300],
                'conversation': cid, 'session': session_of.get(row['message_id'])} | ({'stated': True} if how in CONFIRMING else {})

    def revive(fid, how, row):
        nonlocal changed
        notes['let_go'].pop(fid, None)
        revived = notes['revived'] if isinstance(notes.get('revived'), dict) else {}
        revived[fid] = {'at': stamp, 'how': how, 'message_id': row.get('message_id'), 'text': str(index[fid][1].get('text'))[:200]}
        notes['revived'] = dict(list(revived.items())[-LD.REVIVED_KEEP:])
        changed = True
        report['revived'].append({'old': str(index[fid][1].get('text'))[:200], 'old_id': fid, 'how': how})

    def said_again(fid, row, how):
        nonlocal changed
        f = index[fid][1]
        if index[fid][2]:
            revive(fid, 'said again', row)
        if str(row['message_id']) not in {str(h.get('message_id')) for h in LD._heard(f, notes)}:
            notes['again'].setdefault(fid, []).append(again_of(row, how))
            changed = True
        elif how in CONFIRMING:                           # the same message already counted: only lift it to stated
            for a in notes['again'].get(fid) or []:
                if str(a.get('message_id')) == str(row['message_id']) and not a.get('stated'):
                    a['stated'] = True
                    changed = True
        note_drop(row, 'says again something already known', same_as=fid)
        report['said_again'].append(str(f.get('text'))[:200])

    folded_into = {}                                      # pid -> known id it was folded into
    for p in props:
        if p.get('state') != 'open':
            continue
        row, v = p['row'], p['verdict']
        rel, how = v['relation'], v['how_said']
        target = str(v.get('known_id')) if v.get('known_id') is not None else None
        try:
            if row['kind'] in ('fact', 'correction'):
                hint = None if rel != 'same_as' or not target else target
                words = LD.restates(row, text_of.get(row['message_id'], ''), {k: x[1] for k, x in index.items() if x[0] == 'fact' and not x[2]}, hint)
                cross('same as a known fact', words, target if rel == 'same_as' and target in index else None, row)
                cross('said as a rule or "remember"', bool(LD.REMEMBER.search(row['quote']) or LD.RULE_WORDS.search(row['quote'])), how in CONFIRMING, row)
        except Exception as e:
            disagree.append({'for': row.get('text'), 'check': 'word rules', 'error': f'{type(e).__name__}: {str(e)[:80]}'})
        if row['kind'] == 'promise' or rel in ('different', 'none'):
            p['state'] = 'keep'
            continue
        if target in by_pid and rel == 'same_as' and props.index(by_pid[target]) < props.index(p):
            first = by_pid[target]
            if first.get('state') == 'keep':               # two proposals of one thing: one item, the second saying noted
                p['state'] = 'dup'
                if str(row['message_id']) != str(first['row']['message_id']) and \
                        all(str(a.get('message_id')) != str(row['message_id']) for a in first['row'].get('again') or []):
                    first['row'].setdefault('again', []).append(again_of(row, how))
                note_drop(row, 'already learned', same_as=first['pid'])
                continue
            if target in folded_into:
                target = folded_into[target]
            else:
                p['state'] = 'keep'
                continue
        if target not in index:
            report['refused'].append({'for': row['text'], 'would_link': target, 'why': 'that entry is not something known now'})
            p['state'] = 'keep'
            continue
        store, old, faded = index[target]
        if rel == 'same_as':
            if store == 'fact':
                said_again(target, row, how)
                p['state'] = 'folded'
                folded_into[p['pid']] = target
                continue
            if old['standing'] == LD.CONFIRMED or how not in CONFIRMING:
                note_drop(row, "already one of the owner's rules", same_as=target)
                p['state'] = 'folded'
                folded_into[p['pid']] = target
                continue
            rel = 'new_value_of'                           # the owner's own words now say what Zeus had only guessed from mail
        # new_value_of
        if row.get('expires') and not old.get('expires'):
            report['refused'].append({'for': row['text'], 'would_replace': target, 'why': 'a thing that holds only for a while never replaces a lasting one'})
            p['state'] = 'keep'
            continue
        if faded:
            revive(target, 'corrected', row)
        row['replaces'] = [target]
        if row['kind'] == 'fact':
            row['origin'] = 'stated'                       # the owner's correction is confirmed
        report['replaced'].append({'old': str(old.get('text'))[:200], 'old_id': target, 'by': row['text']})
        try:
            cross('change words', bool(LD.CORRECTS.search(text_of.get(row['message_id'], ''))), True, row)
        except Exception:
            pass
        p['state'] = 'keep'

    for p in props:
        if p.get('state') != 'keep':
            continue
        row, v = p['row'], p['verdict']
        if row['kind'] == 'fact' and 'origin' not in row:
            row['origin'] = 'stated' if v['how_said'] in CONFIRMING else 'heard'
        if p['dropped'] is not None and not row.get('replaces') and \
                any(Q._key(row['text']) == Q._key(k.get('text')) for k in LD.live(life, known, now) + [x['row'] for x in props if x is not p and x.get('state') == 'keep']):
            note_drop(row, 'already learned')               # the very same sentence is already known: never a second copy
            disagree.append({'for': row['text'], 'check': 'the same sentence is already known', 'word_rules': True, 'model': v['relation']})
            continue
        kept.append(row)
        report['kept'].append(row['text'])

    # -- corrections the quote check could not place: not the owner's words as written, but never silently gone -------
    for d in entry.get('dropped') or []:
        why = str(d.get('why') or '')
        if not why.startswith(HOLD_IF_NOT_FOUND) or any(p['dropped'] is d for p in props):
            continue
        it = _item_of(Q, d, items)
        names = [str(x) for x in (it.get('replaces') if isinstance(it.get('replaces'), list) else []) if str(x) in index]
        if it.get('kind') == 'correction' or names:
            rec = {'at': stamp, 'status': 'needs check', 'why': why, 'kind': d.get('kind'), 'text': d.get('text'), 'quote': d.get('quote'),
                   'message_id': d.get('message_id'), 'conversation': cid}
            if names:
                rec['would_replace'] = [{'id': x, 'text': str(index[x][1].get('text'))[:200]} for x in names]
            held.append(rec)
            report['held'].append({'text': d.get('text'), 'why': why} | ({'old_stays': names} if names else {}))

    if held:
        before = notes.get('held') if isinstance(notes.get('held'), list) else []
        notes['held'] = (before + held)[-HELD_KEEP:]
        changed = True
    if changed and not dry:
        Q._write_json(Path(life) / LD.FILE, notes)
    # only now, after every write that can fail, is the entry changed
    from_props = [p['dropped'] for p in props if p['dropped'] is not None]
    entry['kept'] = kept
    entry['dropped'] = [d for d in entry.get('dropped') or [] if not any(d is x for x in from_props)] + dropped
    judge['disagreements'] = disagree
    entry['judge'] = judge
    out = {k: v for k, v in report.items() if v}
    if out:
        entry['ladder'] = out
    return True

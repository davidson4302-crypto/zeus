"""Composer: the real Zeus writes the one message the owner gets, after the work is done.

The text is never written by a side model at the moment of noticing, before any work ("Want me to ...?"). It is
written by Zeus itself: one quiet single-question turn through Zeus's own Hermes profile (same model, same SOUL,
same memory as its chat), in a side session so the owner's real conversation is not touched. It gets the finished result
packets and writes ONE message, or says there is nothing worth a text.

Code checks the draft: permission-asking phrases and made-up reply codes get one rewrite. Zeus then re-reads its own
text against the results, and a second, independent model (the noticer's route, a different model family) checks
every claim against the results; anything it finds gets one targeted fix by Zeus. Nothing is sent from here; the
notifier sends.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402

HERMES = settings.HERMES_BIN
SILENT = '[SILENT]'
LIMIT = 400        # one aim for every text: about 140 characters, 400 at most
# Asking permission to be helpful, vague closing offers, and "answer by number" make a text feel robotic.
BANNED = re.compile(
    r"\b(want me to|would you like me to|do you want me to|shall i\b|should i go ahead|i can (?:also )?(?:help|check|look|find|draft|handle|map|sort|prepare)"
    r"|let me know if|reply with a number|happy to help|if you(?:'d| would) like,? i)", re.I)

# "Reply "rent" and "send"": a made-up word to type is a reply code, and texts never carry one.
CODEWORD = re.compile(r"\b(?:say|reply|text(?: me| back)?|answer(?: with)?|type)\s+[“\"'‘][^”\"'’\n]{1,30}[”\"'’]", re.I)


# Two endings that make an assistant's note read as machine-written: a go-word to say, and a stock closing line.
GO_WORD = re.compile(r"(?:(?:^|(?<=[.!?:;])\s+|\n)Say (?:[a-z']+|the word)(?=[.!]|\s*$))"      # "Say send." "Say rent."
                     r"|(?:\b[Ss]ay (?:send|yes|go|the word),? and (?:I'll|I will|it)\b)")   # "Say yes and I'll send it"
STOCK_CLOSE = re.compile(r"(?:^|(?<=[.!?:])\s+|\n)(?:Nothing to do|No action needed|Nothing needed from you)\s*[.:]", re.I)


# A text is about the thing, never about the assistant's own checking. Typical lines this catches: "I'll look again at
# 7pm", "I couldn't open the original email", "Couldn't recheck this time, so this is my last good read", "nothing
# changes without your yes".
PROCESS = re.compile(
    r"(?:\bi(?:'ll| will) (?:look|check|re-?check|take another look)\b(?: (?:again|back))?"
    r"|\bi (?:couldn't|could not|can't|cannot|wasn't able to|was not able to) (?:open|read|re-?read|re-?check|see|confirm|verify|access|get into)\b"
    r"|\bi (?:haven't|have not) (?:seen|confirmed|looked|opened|read|checked|verified)\b"
    r"|\b(?:couldn't|could not) re-?check\b|\blast good read\b|\bas of my last (?:read|look)\b"
    r"|\bnothing changes without your (?:yes|approval|ok)\b|\bi won't change anything\b|\btracking isn't running\b)", re.I)


def _plain_quotes(text) -> str:
    return str(text or '').replace('\u2019', "'").replace('\u2018', "'")      # a curly apostrophe must not slip a line past the check


def process_talk(text: str) -> list:
    return sorted({m.group(0).lower() for m in PROCESS.finditer(_plain_quotes(text))})


def cut_process(text: str) -> str:
    """Last resort, by code: take out every sentence that talks about the assistant's own process. '' when nothing
    else is left (then there was nothing to tell the owner)."""
    paras = []
    for para in str(text or '').split('\n\n'):
        kept = [s for s in re.split(r'(?<=[.!?])\s+', para.strip()) if s and not PROCESS.search(_plain_quotes(s))]
        if kept:
            paras.append(' '.join(kept))
    return '\n\n'.join(paras)


def violations(text: str) -> list:
    found = {m.group(0).lower() for m in BANNED.finditer(text)}
    found |= {'talk about your own process ("' + p + '")' for p in process_talk(text)}
    if GO_WORD.search(text):
        found.add('a word for the owner to say')
    if STOCK_CLOSE.search(text):
        found.add('a stock closing line')
    if CODEWORD.search(text):
        found.add('a reply word in quotation marks')
    return sorted(found)


ASK_FREE = 'a question, when nothing here is theirs to decide'


def owners_to_decide(items: list) -> bool:
    """Is anything in this message the owner's call: a prepared decision, or something only they can supply?"""
    return any(isinstance(it, dict) and bool(it.get('decision_for_owner') or it.get('result') in ('needs_decision', 'blocked')) for it in items)


def found_in(text: str, items: list) -> list:
    """violations(), plus the one that depends on what is being told: a message about finished work asks nothing."""
    found = violations(text)
    for it in items:                      # the "?" inside an item's own action link (fresh.action_link) asks nothing
        if isinstance(it, dict) and it.get('link'):
            text = text.replace(str(it['link']), ' ')
    if '?' in text and not owners_to_decide(items):
        found.append(ASK_FREE)
    return found


def cut_questions(text: str) -> str:
    """Last resort, by code: take out every sentence that ends in a question mark, and a short pick that leans on it
    ("I lean yes."). Only used when a message that may not ask anything still does after its rewrite."""
    paras = []
    for para in text.split('\n\n'):
        kept, after_question = [], False
        for s in re.split(r'(?<=[.!?])\s+', para.strip()):
            if s.endswith('?'):
                after_question = True
                continue
            if after_question and len(s.split()) <= 8 and re.match(r"(?:I lean|I'd say|I would say|My pick|I'd go|Your call)\b", s):
                continue
            after_question = False
            kept.append(s)
        if kept:
            paras.append(' '.join(kept))
    return '\n\n'.join(paras)


PROMPT = """[Life pipes, compose step. This comes from your own background work, not from {owner}. Do not use tools, do not send anything, do not save anything. Reply with only the text message, or exactly [SILENT].]

You are about to text {owner} on iMessage about something your life pipes noticed and worked on. Write the ONE message they will receive. It is {now}.

What is ready (your own results, JSON):
{items}

What you already texted them in the last day (do not repeat any of it):
{recent}

{voice}How to write it
- Say what happened, the way a person texts it. Straight into the fact: no greeting, no name, no sign-off, no emoji, no exclamation marks. About 140 characters: the one main thing first, the gist not the record ("Dryer's fixed, $120", not the repair log). More than one item: the one that matters most, then each other one in a short clause, never a list or a rundown. Leave out what they do not need in order to act; when something real is left out that they may want, end with "Details if you want." Past 400 characters only for the words of a draft they will send. Shorter means choosing what to say, never stopping mid-thought. Talk to them: "you", "your", and the names they use for their people (Mom, first names), never a full legal name.
- Keep the exact detail they would act on: the time, the amount, the place, the name. Every number, time, date and name must come from the items ("noticed" counts), attached to the same thing it is attached to there. Say dates the way they would: today, tomorrow, or the weekday; for a day that has passed, say "yesterday", "last Saturday", or give the date; check the thing's own date against today's date above first.
- Never talk about yourself or your process. Not what you could not open, read, check or confirm; not where you looked; not what you will not do; not when you will look again. No check times, ever. If the items say a watch is running on it, you may promise the moment instead: "I'll tell you when the flight lands."
- When something is not known yet, say it about the thing itself, in a clause, and only if it changes what they should do: "No gate posted yet." "It hasn't shipped yet."
- If an item has "prep", the background work on it did not happen: tell them only what you noticed, as a plain fact, and claim no checking or look-ups.
- Say nothing more firmly than the items do. Never call something done, sent, booked, paid, cancelled or confirmed unless the items say it is. Something that still needs them is not "handled".
- Most texts ask nothing: they hand over a finished fact or finished work and stop. No offers of more work ("want me to", "I can", "let me know if") and no stock closing line such as "Nothing to do."
- A question is only for a real decision that is theirs: money, a message to someone outside their team, a calendar change, a delete, or a fact only they know. Then: one plain question they can answer yes or no, the reason in the same breath, your pick in a few words. Never give them a word to type. Two or more decisions: number them, give your pick on each, and stop.
- A check on a sign-in or an access they may have made themselves is one short question with the device, place and time the notice gives, and nothing else: "Google new-device login at 5:19pm from Denver. You?" What to do if it was not them waits until they say no.
- If a "decision" has nothing behind it for you to send, buy, book, change or cancel on their yes (what is prepared says nothing is drafted or booked), it is a suggestion, not a decision: say it as your suggestion in one sentence and ask for nothing.
- If a step is theirs alone (a sign-in, a click in their own account), say that one is theirs and give the single step. Do not ask for a go-word you cannot act on.
- If a draft is prepared, give the gist of what it says and everything it commits them to (prices, quantities, dates, promises). Never ask them to approve words they have not seen.
- If they already settled it, in your recent texts or in the items (they chose it, they said it was them, they said leave it), do not raise it again.
- Do not restate a bare calendar fact they already have: add something or stay silent.
- Keep it under {limit} characters. Shorter is better.{rules}
Silence is a real answer, in these cases only: a re-check that found nothing changed; something they already heard in your recent texts; something they have already taken care of themselves; a follow-up whose "tell them if" did not happen; routine upkeep that went as expected; a promise of theirs or a missed ask where nothing is prepared for them to say yes to; or when all you have to report is your own process. Then reply exactly [SILENT]. Never be silent on a real decision, a deadline they must meet, or news they would be worse off not hearing.
"""


def word_budget(items: list) -> int:
    """About how many words this message should run: about 25 words (~140 characters) for the main thing, a short
    clause for each other one. Something that needs the owner (a decision, a fact only they have, work that could not
    be finished) gets room for its numbers."""
    n, total = len(items), 0
    for it in items:
        needs_owner = isinstance(it, dict) and bool(it.get('decision_for_owner') or it.get('result') == 'blocked' or it.get('prep'))
        if needs_owner:
            ready = str((it.get('decision_for_owner') or {}).get('ready') or '')
            total += 45 if len(ready) > 150 else 35      # a prepared draft: its gist and what it commits the owner to
        else:
            total += 25 if not total else 8               # the main thing ~25 words (~140 characters), each other one a clause
    return min(total, 70)                                 # about 400 characters, whatever the count


VOICE_FILE = Path(os.environ.get('LIFE_VOICE_EXAMPLES') or str(settings.LIFE_HOME / 'voice-examples.json'))


def voice_examples() -> str:
    """A few real texts the owner rates highly (voice-examples.json in the life folder, {"examples": [{"text": ...}]}),
    so the writer hears the register instead of following rules about it. They live in the owner's own private folder,
    never in this code. No file means no examples: the rules still apply."""
    try:
        rows = json.loads(VOICE_FILE.read_text(encoding='utf-8'))['examples']
        texts = [' '.join(str(r['text']).replace('[link]', '').split()) for r in rows if isinstance(r, dict) and str(r.get('text') or '').strip()][:8]
    except (OSError, ValueError, KeyError, TypeError):
        return ''
    if not texts:
        return ''
    return ('How the texts the owner likes best sound. These are real ones another assistant sent them. Match the register: how short they '
            'are, how they open on the fact or on the question, how they just stop. Never reuse their facts, names or wording.\n'
            + '\n'.join('- ' + t for t in texts) + '\n\n')


PREP_FAILED = 'You tried to work on this in the background and could not finish, so all you know is what you noticed. Say that the way a person would, in a few words of your own.'
PREP_NONE = 'You did no background work on this one. You are only passing on what you noticed.'


def plain_prep(why) -> str:
    """The worker's reason for "no prep", as one plain sentence. The raw reason carries machine words (failed twice,
    timed out, budget, switched off) that must never reach the text, and "no work was needed" must never be written
    up as "I tried and could not"."""
    failed = re.search(r"fail|could ?n.t|could not|timed? ?out|error|crash|did not run|didn.t run|stall", str(why), re.I)
    return PREP_FAILED if failed else PREP_NONE


def item_view(key: str, opp: dict) -> dict:
    p = opp.get('packet') or {}
    v = {'item': opp.get('title'), 'urgency': opp.get('urgency'), 'result': p.get('status'), 'headline': p.get('headline'),
         'details': p.get('details')}
    if p.get('decision'):
        v['decision_for_owner'] = p['decision']
    if p.get('followups'):
        v['followups'] = [{'when': f.get('when'), 'check': f.get('check')} for f in p['followups']]
    if p.get('prep'):
        v['prep'] = plain_prep(p['prep'])
    for k in ('deadline', 'event_start'):
        if opp.get(k):
            v[k] = opp[k]
    noticed = ' '.join(str(opp.get('text') or '').split())      # only what was noticed, never the plan for it
    if noticed and noticed != ' '.join(str(p.get('headline') or '').split()):
        v['noticed'] = noticed[:400]     # the facts as first noticed stay in front of the writer, so a job that could not re-read them cannot lose the place and the time
    return {k: x for k, x in v.items() if x not in (None, '', [])}


def _ask_zeus(prompt: str, timeout: int = 240) -> str | None:
    """One quiet single-question turn as the real Zeus, in a side session tagged 'tool'. Returns its text or None."""
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', suffix='.txt', prefix='life-compose-', delete=False) as fh:
        fh.write(prompt)
        path = fh.name
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith('LIFE_')}
        env.setdefault('HOME', str(settings.HOME))
        env['LIFE_COMPOSE'] = '1'   # tells the life-items chat hook to stay out of this side session
        p = subprocess.run([HERMES, '-p', settings.PROFILE, 'chat', '-Q', '--source', 'tool', '--max-turns', '3', '--query-file', path],
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env, cwd='/private/tmp')
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        Path(path).unlink(missing_ok=True)
    if p.returncode != 0:
        return None
    lines = [ln for ln in p.stdout.splitlines() if not ln.strip().lstrip('↻ ').startswith(('session_id:', 'Resumed session'))]
    text = '\n'.join(lines).strip()
    return text or None


def clean(text: str) -> str:
    t = str(text or '').strip()
    t = re.sub(r'^```[a-z]*\n|\n```$', '', t).strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in '"“':
        t = t[1:-1].strip()
    t = re.sub(r'\s*—\s*|\s+–\s+', ', ', t)   # house style: no em dashes
    return re.sub(r'\n{3,}', '\n\n', t)


CHECK_SYSTEM = """You are the second reader. An assistant is about to text its owner. You get the work results the text
was written from ("results") and the text. Find claims in the text that the results do not support. Be exact and
fair: paraphrase is fine, a reformatted date or time is fine, leaving facts out is fine, and a plain recommendation
("I'd send it") is fine. Report a problem only when it is clearly one of these:
1. A fact (number, time, date, name, place, consequence) that is not in the results, or is attached to a different
   thing than in the results. The text may say "today", "tomorrow" or a weekday where the results give a date: work
   out which date that word means from "now", and report a wrong day as a wrong fact.
2. Something said more firmly than the results say it: what the results call possible, unconfirmed, or true of a
   model, a range or a group is written as a settled fact about the owner's own thing; or the text assumes the answer
   to something it asks the owner for.
3. Something said to be done, sent, booked, paid, ordered, cancelled, confirmed or under way that the results do not
   say is so. A result with "prep" means the background work did not happen: the text must not hide that.
4. A promise to check or do something later that is not in that result's "followups", or at a different time.
5. Asking permission to do free work, or offering extra work ("want me to", "I can also", "let me know if").
6. Blind approval: the text asks the owner to approve sending a prepared draft (a reply, a quote, a statement, a note) but
   says nothing of what the draft says. One main point of the draft, in any words, is enough to pass; a draft that
   is itself only a line or two of logistics passes when the text gives those logistics. Quote the sentence that asks.
For each problem give the exact words from the text and one plain sentence saying what the results actually say (for
6: the main points the draft makes).
An empty list is the normal answer."""
CHECK_SCHEMA = {'type': 'object', 'required': ['problems'], 'additionalProperties': False, 'properties': {'problems': {'type': 'array', 'items': {
    'type': 'object', 'required': ['quote', 'why'], 'additionalProperties': False,
    'properties': {'quote': {'type': 'string'}, 'why': {'type': 'string'}}}}}}

FIX = """[Life pipes, fix step. This comes from your own background work, not from {owner}. Do not use tools, do not send anything. Reply with only the final message.]

A second reader checked your text against the results and found these problems:
{problems}

Results (JSON):
{items}

Your text:
{draft}

Fix exactly these problems and change nothing else. Where the text claims more than the results say, remove the claim or say it the way the results say it (keep "can", "may", "not confirmed"). Where it asks them to approve a draft without saying what the draft says, add one sentence with the draft's main points. Add nothing else. Keep your voice and stay under {limit} characters."""


def _check_facts(items: list, text: str, now_words: str = ''):
    """Independent second reader: a different model family than the writer, through the noticer's own headless
    route. -> list of {'quote','why'} (empty = clean), or None when the checker could not be reached."""
    try:
        import noticer as N
        res = N.complete(CHECK_SYSTEM, json.dumps(({'now': now_words} if now_words else {}) | {'results': items, 'text': text},
                                                  ensure_ascii=False, default=str), CHECK_SCHEMA, timeout=120)
    except Exception:
        return None
    if not res or not isinstance(res.get('out'), dict):
        return None
    probs = res['out'].get('problems')
    if not isinstance(probs, list):
        return None
    out = []
    for pr in probs[:6]:
        if isinstance(pr, dict) and str(pr.get('quote') or '').strip():
            out.append({'quote': str(pr['quote'])[:240], 'why': str(pr.get('why') or '')[:300]})
    return out


CLAIMS_SYSTEM = """You check a text message against the work results it was written from. Go through the text one claim at a
time. A claim is anything a reader would take as fact: who did or will do something, whose something is, how much,
when, where, what happens if.
For every claim, find the place in the results that supports it, then judge only these three things:
- who: is the person, company or thing in the claim the same one the results name for it? A crew, a card, a deadline
  or an action that the results give to one party must not be given to another. That holds for "their", "his"
  and "her" too: where the results do not say that something belongs to another person or company they name
  ("the truck is booked"), the text must not hand it to them ("their truck"). "I" and "my" for the work these
  results report, and "you" and "your" for the reader's own things, are fine.
- when: is the day and time the one the results give for THAT event? "today", "tomorrow", "yesterday" and weekday
  names are counted from "now". A date that belongs to a different event in the results is wrong, even though the
  date itself appears there.
- what: is the amount, number, place or consequence the one the results give for that thing?
Paraphrase is fine, a reformatted date or time is fine, leaving things out is fine, and a recommendation ("I'd send
it") is not a claim. Return every claim that fails: the exact words from the text, which of who / when / what failed,
and one sentence saying what the results actually say. An empty list is the normal answer."""
CLAIMS_SCHEMA = {'type': 'object', 'required': ['problems'], 'additionalProperties': False, 'properties': {'problems': {'type': 'array', 'items': {
    'type': 'object', 'required': ['quote', 'kind', 'why'], 'additionalProperties': False,
    'properties': {'quote': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['who', 'when', 'what']}, 'why': {'type': 'string'}}}}}}


def _same_claim(a: str, b: str) -> bool:
    """Do two readers mean the same words of the text? One quote inside the other, or a shared run of 12 characters."""
    a, b = ' '.join(str(a).lower().split()), ' '.join(str(b).lower().split())
    if not a or not b:
        return False
    return a in b or b in a or any(a[i:i + 12] in b for i in range(max(1, len(a) - 11)))


def _check_claims(items: list, text: str, now_words: str = ''):
    """The second independent reader: claim by claim, who / when / what. Two passes, and only what BOTH passes flag
    counts (one pass alone flags a clean note about as often again). -> list of {'quote','why'} (empty = clean), or
    None when the reader could not be reached twice."""
    try:
        import noticer as N
        from concurrent.futures import ThreadPoolExecutor
        prompt = json.dumps(({'now': now_words} if now_words else {}) | {'results': items, 'text': text}, ensure_ascii=False, default=str)
        with ThreadPoolExecutor(2) as ex:
            got = list(ex.map(lambda _i: N.complete(CLAIMS_SYSTEM, prompt, CLAIMS_SCHEMA, timeout=120), range(2)))
    except Exception:
        return None
    passes = []
    for g in got:
        probs = g['out'].get('problems') if g and isinstance(g.get('out'), dict) else None
        if not isinstance(probs, list):
            return None
        passes.append([pr for pr in probs[:8] if isinstance(pr, dict) and str(pr.get('quote') or '').strip()])
    out = []
    for pr in passes[0]:
        if any(q.get('kind') == pr.get('kind') and _same_claim(pr['quote'], q['quote']) for q in passes[1]):
            out.append({'quote': str(pr['quote'])[:240], 'why': str(pr.get('why') or '')[:300]})
    return out[:4]


def _check_all(items: list, text: str, now_words: str = ''):
    """Both independent readers. -> what either found (the claim reader's only where its two passes agree), or None
    when neither could be reached. A text is never held for them: the caller sends it either way."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:                     # side by side: a note waits for the slower reader, not for both
        first, second = ex.submit(_check_facts, items, text, now_words), ex.submit(_check_claims, items, text, now_words)
        whole, claims = first.result(), second.result()
    if whole is None and claims is None:
        return None
    out = list(whole or [])
    for c in claims or []:
        if not any(_same_claim(c['quote'], w['quote']) for w in out):
            out.append(c)
    return out[:6]


def compose(items: list, now_dt: datetime, recent: list, *, ask=None, limit: int = LIMIT, check=None, rules: str = '', must_say=None) -> dict:
    """-> {'text': str | None, 'silent': bool, 'violations': [..], 'tries': n, ...}. text None = the model call failed.
    ask = the writer (default: the real Zeus). check = the independent fact check (default: the real one when the
    real writer is used; none when a test passes its own writer).
    rules: one more writing rule from the caller (fresh.composing); '' leaves the prompt exactly as it was.
    must_say: the caller's check of facts the text must carry (fresh.owed_missing): a list of {'quote', 'why'}, one per
    fact missing. Run after the independent readers; what it finds gets ONE targeted fix through the same FIX step."""
    if check is None:
        check = ((lambda _items, _text: _check_all(_items, _text, __import__('ownertime').clock(now_dt, '%A %b %d %Y, %I:%M%p Pacific')))
                 if ask is None else (lambda _items, _text: []))
    ask = ask or _ask_zeus
    items = [{**it, 'prep': plain_prep(it['prep'])} if isinstance(it, dict) and it.get('prep') else it for it in items]
    n = len(items)
    limit = min(limit, LIMIT)   # one limit whatever the item count; a second item is a clause, not a paragraph
    shown = json.dumps(items, ensure_ascii=False, indent=1, default=str)
    now_words = __import__('ownertime').clock(now_dt, '%A %b %d %Y, %I:%M%p Pacific').replace(' 0', ' ')
    prompt = PROMPT.format(now=__import__('ownertime').clock(now_dt, '%A %b %d, %I:%M%p Pacific').replace(' 0', ' '), items=shown,
                           recent=json.dumps([str(r)[:500] for r in recent][-6:], ensure_ascii=False, indent=1) if recent else '(nothing)',
                           limit=limit, voice=voice_examples(), rules=rules, owner=settings.OWNER_FIRST)
    out = {'text': None, 'silent': False, 'violations': [], 'tries': 0}
    draft = ask(prompt)
    out['tries'] = 1
    if draft is None:
        return out
    draft = clean(draft)
    if draft.upper().startswith(SILENT):
        out['silent'] = True
        return out
    # ONE writer call, then code's checks and the independent reader, then at most ONE targeted rewrite. A chain of
    # review, shorten and no-question passes is slow and keeps the hedges each pass is told to keep. The independent
    # reader checks every claim (facts, and nothing called done that is not).
    budget = word_budget(items)

    def issues(text: str) -> list:
        found = [{'quote': v, 'why': 'this breaks a writing rule: take it out, and say the thing itself instead'} for v in found_in(text, items)]
        if len(text.split()) > budget * 1.3:
            found.append({'quote': ' '.join(text.split()[:8]), 'why': f'the text runs {len(text.split())} words and should be about {budget} or fewer: keep the '
                                                                       'fact, every number the owner decides on and the real deadline, and cut the rest'})
        return found

    problems = check(items, draft)
    out['checked'] = problems is not None
    todo = issues(draft) + list(problems or [])
    if todo:
        out['check_found'] = todo
        fixed = ask(FIX.format(problems='\n'.join(f'- "{pr["quote"]}": {pr["why"]}' for pr in todo), items=shown, draft=draft, limit=limit, owner=settings.OWNER_FIRST))
        out['tries'] += 1
        fixed = clean(fixed) if fixed else ''
        if fixed and not fixed.upper().startswith(SILENT) and len(fixed) <= max(limit + 150, len(draft) + 200):
            # the rewrite is taken when it breaks no more writing rules AND the reader finds less wrong with it.
            # Running long is never a reason to send the text the reader flagged.
            left = check(items, fixed) if out.get('checked') else None      # every rewrite is read again, also one made only for a writing rule or for length
            if len(found_in(fixed, items)) <= len(found_in(draft, items)) and (not left or len(left) < len(problems or [])):
                draft = fixed
                out['fixed'] = True
                if left is not None:
                    out['check_left'] = left
    def plain_headline() -> str:
        """The item's own headline as the text of last resort, screened like any text: no question when
        nothing is the owner's to decide, nothing about the owner in the third person, no process talk, no banned phrase."""
        head = fallback_text(items)
        if head and ASK_FREE in found_in(head, items):
            head = cut_questions(head)
        if not head or found_in(head, items) or re.match(r'\s*(?:He|She|%s)\b' % re.escape(settings.OWNER_FIRST), head) or re.search(r'\bNeeds your call:', head):
            return ''
        return head

    # Last resort, by code, so the words that go out never carry these two: a question when nothing is theirs to decide,
    # and talk about the assistant's own process.
    if ASK_FREE in found_in(draft, items):
        draft = cut_questions(draft) or draft
        out['question_removed'] = True
    if process_talk(draft):
        plain = cut_process(draft)
        if not plain:                      # every sentence was about its own process. The item's own headline, when that is a plain fact, goes instead of silence
            head = plain_headline()
            if head:
                plain, out['fell_back_to_headline'] = head, True
        if not plain:
            out['silent'] = True
            out['silenced_by'] = 'it had nothing but its own process to report'
            return out
        draft = plain
        out['process_removed'] = True
    if (out.get('question_removed') or out.get('process_removed')) and out.get('checked'):
        out['check_left'] = check(items, draft)      # the words that go out are the words the reader saw
        if out['check_left'] and not out.get('fell_back_to_headline'):
            head = plain_headline()                  # the reader objects to what the cut left; the plain headline goes instead, when there is one
            if head:
                draft, out['fell_back_to_headline'] = head, True

    def acceptable(new: str, old: str) -> bool:        # used by the caller's own fact check just below
        return len(found_in(new, items)) <= len(found_in(old, items)) and len(new) <= max(limit + 150, len(old) + 200)
    if must_say is not None:
        # Facts code owes the owner (fresh.py): the writer says them, code checks; one targeted fix for what is missing. What
        # is still missing after it, the caller adds as code's own sentence. Never both.
        left = must_say(draft)
        if left:
            out['must_say_found'] = len(left)
            fixed = ask(FIX.format(problems='\n'.join(f'- "{pr["quote"]}": {pr["why"]}' for pr in left), items=shown, draft=draft, limit=limit, owner=settings.OWNER_FIRST))
            out['tries'] += 1
            fixed = clean(fixed) if fixed else ''
            if fixed and not fixed.upper().startswith(SILENT) and acceptable(fixed, draft) and len(must_say(fixed)) < len(left):
                draft = fixed
                out['must_say_fixed'] = True
                left = must_say(draft)
        out['must_say_left'] = len(left)
    out['text'] = draft
    out['violations'] = found_in(draft, items)
    return out


def fallback_text(items: list) -> str:
    """Only when the composer could not be reached three ticks in a row: plain facts, no offers, nothing invented."""
    parts = []
    for it in items:
        line = str(it.get('headline') or it.get('item') or '').strip()      # the headline only: "details" are notes for the writer, never text for the owner
        d = it.get('decision_for_owner')
        if d and d.get('ask'):
            line += ' Needs your call: ' + d['ask']
        parts.append(line)
    return '\n\n'.join(p for p in parts if p)

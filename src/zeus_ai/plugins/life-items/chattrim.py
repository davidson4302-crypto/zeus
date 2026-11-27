"""chattrim: answer what the owner said, then stop.

Why. Replies that answer correctly often keep going: how the agent knew something (an order date, how long a box
usually lasts, what an email said), its own bookkeeping ("I haven't marked it done", "I won't say it's on until it
is"), the backstory of a guess the owner had just corrected, another waiting thing raised right after "Sent", and
generic options in front of the one pick. The plan (chatplan.py) already decides what a reply covers on the kinds of
message it knows; this is the same idea after the reply, for every message in the owner's chat, in code and without a
model call: sentences that bring up something the owner did not ask about are cut, and the answer stays.

  trim(reply, message, items, now, P, plan=None, answer=False, soon=None, named_message=None, status=False)
      -> (the new reply, [{'reason': 'trim: ...', 'sentence': ...}]) or (None, []) when nothing changes

What goes (each is a family of patterns, never one example's wording):
  outside   a sentence about an open thing outside the plan (and outside what the owner's message names), including
            one right after an answer line ("Sent."), and an ask or a draft that only follows such a sentence
  how       how the agent knows: when something was last bought, how long it usually lasts, "the email only said ...
            so I", "I keep an eye on your orders", "I read it at 9:40". Not when the owner asks how or why.
  status    its own bookkeeping, always ("marked it done", "I won't say it's set until", "nothing gets bought without
            your yes"); plain not-done lines nobody asked about ("I haven't ordered anything", "nothing is set yet");
            and work in progress nobody asked about ("I'm also working on ..., you'll hear at 3")
  backstory after a correction: why it had guessed wrong ("I'd guessed the younger one because ...")
  options   for ideas, when the reply makes a pick: the generic alternatives before or after it
  after     when the owner asked for the words to send: everything after the first draft (why it works, a remark
            about the person, a second version, an offer to send it), except a sentence naming a planned thing or a
            question due before the owner's next morning text

On a turn code could not place (the menu turn), a reply that talks about two or more of the things in code's own
whole-picture plan followed that plan: everything in it is inside the plan, the rest is still outside.

What never goes: the first sentence; a quoted draft (four words or more inside quotes, or words to send); a question
the owner must answer about a planned thing or one due before their next morning text; a sentence that names a planned
thing; and exactly one plain not-done line when that is the truthful answer (the owner said yes to a step, asked about
it, or asked for something to be done). If what is left would not make sense (only a greeting word, or a few words
where the original answered something), the original stays.

status=True means the plugin's status answer fired ("what do you do for me on your own?"). There the agent's sentences
about its own duties are the answer, so the bookkeeping and how-it-knows cuts stand down on that turn only; everything
else above is unchanged. It rides on the switch status-answer-on.

Standard library only (plus the engine's settings for the owner's name). Nothing here reads or writes a file; the
plugin logs what was cut.
"""
from __future__ import annotations

import importlib.util
import re
import sys
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

# ---- sentences, with quotes and emoji kept whole ---------------------------------------------------------------------
EMOJI = re.compile('[←-⇿⌀-➿⬀-⯿️\U0001f000-\U0001faff]')
ABBREV = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|vs|Mt|Ave|approx|e\.g|i\.e)\.$")


def sentences(line: str) -> list:
    """One line -> its sentences. A quote is never split ("We're in! See you Saturday." stays one piece with what
    surrounds it), "Ms." does not end a sentence, and an emoji followed by a capital ends one ("Welcome back 🙂 One ...")."""
    out, cur, inside = [], '', False
    text = str(line or '')
    for i, ch in enumerate(text):
        cur += ch
        if ch == '"':
            inside = not inside
        elif ch == '“':
            inside = True
        elif ch == '”':
            inside = False
        if inside:
            continue
        rest = text[i + 1:]
        if rest and not rest[0].isspace():
            continue
        ended = ch in '.!?' or (ch in '"”)' and len(cur) >= 2 and cur[-2] in '.!?')
        if ch == '.' and ABBREV.search(cur):
            ended = False
        if not ended and EMOJI.match(ch):
            nxt = rest.lstrip()[:1]
            ended = bool(nxt) and (nxt.isupper() or nxt in '"“')
        if ended and cur.strip():
            out.append(cur.strip())
            cur = ''
    if cur.strip():
        out.append(cur.strip())
    return out


# ---- what a sentence is ----------------------------------------------------------------------------------------------
QUOTE = re.compile(r'"([^"]*)"|“([^”]*)”')
DRAFT_CUE = re.compile(r"(?:\b(?:try|send|say|text|reply|write|draft(?:ed)?|version|something like|like this|here'?s)\b[^\"“]{0,30}|:\s*)[\"“]", re.I)
ASKS = re.compile(r"\?[\"”)]?\s*$|\bsay (?:yes|the word|so)\b|\b(?:want|would you like) me to\b|\b(?:ok|okay) (?:to|if i)\b|"
                  r"\bshould i\b|\bshall i\b|\byes or no\b", re.I)

# how the agent knows
HOW = re.compile(
    r"\blast (?:box|bag|bottle|pack|packet|order|refill|one|batch|delivery|tube|jar|case|carton|roll|set|pair|tub|can|shipment)\b[^.!?]{0,50}?\b(?:was|were|came|arrived|got)\b|"
    r"\b(?:bought|ordered|purchased|refilled|picked up)\b[^.!?]{0,14}?\b(?:on )?(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.? \d|\d{1,2}/\d{1,2}\b|(?:\w+ )?(?:weeks?|months?|days?) ago\b)|"
    r"\b(?:usually|typically|normally|tends? to|generally|often) (?:lasts?|runs? out|goes|empties|gets used up)\b|\blasts? (?:you )?(?:about|around|roughly|almost) (?:a|an|\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b|"
    r"\bruns? out (?:every|after|about)\b|"
    r"\b(?:only|just) (?:said|says|mentioned|listed|showed)\b[^.!?]{0,60}?\bso (?:i|we)\b|\b(?:didn'?t|did not|never) (?:say|said|mention|specify|name)\b|"
    r"\bi (?:just )?(?:keep|kept) (?:an eye on|track of|tabs on)\b|"
    r"\bi (?:track|tracked|watch|monitor|monitored|noticed|saw|spotted|checked|read|pulled|went through) (?:it )?(?:in |on |from |through )?(?:your|the) (?:orders?|order history|purchases?|receipts?|e-?mails?|inbox|mail|texts?|messages|calendar|account|statements?)\b|"
    r"\bi (?:read|checked|saw|looked at|pulled|grabbed) (?:it|that|this|the (?:page|site|email|listing|post))\b[^.!?]{0,12}?\b(?:at \d|this morning|earlier|yesterday|today at)\b|"
    r"\b(?:because|since|as) i (?:saw|noticed|read|spotted|checked|found|keep|track|have)\b|"
    r"\bthat'?s how i (?:knew|know|found|figured|spotted|caught)\b|\bwhich is how i\b|"
    r"\bbased on (?:your|the) (?:order|orders|purchase|purchases|last|past|history|usual|receipts?)\b|\bfrom your (?:order|purchase|buying) history\b", re.I)
ASKS_HOW = re.compile(r"\bhow (?:did|do|would|could|can) you (?:know|tell|figure|find|see|get|guess|spot)|\bwhy\b|\bwhat made you\b|\bhow come\b|"
                      r"\bwhere did you (?:see|get|find|read|hear)|\bsource\b|\bsays who\b|\bhow (?:do|did) you know\b|\bexplain\b")

# its own bookkeeping: never what the owner needs to hear
BOOKKEEPING = re.compile(
    r"\b(?:marked|mark|logged|log|recorded|filed|ticked|checked) (?:it|that|this|them) (?:off |as )?(?:done|complete|completed|finished|closed|off)\b|"
    r"\bi(?:'m| am) not (?:going to|gonna) (?:tell|say|claim|call|pretend)\b|"
    r"\bi won'?t (?:say|tell you|claim|call it|mark|pretend)\b[^.!?]{0,50}?\b(?:until|unless|when|till|before)\b|"
    r"\b(?:nothing|it|that) (?:gets|will get|will be|is|goes) (?:bought|ordered|sent|booked|paid|charged|placed|out)\b[^.!?]{0,25}?\bwithout your\b|"
    r"\b(?:tell|say|claim) (?:you )?(?:it'?s|it is|that it'?s) (?:set|on|done|sent|booked)\b[^.!?]{0,20}?\b(?:when it|until it|unless it)\b", re.I)
# a plain not-done line about its own work
NOT_DONE = re.compile(
    r"\bi (?:haven'?t|have not|didn'?t|did not) (?:yet )?(?:ordered|order|bought|buy|booked|book|sent|send|changed|change|touched|done|paid|placed|set|switched|"
    r"turned|marked|started|signed|replied|answered|confirmed|scheduled|made|locked|picked|reached)\b|"
    r"\b(?:nothing|none of it|nothing'?s)(?:'s| is| has been| was| got)? (?:been )?(?:ordered|bought|booked|sent|set|locked|changed|running|placed|paid|scheduled|"
    r"confirmed|done|final|charged)(?: in| up)?\b|"
    r"\b(?:isn'?t|is not|aren'?t|are not|wasn'?t|not|weren'?t) (?:switched |turned )?(?:on|set|set up|live|running|done|booked|sent|ordered|placed|active|working) yet\b|"
    r"\b(?:no|nothing) (?:exact |firm )?(?:date|time|booking|order)'?s? (?:is )?(?:set|locked|booked|placed) yet\b|"
    r"\bi (?:can'?t|cannot|couldn'?t|could not|don'?t have (?:a )?way to|have no way to|am not able to|'m not able to|wasn'?t able to) (?:reach|get to|switch|turn|change|access|do|set|get into|open|log)\b", re.I)
# work in progress, offered when nobody asked
PROGRESS = re.compile(
    r"\bi(?:'m| am) (?:also |still |now )?(?:working on|looking into|digging into|comparing|chasing|sorting(?: out)?|lining up|picking(?: out)?|finding|hunting|"
    r"redoing|checking on|on the|on it|tracking down|pricing|getting)\b|\bi(?:'ll| will) (?:have|get) (?:a|an|the|that)\b[^.!?]{0,40}?\b(?:for you|to you)\b|"
    r"\b(?:still|also) (?:working|comparing|looking|checking)\b", re.I)
HEARS = re.compile(r"\byou(?:'ll| will) hear\b|\bi(?:'ll| will) (?:text|tell|update|ping|message|get back to|have)\b|\bhear (?:from me|back)\b", re.I)
# a guess the owner just corrected
BACKSTORY = re.compile(
    r"\bi(?:'d| had| was|'ve| have)? (?:guessed|assumed|thought|figured|went with|read it as|took it as|misread|mixed (?:them|it|that|those) up|had it as|put down)\b|"
    r"\bmy (?:guess|assumption|mistake) was\b|\bthat'?s why i\b|\b(?:only|just) said\b[^.!?]{0,40}?\bso i\b|\bwhich is why i\b", re.I)
CORRECTION = re.compile(r"\bactually\b|\bi meant\b|\bthats wrong\b|\bcorrection\b|\bwrong (?:one|day|time|date|person|kid|boy|girl|place|name|guy)\b|"
                        r"\bnot (?:the|that|this|my|his|her|\d)\b|\b\d[\d:apm ]* not\b|^no\b.{0,40}\b(?:its|it is|the)\b")
# CORRECTION reads the message with its commas gone, so "X, not Y" (the commonest way to put a correction) would never
# match there. CONTRAST reads the owner's message as written.
CONTRAST = re.compile(r"[a-z0-9],\s*not\s+(?:a\s+|an\s+|the\s+)?[a-z0-9]|^\W*not\s+[a-z0-9]+\s*,")
# for ideas: a pick, and the options around it
PICK = re.compile(r"\b(?:my |top |best |the )?pick\b|\bi'?d (?:go with|pick|get|choose|do|take|book)\b|\bmy (?:vote|choice|recommendation|money)\b|"
                  r"\bbest bet\b|\bgo with\b|\bi'?d (?:say|suggest)\b", re.I)
PICK_BY_NUMBER = re.compile(r"\bpick\b[^.!?]{0,15}#?\b(?:\d|one|two|three|first|second|third)\b", re.I)
ALTERNATIVE = re.compile(r"\b(?:another|other|second) (?:option|idea|choice|pick)s?\b|\balternatively\b|\byou could also\b|\bor (?:you could|maybe|if)\b|"
                         r"\bif (?:she|he|they|you)(?:'d| would) rather\b|\brunner[- ]up\b|\bbackup (?:idea|option|pick|plan)\b|\bcould also\b", re.I)
SUGGEST_START = re.compile(r"^\W*(?:(?:you could|maybe|or|and|also|just)\W+)*(?:get|grab|try|go|do|take|book|bring|buy|make|consider|think about|order|pick up|plan|head)\b", re.I)
QUESTIONISH = re.compile(r"\?|^(?:(?:so|ok|okay|and|hey|yo|alright)\s+)?(?:what|whats|when|where|wheres|why|how|howd|hows|who|whos|which)\b|"
                         r"\b(?:anything|everything|any news|updates?|status|latest|scoop|moving|happening|going on|shake out|catch (?:me|us) up|fill (?:me|us) in)\b")
ASKS_STATUS = re.compile(r"\b(?:did|have|has) (?:you|it|that|she|he|they)\b|\bis (?:it|that|the \w+) (?:on|set|done|sent|booked|ordered|in|live)\b|"
                         r"\bany (?:word|news) on\b|\bwhere (?:are we|is it|is that) on\b|\bstatus\b|\bdid it go\b")
DO_REQUEST = re.compile(r"\b(?:remind|send|book|order|pay|buy|cancel|reply|forward|schedule|move|tell|text|email|call|ask|check|find|look|watch|lmk|"
                        r"let me know|can you|could you|would you|will you|please|pls|keep an eye|track|set up|sign up|switch|turn on|turn off)\b")
# The owner asked for the words to send: the plan for such a request is the draft, and what follows it (why it works, a
# remark about the person, a second version, an offer to send it) is outside the plan. Read on the owner's message,
# normalised.
WORDING = re.compile(r"\bhow (?:should|do|can|would|could|to) (?:i |we )?(?:word|say|ask|phrase|put|tell|text|write|answer|reply|"
                     r"respond|approach|bring up|raise|nudge|remind|chase|follow up|let \w+ know|break (?:it|the news)|turn \w+ down|"
                     r"turn down|decline|thank|apologi[sz]e)\b|\bwhat (?:should|do|can|could|would) (?:i|we) (?:say|write|text|tell|"
                     r"reply|answer|put)\b|\bhelp me (?:word|write|phrase|reply|respond|answer|draft|say)\b|"
                     r"\b(?:word|phrase|draft|write) (?:a|an|the|this|my|me a|up a) (?:text|message|note|email|reply|card|response)\b|"
                     r"\bwithout (?:it )?(?:sounding|being|coming across|seeming)\b|\bw o sounding\b")
AFTER_DRAFT = 'trim: words after the draft the owner asked for'
ACK_WORDS = frozenset('ok okay k got it sure yes yep yeah no nope thanks thank you ty cool great perfect nice noted done sent on '
                      'hi hey hello morning night welcome back home good ha haha lol'.split()
                      # the owner's own names count as small words ("thanks, <name>"); owner.nicknames is optional
                      + [w for w in ' '.join([str(settings.OWNER_NAME or '')] + [str(n) for n in (settings.get('owner.nicknames', []) or [])])
                         .lower().split() if w not in ('the', 'owner')])


def _quotes(s: str) -> list:
    return [(m.group(1) if m.group(1) is not None else m.group(2)) for m in QUOTE.finditer(s)]


def quoted_draft(s: str) -> bool:
    """A sentence carrying words to send or say: four words or more inside quotes, or a quote introduced as words."""
    qs = _quotes(s)
    return bool(qs) and (any(len(q.split()) >= 4 for q in qs) or bool(DRAFT_CUE.search(s)))


def _drafted_words(it: dict) -> set:
    return set(re.findall(r"[a-z]{4,}", str(it.get('prepared') or '').split(':', 1)[-1].lower()))


def _is_draft_of(s: str, it: dict) -> bool:
    """Is the quote in this sentence that thing's own prepared draft (most of its words)?"""
    drafted = _drafted_words(it)
    if not drafted:
        return False
    said = set(re.findall(r"[a-z]{4,}", ' '.join(_quotes(s)).lower()))
    return len(drafted & said) >= max(2, (len(drafted) + 1) // 2)


def _only_small_words(text: str) -> bool:
    words = re.findall(r"[a-z]+", text.lower().replace("'", ''))
    return not words or all(w in ACK_WORDS for w in words)


# ---- the trim -----------------------------------------------------------------------------------------------------------
def trim(reply, message, items, now, P, plan=None, answer=False, soon=None, named_message=None, status=False, about_keys=(),
         wants_followup=False):
    """-> (the new reply, the cuts) or (None, []). See details() for the arguments."""
    out, cuts, _ = details(reply, message, items, now, P, plan, answer, soon, named_message, status, about_keys, wants_followup)
    return out, cuts


PROMISE = "trim: a promise in the owner's name they never made"


def details(reply, message, items, now, P, plan=None, answer=False, soon=None, named_message=None, status=False, about_keys=(),
            wants_followup=False):
    """When the owner asks for words to send, each quoted draft first loses any offer the owner never made
    (chatplan.drop_unoffered); then the trim below runs on what is left."""
    text = str(reply or '').strip()
    gone = []
    t = P.norm(message) if text else ''
    if t and WORDING.search(t) and not ASKS_HOW.search(t) and hasattr(P, 'drop_unoffered'):
        fixed, gone = P.drop_unoffered(text, str(message or ''))
        text = fixed or text
    out, cuts, kept = _details(text, message, items, now, P, plan, answer, soon, named_message, status, about_keys, wants_followup)
    if not gone:
        return out, cuts, kept
    return (out or text), [{'reason': PROMISE, 'sentence': g} for g in gone] + (cuts if out else []), False


def _details(reply, message, items, now, P, plan=None, answer=False, soon=None, named_message=None, status=False, about_keys=(),
             wants_followup=False):
    """-> (the new reply or None, the cuts, kept_original: True when cuts were found but what was left would not make
    sense, so the original stays). P = chatplan (its readers of open things are reused, so a sentence
    is "about" a thing by exactly the same test plan_guard uses). plan = the plan the turn was given (or None);
    answer = the owner's message is only an answer word ("yes", "sounds good"); soon(sentence) = the plugin's own test
    for "names a time before the owner's next morning text"; named_message = the owner's message with misspelled known
    names written right; status = the plugin's status answer fired on this turn (the owner asked what the agent does or
    whether it is watching)."""
    text = str(reply or '').strip()
    msg = str(message or '')
    if not text or not msg.strip():
        return None, [], False
    p = plan or {}
    pk = p.get('kind') or ''
    if pk in ('answer-ack', 'answer-which'):
        return None, [], False               # the answer guards own those two shapes
    k = pk if pk and not pk.startswith('answer') else P.kind(msg)
    t = P.norm(msg)
    items = list(items or [])
    by_key = {it['key']: it for it in items}
    due = P.due_soon(items, now) if now is not None else []
    planned = set(p.get('keys') or []) | {it['key'] for it in due}
    if pk == 'answer' and p.get('about'):
        planned.add(p['about']['key'])
    named = named_message if named_message is not None else msg
    subject = {it['key'] for it in items if P._named(named, it, True)}
    # A message of one word ("pull", a typo fix) or three ("the green one") names nothing, yet the sentences that ARE the
    # answer ("It leaves at 8:32am ...", "exchange or store credit only ... by Oct 20") must not be cut as "outside what
    # the owner asked about". about_keys = the things the shared reading of the message (turnctx.py) says it is about,
    # read with the conversation. They are the subject as much as a thing the owner names. Empty (no reading): unused.
    subject |= {k for k in (about_keys or ()) if k in by_key}
    correction = bool(CORRECTION.search(t) or CONTRAST.search(msg.lower().replace('’', "'")))
    asks_how = bool(ASKS_HOW.search(t))
    # Asked "what do you do for me on your own?", the agent's sentences listing its own duties ("nothing gets bought
    # without your yes", "I keep an eye on your email") would be cut as bookkeeping or how-it-knows. On a turn where code
    # handed over the status lines, those sentences ARE the answer: only those two cuts stand down.
    own_work = bool(status)
    asks_status = bool(ASKS_STATUS.search(t))
    # wants_followup: the reading says doing what the owner asks means work after this reply ("shout when it's in the
    # mail"). Then one plain line about what is NOT set up is owed to the owner, as after a request to do something.
    wants_done = bool(DO_REQUEST.search(t)) or k == 'confirm' or bool(wants_followup)
    ideas = k == 'ideas' or bool(re.search(r"\bwhat (?:should|do|can|could) (?:we|i) do\b|\bideas?\b|\bsuggest", t))
    # An open question the plan does not know and that names nothing ("anything moving?", "how'd the day shake out") may
    # well be asking for the whole picture: there, as on a rundown, only how-it-knows and bookkeeping are cut.
    open_question = not pk and k not in P.PLANNED and not answer and not subject and not ideas and not correction \
        and bool(QUESTIONISH.search(t) or DO_REQUEST.search(t))
    whole = k in ('rundown', 'yesno') or open_question

    lines = []                               # [(paragraph index, list marker, [sentences])]
    for pi, para in enumerate(re.split(r'\n\s*\n', text)):
        for line in para.split('\n'):
            lead = re.match(r'^(\s*(?:\d+[.)]|[-•*])\s+)?', line).group(0)
            lines.append((pi, lead, sentences(line[len(lead):])))
    flat = [s for _, _, ss in lines for s in ss]
    if len(flat) < 2:
        return None, [], False
    name_of = {it['key']: names(P, it) for it in items}

    def about(s, keys=None):
        return [it['key'] for it in items if (keys is None or it['key'] in keys) and P._talks_about(s, name_of[it['key']], P.title_words(it))]

    if answer and pk != 'answer':            # a yes with no plan to read: the thing the reply answers is the first one it talks about
        for s in flat:
            first = [kk for kk in about(s) if P.role(by_key[kk]) == 'ask'] or about(s)
            if first:
                planned.add(first[0])
                break
    # When code could not place a short request, the turn carried the menu and the model may pick the whole picture code
    # had built. Guessing by word lists here would cut a second ask or a finished repair. The model judges which part
    # fits; code checks the reply against the part it took: two or more things of code's own whole-picture plan means
    # that plan.
    if not pk and k not in P.PLANNED and not answer and now is not None:
        whole_plan = set(P.plan('rundown', items, now, msg)['keys'])
        if len({kk for s in flat for kk in about(s, whole_plan)}) >= 2:
            planned |= whole_plan
    others = {kk for kk in by_key if kk not in planned and kk not in subject}
    # Outside the plan only means something when the reply's subject is known: a code plan, an answer, a message that
    # names a thing, or a plain statement. And when the plan does not know the message, a reply that brings up two or more
    # pieces of news or asks outside it is taken as a rundown the owner may have wanted: then nothing is cut as "outside".
    outside_known = not whole
    if outside_known and not pk and k not in P.PLANNED and not answer:
        raised = {kk for s in flat[1:] for kk in about(s, others) if P.role(by_key[kk]) in ('news', 'ask')}
        outside_known = len(raised) < 2

    protected_not_done = False
    cuts, keep_flags = [], []
    index = 0
    prev_cut = None                          # (reason, the thing it was about or None) of the sentence just cut, same line run
    for pi, lead, ss in lines:
        flags = []
        for s in ss:
            index += 1
            first = index == 1
            reason = None
            names_planned = bool(about(s, planned))
            asks = bool(ASKS.search(s))
            due_now = bool(soon(s)) if soon is not None else False
            draft = quoted_draft(s)
            outside = about(s, others) if outside_known else []
            if outside and draft and not any(_is_draft_of(s, by_key[kk]) for kk in outside):
                outside = []                 # the owner's own draft stays, even when it names someone
            if ideas and not whole and _alternative(s, flat) and not names_planned and not draft:
                cuts.append({'reason': 'trim: options around the pick', 'sentence': s})      # the pick comes later: even a first sentence of options goes
                prev_cut = None
                flags.append(False)
                continue
            if first or names_planned:
                if NOT_DONE.search(s):
                    protected_not_done = True
                flags.append(True)
                prev_cut = None
                continue
            if asks and due_now:
                flags.append(True)
                prev_cut = None
                continue
            if outside and not (due_now and any(P.role(by_key[kk]) in ('ask', 'snag', 'coming') for kk in outside)):
                reason = OUTSIDE
            elif prev_cut is not None and not about(s) and _follows(s, asks, draft, by_key.get(prev_cut[1])):
                reason, outside = prev_cut[0], [prev_cut[1]] if prev_cut[1] else []
            elif draft:
                flags.append(True)
                prev_cut = None
                continue
            elif HOW.search(s) and not asks_how and not own_work:
                reason = 'trim: how it knows'
            elif BOOKKEEPING.search(s) and not own_work:
                reason = 'trim: its own bookkeeping'
            elif correction and BACKSTORY.search(s) and not asks_how:
                reason = 'trim: backstory of a correction'
            elif NOT_DONE.search(s) and not whole:
                honest = answer or asks_status or (wants_done and (bool(about(s)) or re.search(r"\bi (?:can'?t|cannot|couldn'?t|could not|wasn'?t able)", s, re.I)))
                if honest and not protected_not_done:
                    protected_not_done = True          # the one plain not-done line the owner is owed
                    flags.append(True)
                    prev_cut = None
                    continue
                reason = 'trim: its own status, not asked'
            elif PROGRESS.search(s) and not whole and not wants_done and not asks_status and not about(s, planned):
                reason = 'trim: work in progress, not asked'
            if reason:
                cuts.append({'reason': reason, 'sentence': s})
                prev_cut = (reason, outside[0] if outside else (about(s) or [None])[0])
                flags.append(False)
            else:
                prev_cut = None
                flags.append(True)
        keep_flags.append(flags)
        if lead:
            prev_cut = None                  # a list item's follow-on never reaches into the next item
    if WORDING.search(t) and not asks_how:       # after the draft the owner asked for, nothing
        seen = False
        for (_pi, _lead, ss), flags in zip(lines, keep_flags):
            for i, s in enumerate(ss):
                if not flags[i]:
                    continue
                if seen and not about(s, planned) and not (ASKS.search(s) and soon is not None and soon(s)):
                    flags[i] = False
                    cuts.append({'reason': AFTER_DRAFT, 'sentence': s})
                elif any(len(q.split()) >= 4 for q in _quotes(s)):
                    seen = True
    if not cuts:
        return None, [], False
    paras = {}
    for (pi, lead, ss), flags in zip(lines, keep_flags):
        kept = [s for s, f in zip(ss, flags) if f]
        if kept:
            paras.setdefault(pi, []).append(lead + ' '.join(kept))
    out = '\n\n'.join('\n'.join(v) for _, v in sorted(paras.items())).strip()
    by_pattern = any(c['reason'] != OUTSIDE for c in cuts)
    # Only a greeting or "got it" left is fine after a bare answer word ("yes" -> "Sent."), never after a pattern cut or
    # a message that asked for something: there the reply said nothing the owner asked for, and cutting would not fix that.
    if not out or (_only_small_words(out) and (by_pattern or not answer)) or (by_pattern and len(out.split()) < 3 <= len(text.split()) // 3):
        return None, cuts, True               # nothing sensible would be left: the original stays
    return (out, cuts, False) if out != text else (None, [], False)


OUTSIDE = 'trim: outside what the owner asked about'
FOLLOW_ON = re.compile(r"^\W*(?:i(?:'ve| have)? drafted|it'?s|that'?s|she|he|they|this|that)\b", re.I)
BARE_HEARS = re.compile(r"^\W*(?:you(?:'ll| will) hear|i(?:'ll| will) (?:text|tell|update|ping|let) you|more (?:at|by|soon|tomorrow))\b", re.I)


def _follows(s: str, asks: bool, draft: bool, it) -> bool:
    """Right after a cut sentence, does this one only carry on with it? An ask or an offer, that thing's own draft, a
    sentence that starts by pointing back ("She wants ...", "I drafted ..."), or a bare "you'll hear at 3"."""
    if draft:
        return it is not None and _is_draft_of(s, it)
    return asks or bool(FOLLOW_ON.match(s)) or (bool(BARE_HEARS.match(s)) and len(s.split()) <= 10)


def names(P, it: dict) -> set:
    """The names of a thing that can tell it apart: a capitalised word in the middle of a sentence of its title or facts,
    or one that is not an ordinary word. A sentence-opening "Nothing", "Told" or "The" is not a name (chatplan.names_of
    counts every capitalised word, so "Nothing is set yet" would read as being about that thing)."""
    words = P.dictionary()
    out = set()
    for field in ('what', 'owners_call', 'you_found', 'you_noticed', 'facts'):
        txt = str(it.get(field) or '')
        for m in re.finditer(r"\b[A-Z][a-z]{2,}\b", txt):
            w = m.group(0).lower()
            opening = m.start() == 0 or bool(re.search(r"[.!?:;]\s*[\"“(]?\s*$", txt[:m.start()]))
            if not opening or w not in words:
                out.add(w)
    return out - P.NOT_A_NAME


def _alternative(s: str, flat: list) -> bool:
    """For ideas: when another sentence of the reply makes the pick, this one only offers generic alternatives."""
    if PICK.search(s) or any(PICK_BY_NUMBER.search(x) for x in flat) or not any(PICK.search(x) for x in flat if x != s):
        return False
    return bool(ALTERNATIVE.search(s)) or (bool(SUGGEST_START.match(s)) and bool(re.search(r"\bor\b", s, re.I)))

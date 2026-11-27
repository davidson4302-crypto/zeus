"""hisreply: read one reply of the owner's on its own.

When the owner answers something that is waiting on their word, code reads THEIR message and nothing else: not the
question, not what the chat model thinks they meant. Read alone, a reply cannot be nudged by a leading question, and
it cannot smuggle in a changed step ("yes, but send it to Taylor"): anything beyond a plain yes is not a yes.

  yes      a plain, unconditional yes in ordinary words, typos and all ("yes", "yep send it", "approved", "go ahead",
           "yes, set it up", "yeah, turn it on")
  no       a definite no ("no", "skip it", "don't send", "never mind")
  changed  a yes or a no with a change or a condition in it ("yes, but make it Friday", "yes if she agrees",
           "change the second line"). The stored step must NOT run; a fresh one is prepared.
  other    anything else: a question, "wait", "ok", "thanks", a thumbs-up, words that name a thing but do not decide

Rules: plain words approve, never a code; any fuzzy variation of yes, send, approved and so on sends; "ok" alone
never approves. So "ok", "sounds good", "perfect" and a thumbs-up are not a yes by themselves. "Sounds good" IS a yes
when it answers the ask Zeus just made: read() still says "other" (the words alone cannot know what they answer),
agrees() below says the words are only "sounds good", and onesend.approve decides from the times on record whether it
answers that ask.

read() looks only at the reply. bind() is the second step: the words left over after a yes must all be the prepared
step's own words (the owner is pointing at it: "yes send the reply to Jordan"); a time or a number that is not in the
step is a change, and so is a name or a detail that is not in the step; any other word only says how the owner feels
and is ignored. Unclear never runs anything.
No model is asked. Code has the last word.

The reader looks for SIGNALS rather than treating every unknown word as a possible change: a yes (yes words and
phrases, "100%", "absolutely", "for sure", "please" on its own), a no or a hold, and a change ("but", "instead",
"only", "make it" ..., a time or number, a name or a detail that is not in the prepared step, a different action like
"call her"). A yes with no no, hold or change signal is a yes, whatever else surrounds it: enthusiasm, thanks and
filler are ignored and never point at a step ("yes, brilliant" is a yes). Both a yes and a no: unclear, the owner is
asked once. No signal at all: not an answer. Which ask a yes answers is still decided from the times on record
(onesend, lastsaid), and the stored step goes exactly as prepared, once.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402

# The owner's own name and the agent's name are forms of address in a reply, never a detail of the step.
ADDRESS = frozenset(w.lower() for w in (str(settings.OWNER_NAME).split() + [str(settings.AGENT_NAME), 'zeus']) if w.isalpha())

YES = frozenset(('yes', 'yep', 'yeah', 'yup', 'yea', 'yeh', 'yah', 'ya', 'yas', 'yse', 'ys', 'yez', 'yess', 'aye', 'sure', 'approved', 'approve',
                 'approves', 'approving', 'send', 'sned', 'sedn', 'snd', 'confirmed', 'affirmative',
                 # strong yes words, and the usual thumb slips of "yes"/"yeah"
                 'absolutely', 'definitely', 'def', 'defo', 'deffo', 'totally', 'certainly', 'indeed', 'yesh', 'yessir', 'yh', 'ues', 'yrs',
                 'yws', 'mhm', 'mhmm', 'yesyes', 'surely', 'shure', 'suree'))
YES_PHRASES = re.compile(r"\bgo (?:ahead|for it|a?head|ahed|ahaed|ahea|aheda)\b|\bdo (?:it|this|that)\b|\bship it\b|\bplease do\b|\bgreen light\b"
                         r"|\b(?:send|book|order|pay|move|submit|post|schedule|buy|refill|file|sign|grab|take|confirm|accept|start|lock)\s+"
                         r"(?:it|them|that|this|those|these)\b"
                         # switching a thing on in ordinary words ("set it up", "turn it on", "switch that on")
                         r"|\bset (?:(?:it|them|that|this|those|these) )?up\b|\b(?:turn|switch) (?:(?:it|them|that|this|those|these) )?on\b"
                         r"|\bset (?:it|them|that|this|those|these)\b")   # "go ahead and set it"
NO = frozenset(('no', 'nope', 'nah', 'nop', 'na', 'skip', 'cancel', 'stop', 'dont', 'nevermind', 'negative', 'naw', 'nawh', 'nay', 'noo'))
NO_PHRASES = re.compile(r"\bdo not\b|\bnever mind\b|\b(?:i(?:'?ll| will)|hard) pass\b|\bdrop it\b|\bforget it\b|\bleave it\b|\bkill it\b|\bi(?:'?ve| have) got (?:it|that|this)\b"
                        r"|\bi(?:'?ll| will) (?:do|handle|send) (?:it|that|this)\b|\balready (?:sent|did|done|handled|replied)\b")
# Not decided yet. The ask stays open and nothing runs.
HOLD = re.compile(r"\bwait\b|\bhold\b|\bnot yet\b|\bnot now\b|\bnot sure\b|\bmaybe\b|\blet me (?:think|check|look|see)\b|\bone sec\b"
                  r"|\bgive me\b|\bhang on\b|\blater\b"
                  r"|\bpause\b|\bpostpone\b|\bdelay\b|\bhold off\b"      # "yes, pause" is not decided either
                  r"|\bnot (?:right )?now\b|\bnot today\b|\bnot tonight\b|\bnot just yet\b|\bsleep on it\b|\bthink about it\b|\bgimme\b")
# A change or a condition. With one of these in it, a yes is not a plain yes, whatever else it says.
CHANGE = re.compile(r"\b(?:but|except|instead|unless|however|although|though|if|once|when|after|before|until|provided|only|change|changed|edit|reword|"
                    r"rewrite|rephrase|shorten|shorter|longer|soften|add|remove|delete|cut|fix|tweak|update|replace|swap|cc|bcc|also|plus|say|tell|"
                    r"mention|include|without|rather|different|another|other|not)\b|\bmake (?:it|that|this|them)\b|\bas long as\b")
TIME = frozenset(('tonight', 'tomorrow', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'morning', 'afternoon',
                  'evening', 'noon', 'midnight', 'week', 'month', 'weekend', 'am', 'pm', 'eod'))
# Words that add nothing to a yes: manners, emphasis, pointing words, and the ok family (never a yes by themselves).
FILLER = frozenset("""ok okay k kk today number could please pls plz thanks thank you u thx ty it that this them those these the a an one ones now right away asap
immediately go ahead do lets let and then just so well hey hi zeus yo man dude sir buddy bud lol haha ha great good perfect nice cool awesome fine
sounds looks works all is its thats for me i im am with of to on in up out off totally absolutely definitely course as written prepared drafted draft
reply email message note text exactly like way have has be will would can my mine your yours her them their she they they we us sending sent over through
along too very much really def deal done got gotcha here there what was were are s t d ll ve re m lgtm love yours truly both every each thing things
stuff version copy looks look good bro babe sweet cheers tysm thnx thanx yay appreciate appreciated alright aight
work whenever anytime myself yourself""".split())   # "work" as in "yep that'll work"; "whenever"
ALL = frozenset(('both', 'all', 'everything', 'each', 'every'))
# "don't send it", "do not approve": the yes word in it is not a yes.
NEGATED = re.compile(r"\b(?:dont|do not|never|not)\b\s+(?:\w+\s+){0,2}?(?:send|approve|approved|do it|go ahead|book|order|pay|ship|set|turn|switch|start|grab|take|confirm|accept|lock)\b")
# A message that opens like a question asks something, with or without the question mark. "Can you send it" is a
# request, not a question, and is read on like any other reply.
ASKS = re.compile(r"^(?:did|does|can|could|would|should|will|is|are|was|were|what|when|where|why|how|who|which|have|has)\b(?!\s+(?:not|be|love|appreciate)\b)"
                  r"|^do\b(?!\s+(?:not|it|that|this|them|so)\b)")
REQUEST = re.compile(r"^(?:can|could|would|will) you (?:please |just |go ahead and )*(?:send|do|book|order|pay|go ahead|ship|approve|submit)\b")
UNDECIDED = 'the owner has not decided yet'   # the 'why' of a hold; other modules compare against this exact text
ORDINAL = {'first': 1, '1st': 1, 'second': 2, 'two': 2, '2nd': 2, 'third': 3, 'three': 3, '3rd': 3, 'fourth': 4, 'four': 4, '4th': 4,
           'last': -1}


def _flat(text: str) -> str:
    t = str(text or '').replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"').lower()
    return re.sub(r'\s+', ' ', t).strip()


# "yes, that'd be ..." must not read as a yes with one unknown word, "thatd", that binds to no step. A contraction of a word that adds nothing ("that'd", "I'd", "it'll", "we're", "you've") is that
# word plus its ending, and both add nothing. Any other apostrophe word reads as before ("Jordan's", "don't", "can't").
CONTRACTION = re.compile(r"\b([a-z]+)'(d|ll|ve|re|s|m)\b")


def tokens(text: str) -> list:
    flat = CONTRACTION.sub(lambda m: m.group(1) + ' ' + m.group(2) if m.group(1) in FILLER else m.group(0), _flat(text))
    return re.findall(r"[a-z0-9]+", flat.replace("'", ''))


def _squeeze(word: str) -> str:
    """'yesss' -> 'yes', 'sendd' -> 'send', 'approoved' -> 'aproved' (compared against squeezed list words)."""
    return re.sub(r'(.)\1+', r'\1', word)


def _swapped(a: str, b: str) -> bool:
    """Same letters with one neighbouring pair swapped ('sned' / 'send')."""
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]


def _near(a: str, b: str) -> bool:
    """One letter off, for long words only ('approvd', 'aproved', 'confrmed')."""
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 6:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1 or _swapped(a, b)
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


_YES_SQUEEZED = frozenset(_squeeze(w) for w in YES)
_NO_SQUEEZED = frozenset(_squeeze(w) for w in NO)


HMM = re.compile(r'^h+m+$')   # "hmm", "hmmm"... is thinking, never a yes ("hmmm" was one typo from "mhmm")


def is_yes_word(word: str) -> bool:
    if HMM.match(word):   # only a clear yes is a yes; "hmmm ok" reads as unclear and gets one quick confirm
        return False
    if word in YES or _squeeze(word) in _YES_SQUEEZED:
        return True
    return any((len(y) >= 4 and _swapped(word, y)) or _near(word, y) for y in YES)


def is_no_word(word: str) -> bool:
    if word in NO or _squeeze(word) in _NO_SQUEEZED:
        return True
    return any(_near(word, n) for n in NO)


# WHERE a leftover word stands matters: details() below reads it.
# A clause ends at a comma, a stop or an emoji. "too" or "as well" means they add something to the step.
CLAUSE = re.compile(r"[,;:!?.\n()\[\]\"]+|\s[-\u2013\u2014]+\s|[\U0001F000-\U0001FAFF☀-➿]")
DETERMINER = frozenset('the a an my his her their our your some another these those'.split())
SOFT = frozenset(('this', 'that'))
ADDS = re.compile(r"\btoo\b|\bas well\b|\baswell\b")


def _names(text: str) -> set:
    """Words with a capital letter in the middle of a sentence (a name, maybe). A message in capitals has none."""
    raw = str(text or '').replace('’', "'")
    if not re.search(r'[a-z]', raw):
        return set()
    out = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z']*", raw):
        before = raw[:m.start()].rstrip()
        if m.group(0)[0].isupper() and before and before[-1] not in '.!?"':
            w = m.group(0).lower().replace("'", '')
            if w not in ('i', 'im', 'id', 'ill', 'ive') and w not in ADDRESS:
                out.add(w)
    return out


# Ways of saying something in several words, read as one signal before anything else is looked at.
# A no first ("of course not" is a no, never "of course"), then the words that say nothing about the step
# ("no problem", "thanks for checking", "if you can"), then the yeses ("100%", "for sure", "make it happen").
NO_IDIOMS = re.compile(r"\b(?:absolutely|definitely|certainly|totally|surely|hell|heck|of course|god) not\b|\bmaybe (?:next|another|some other) time\b"
                       r"|\b(?:some )?other time\b|\banother time\b|\bnot (?:this|that) (?:time|one|round|week|year)\b|\bnot interested\b"
                       r"|\bnot for (?:me|us|now)\b|\bno need\b|\bnot needed\b|\bnot necessary\b|\bdon'?t bother\b|\bdo not bother\b|\bno way\b"
                       r"|\bnot a chance\b|\bnot happening\b|\bi'?m good\b(?!\s+(?:with|to go)\b)|\bwe'?re good\b(?!\s+(?:with|to go)\b)|\bhard pass\b"
                       r"|\bpass\b(?!\s+(?:it|this|that|them|these|those|along|on|over|through|to|by|the)\b)|\brather not\b|\bi'?d rather not\b")
NEUTRAL_IDIOMS = re.compile(r"\bno (?:problem|prob|probs|worries|rush|biggie)\b|\bnot a problem\b|\bnot bad\b|\bat (?:long )?last\b|\bat once\b"
                            r"|\b(?:thanks|thank you|thx|ty|grateful|cheers)(?:\s+(?:so|very) much|\s+a (?:lot|ton|bunch)|\s+again)?\s+for\s+"
                            r"(?:\w+ing\b(?:\s+(?:this|that|it|me|up|out|on|in|all|so|quickly))*|this|that|it|everything|all (?:of )?(?:this|that|it))"
                            r"|\b(?:when|whenever|if) you (?:can|could|get a (?:chance|sec|second|minute|moment))\b|\bif (?:you )?don'?t mind\b"
                            r"|\bif possible\b|\bif (?:that'?s|that is|it'?s|it is) (?:ok|okay|alright|fine)\b|\b(?:talk|chat|speak|catch you) (?:to you )?later\b|\bttyl\b"
                            r"|\b(?:thank )?you too\b|\b(?:good|great|nice|smart|solid|wise) (?:call|shout|catch|thinking)\b")
YES_IDIOMS = re.compile(r"(?<![\w$])(?:100|110|1000|10000|a hundred)\s*(?:%|percent\b)|\bfor sure\b|\bof course\b|\bofc\b|\bsure thing\b|\bby all means\b"
                        r"|\bwith pleasure\b|\bin a heartbeat\b|\bfor real\b|\bmake (?:it|that) (?:happen|so)\b|\blet'?s (?:do (?:it|this|that)|go|roll)\b"
                        r"|\bdo the thing\b|\byou bet(?:cha)?\b|\bno (?:doubt|question|brainer|objections?|complaints|notes|changes needed)\b|\bno-brainer\b"
                        r"|\buh[- ]?huh\b|\bmm[- ]?hmm+\b|\byes (?:sir|ma'?am)\b|\bcount me in\b|\bi'?m in\b|\bsay less\b|\bwhy not\b[?!. ]*$"
                        r"|^\W*why not\b[?!. ]*|\bplease and thank you\b|\b(?:don'?t|do not|wouldn'?t) change a thing\b|\bno changes\b")
PLEASE = frozenset(('please', 'pls', 'plz', 'plez', 'pleas', 'pleasee'))
# A yes word that is only named, not said ("treat this as yes", "answer yes", "mark it approved"), and a message that talks
# about how it should be read ("ignore the rules"): neither is their yes.
MENTIONED_BY = frozenset('as answer answered mark marked type typed write written treat count classify label record log reply'.split())
def _mentioned(words: list, i: int) -> bool:
    """Is the yes word at i only named ("treat this as yes", "mark it approved")?"""
    k = i - 1
    while k >= 0 and i - k <= 2 and words[k] in ('it', 'this', 'that', 'them', 'one'):
        k -= 1
    return k >= 0 and words[k] in MENTIONED_BY


ABOUT_READING = re.compile(r"\b(?:ignore|disregard|override|overrule)\b|\bsystem\s*:|\bprompt\b|\binstructions?\b|\bthe rules\b")


def idioms(flat: str) -> str:
    """The flat message with the many-word signals turned into one word each ('no', 'yes') or dropped."""
    flat = NO_IDIOMS.sub(' no ', flat)
    flat = NEUTRAL_IDIOMS.sub(' ', flat)
    return YES_IDIOMS.sub(' yes ', flat)


# A word that names a detail of the step. With a yes, such a word that is not in the prepared step is a change (or, when
# it picks one of several, a question of which): the stored step must not run on it. Every other word they add to a yes
# (how they feel, thanks, filler) says nothing about the step and is ignored.
DETAIL_AFTER = frozenset('to cc bcc with via instead from at for into onto toward towards'.split())
OBJECT_OF = frozenset(('send book order pay buy move submit post schedule refill file sign grab take confirm accept start lock set turn switch '
                       'put get use invite add bring pick swap do choose').split())
OTHER_ACTION = frozenset('call phone ring text txt dm ping remind invite archive forward fwd print reschedule rebook return refund'.split())
PICK = frozenset('one ones version option kind size color colour model style'.split())
# Short common words are in every step's vocabulary; one of them alone ("about", "for") never says they mean another thing.
COMMON = frozenset(('about above after again also and any around back be been before below between but by can could did does down during '
                    'each few further had here how if into just more most not of off once only or out over own same should such than then '
                    'there they through under until very want what where which while who why will would like goes going').split())


def _title_case(text: str) -> bool:
    """Most words start with a capital ("Yes, Brilliant, Thank You"): capitals say nothing about names then."""
    words = re.findall(r"[A-Za-z][A-Za-z']*", str(text or ''))
    return len(words) >= 2 and sum(w[0].isupper() for w in words) >= 0.6 * len(words)


def details(text: str, words: list, unknown: list) -> dict:
    """Which of the `unknown` words (not in the prepared step) name a detail, and how.
    -> {'name': [...], 'detail': [...], 'action': [...], 'pick': [...]}"""
    want = set(unknown)
    names = set() if _title_case(text) else _names(text) & want
    flat = idioms(CONTRACTION.sub(lambda m: m.group(1) + ' ' + m.group(2) if m.group(1) in FILLER else m.group(0), _flat(text)))
    clauses = [re.findall(r"[a-z0-9]+", c.replace("'", '')) for c in CLAUSE.split(flat)]
    detail, pick = set(), set()
    for clause in clauses:
        for i, w in enumerate(clause):
            if w not in want:
                continue
            k, steps = i - 1, 0
            while k >= 0 and steps < 2 and clause[k] in DETERMINER | SOFT:
                k, steps = k - 1, steps + 1
            if k >= 0 and (clause[k] in DETAIL_AFTER or clause[k] in OBJECT_OF):
                detail.add(w)                                  # "to the office", "with mom", "do the deluxe", "book the window"
            if i + 1 < len(clause) and clause[i + 1] in PICK:
                pick.add(w)                                    # "the blue one", "the early version"
    return {'name': sorted(names), 'detail': sorted(detail - names), 'action': sorted(w for w in want if w in OTHER_ACTION),
            'pick': sorted(pick | {w for w in want if w in ORDINAL})}


def read(text: str) -> dict:
    """Read the reply alone. -> {'answer': 'yes' | 'no' | 'changed' | 'other', 'bare': nothing but the yes or the no,
    'extra': the words left over (for bind), 'all': he said both / all, 'why': one plain line}."""
    flat = idioms(_flat(text))                          # "100%", "for sure", "of course not" ... as one word
    words = tokens(flat)
    if not words:
        return {'answer': 'other', 'bare': False, 'extra': [], 'all': False, 'why': 'there are no words in it'}
    if words == ['y']:
        return {'answer': 'yes', 'bare': True, 'extra': [], 'all': False, 'why': 'a plain yes'}
    if words == ['n']:
        return {'answer': 'no', 'bare': True, 'extra': [], 'all': False, 'why': 'a plain no'}
    plain = flat.replace("'", '')
    negated = NEGATED.search(plain)
    affirm = NO_PHRASES.sub(' ', NEGATED.sub(' ', plain))   # where a yes may be looked for (not inside a no)
    yes_spans = [m.span() for m in YES_PHRASES.finditer(affirm)]
    no_phrase = bool(negated or NO_PHRASES.search(flat) or NO_PHRASES.search(plain))
    said = tokens(affirm)
    yes = bool(yes_spans) or any(is_yes_word(w) and not _mentioned(said, i) for i, w in enumerate(said))
    if ABOUT_READING.search(plain):
        yes = False                                     # words about how to read it are never a yes
    if not yes and any(w in PLEASE for w in words) and all(w in FILLER or w in PLEASE for w in words):
        yes = True                                      # "please" on its own ("yes please" is a yes anyway)
    no = no_phrase or any(is_no_word(w) for w in words)
    said_all = any(w in ALL for w in words)
    used = set()
    for a, b in yes_spans:
        used.update(tokens(affirm[a:b]))
    if negated:
        used.update(tokens(negated.group(0)))
    for m in list(NO_PHRASES.finditer(flat)) + list(NO_PHRASES.finditer(plain)):
        used.update(tokens(m.group(0)))
    extra = [w for w in words if not (is_yes_word(w) or is_no_word(w) or w in FILLER or w in used or w in ALL)]
    out = {'bare': False, 'extra': extra, 'all': said_all, 'said': str(text or '')}     # bind reads where the words stand
    if '?' in flat or (ASKS.match(plain) and not REQUEST.match(plain)):
        return out | {'answer': 'other', 'why': 'it asks something'}
    if HOLD.search(plain):
        return out | {'answer': 'other', 'why': UNDECIDED}
    if yes and no:
        return out | {'answer': 'other', 'why': 'it says yes and no'}
    without_no = NO_PHRASES.sub(' ', plain)
    if CHANGE.search(without_no):
        return out | {'answer': 'changed', 'why': 'it has a change or a condition in it'}
    if no:
        return out | {'answer': 'no', 'bare': not extra, 'why': 'a plain no' if not extra else 'a no with more in it'}
    if yes:
        return out | {'answer': 'yes', 'bare': not extra and not said_all, 'why': 'a plain yes' if not extra else 'a yes with more in it'}
    return out | {'answer': 'other', 'why': 'it does not say yes or no'}


AGREE = re.compile(r"(?:(?:ok|okay|alright|cool|oh|ah|great|perfect|nice|awesome)\s+)?(?:(?:that|this|it|all that|that all)\s+)?"
                   r"(?:sounds?\s+(?:good|great|perfect|ideal|lovely|brilliant|amazing|awesome|fantastic)(?:\s+to\s+me)?"
                   # the other ordinary ways of agreeing to the thing just put to them (only after that ask)
                   r"|(?:s\s+|is\s+)?(?:works|will work|ll work|would work|d work)(?:\s+for\s+(?:me|us))?|works\s+for\s+(?:me|us)"
                   r"|(?:would|d|will|ll)\s+be\s+(?:great|perfect|amazing|awesome|lovely|ideal|brilliant|fantastic|wonderful|helpful|ace|grand"
                   r"|(?:a\s+)?(?:big|huge|great|real)\s+help|a\s+lifesaver)|(?:i\s+)?(?:would|d)\s+love\s+(?:that|it|this))"
                   r"(?:\s+(?:thanks|thank you|thx|ty|zeus|so much" + ''.join('|' + re.escape(w) for w in sorted(ADDRESS)) + r"))*")


def agrees(text: str) -> bool:
    """Is this reply only "sounds good" ("Sounds good!", "that sounds good to me", "ok sounds good")? Nothing
    else in it: "sounds good but make it friday" is not. Whether it is their yes is decided by onesend.approve.
    Also "sounds great", "that works", "works for me", "that'd be great", "would love that"."""
    return bool(AGREE.fullmatch(' '.join(tokens(text))))


def vocabulary(*texts) -> set:
    """The words a prepared step is made of (its title, the ask, the stored words, who it goes to)."""
    words = set()
    for t in texts:
        if isinstance(t, (list, tuple, set)):
            words |= vocabulary(*t)
        elif t:
            words |= {w for w in tokens(str(t)) if len(w) >= 2}
    return words


def bind(reading: dict, own: set, others: list | None = None) -> dict:
    """Second step, for a yes: tie the reply to ONE prepared step. `own` is that step's vocabulary, `others` the
    vocabularies of every other thing waiting on their word.
    -> the reading with 'answer' possibly turned into 'changed' or 'other', and 'points' = they named this step.
    A word of the owner's that is not in the step is no longer a reason to refuse a yes. It changes the answer only
    when it names a detail: a time or a number, a name, a word after "to", "with", "for" ... or after an action word, a
    different action ("call her"), "too" / "as well" (a change: the step is redone and they are asked once); a pick ("the
    blue one", "the second one") or a word of another waiting thing (unclear: which one they mean). Anything else they add
    to a yes (how they feel, thanks, filler) is ignored and never points at a step."""
    out = dict(reading, points=False)
    if reading.get('answer') not in ('yes', 'no'):
        return out
    extra = list(reading.get('extra') or [])
    unknown = [w for w in extra if w not in own]
    if unknown and reading['answer'] == 'no':
        # A no with words that are not about this step ("stop asking me about these", "cancel my 3pm") may be a new
        # request and not an answer. Closing their item on a guess is worse than asking.
        return out | {'answer': 'other', 'why': 'it has words in it that are not part of the prepared step: ' + ', '.join(unknown[:5])}
    elsewhere = set().union(*others) if others else set()
    if reading['answer'] == 'yes' and ADDS.search(idioms(_flat(reading.get('said') or ''))):
        return out | {'answer': 'changed', 'why': 'it adds something to the prepared step ("too", "as well")'}
    if unknown:
        if any(w in TIME or w.isdigit() or re.fullmatch(r'\d+(?:am|pm|st|nd|rd|th)?', w) for w in unknown):
            return out | {'answer': 'changed', 'why': 'it names a time or a number that is not in the prepared step'}
        theirs = [w for w in unknown if w in elsewhere and w not in COMMON]
        if theirs:
            return out | {'answer': 'other', 'why': 'it has words in it that are about another thing waiting on them: ' + ', '.join(theirs[:5])}
        d = details(reading.get('said') or ' '.join(extra), extra, unknown)
        if d['pick']:
            return out | {'answer': 'other', 'why': 'it picks one, and the prepared step has no ' + ', '.join(d['pick'][:3])}
        named = d['name'] + d['detail'] + d['action']
        if named:
            return out | {'answer': 'changed', 'why': 'it has a change in it: ' + ', '.join(named[:5]) + ' is not in the prepared step'}
        extra = [w for w in extra if w in own]
        out['why'] = 'a yes; the other words only say how they feel'
    out['points'] = any(w not in elsewhere and w not in FILLER for w in extra)
    return out


def selector(text: str, own: set, others: list | None = None):
    """After "which one?": does this reply name one of the things? -> 'all' | a position (1, 2, ... or -1 for the
    last) | 'this' (its words are this step's and no other's) | None. A reply with a change, a question or a no in
    it names nothing."""
    r = read(text)
    if r['answer'] in ('changed', 'no') or r['why'] in ('it asks something', UNDECIDED, 'it says yes and no'):
        return None
    words = [w for w in tokens(text) if not is_yes_word(w)]
    if any(w in ALL for w in words):
        return 'all'
    content = [w for w in words if w not in FILLER or w in ORDINAL]
    numbers = [ORDINAL[w] if w in ORDINAL else int(w) for w in content if w in ORDINAL or (w.isdigit() and 0 < int(w) < 10)]
    rest = [w for w in content if w not in ORDINAL and not w.isdigit()]
    if len(numbers) == 1 and not rest:
        return numbers[0]
    elsewhere = set().union(*others) if others else set()
    if rest and all(w in own for w in rest) and any(w not in elsewhere for w in rest):
        return 'this'
    return None

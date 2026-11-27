"""chatplan: code decides WHAT a chat reply covers; the model only says it.

Why. Sentences in the reference and one code-decided line per kind of message fix the words, but the model still
chooses its own content when the turn carries the whole list: a rundown that opens with the asks and runs 140 words, a
goodnight that leaves out the one ask due in the morning, a welcome back that lists three things. Repairing the length
afterwards with a model rewrite inside the reply hook takes far longer than the hook may wait. So the choice moves in
front of the model, where code can make it in microseconds: for a rundown, a hello, a goodnight, a welcome back, a
"where I am" confirmation and a request for ideas or wording, code builds an ordered, capped plan from the list, and the
model is handed that plan instead of the list.

  kind(message)                  what the owner's message is: 'rundown' 'yesno' 'greeting' 'goodbye' 'arrival' 'confirm'
                                 'ideas' 'ack' or '' (anything else: the turn is left exactly as it was)
  plan(kind, items, ...)         the plan: which open things to say, in which order, and the words that hand it over
  check(reply, plan, items)      after the reply: planned things missing, unplanned list things added, order
  cut_unplanned(reply, ...)      code's one repair: a sentence that only talks about a list thing outside the plan goes
  drop_closing_offer(reply, k)   a request for ideas or wording, or a watch, gets no closing offer ("say yes and I'll send it")
  respell(message, P, home)      a name the owner misspelled reads as the known person, for the people section

Why words and not a model call for the kind of message: the plugin's own model route can take from a few seconds to
well over thirty. Before the turn that wait would sit in front of every message the owner sends, and Hermes drops the
whole reference when the turn hook passes 30 seconds. A detector is instant; this one is broad and tested on made-up
messages in every family, and when it does not recognise a message the turn is left exactly as it was.

Standard library only (plus the engine's settings for names and the morning hour). Nothing here reads or writes a file
except the system word list (/usr/share/dict/words, read once, for spelling).
"""
from __future__ import annotations

import importlib.util
import re
import sys
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
O = settings.OWNER_FIRST if settings.OWNER_FIRST != 'the owner' else 'the owner'      # how the prompts name the owner
POSS = O + "'s"
AGENT = str(settings.AGENT_NAME or 'Zeus')
# The owner's names (full name parts, owner.nicknames) and the agent's name, lower case: a message may tack one on
# ("thanks <agent>", "night all <owner>") without changing what kind of message it is, and none of them names a thing.
NAME_WORDS = frozenset(w for w in ' '.join([str(settings.OWNER_NAME or ''), AGENT]
                                            + [str(n) for n in (settings.get('owner.nicknames', []) or [])]).lower().split()
                       if w not in ('the', 'owner') and re.fullmatch(r"[a-z][a-z0-9'-]*", w))
_NAME_ALT = ''.join('| ' + re.escape(w) for w in sorted(NAME_WORDS, key=len, reverse=True))
# the owner's own names only, as regex alternatives ("|alex|rivera"), for sentences about telling the owner
_OWNER_ALT = ''.join('|' + re.escape(w) for w in sorted({w for w in ' '.join([str(settings.OWNER_NAME or '')]
                     + [str(n) for n in (settings.get('owner.nicknames', []) or [])]).lower().split()
                     if w not in ('the', 'owner') and re.fullmatch(r"[a-z][a-z0-9'-]*", w)}, key=len, reverse=True))
MORNING_HOUR = int(settings.QUIET_END)          # the owner's morning: quiet hours end and the morning text goes out

# ---- what kind of message is this --------------------------------------------------------------------------------------


def norm(message) -> str:
    """Lower case, apostrophes gone ("what's" -> "whats"), every other mark but "?" turned to a space."""
    t = str(message or '').replace('’', "'").replace('‘', "'").lower().replace("'", '')
    t = re.sub(r"[^a-z0-9?\s]", ' ', t)
    return ' '.join(t.split())


# A message that asks for something or answers something is never a plain hello, goodnight or welcome back.
REQUEST = re.compile(r"\?|\b(?:remind|send|book|order|pay|buy|cancel|reply|forward|schedule|move|tell|text|email|call|ask|check|find|"
                     r"look|watch|lmk|let me know|can you|could you|would you|will you|please|pls|what|when|where|why|how|who|which|"
                     r"did|does|need|yes|yep|yeah|yup|no|nope|nah|approve|go ahead|do it|dont|skip|stop)\b")
# Watching or signing in: these confirm a step even with a goodbye on the end ("lmk when the lot reopens, ttyl").
WATCH = re.compile(r"\b(?:lmk|let me know|tell me|ping me|text me)\b.{0,12}\b(?:when|if|once|as soon as|the minute)\b|"
                   r"\bkeep (?:an eye|tabs|watch)\b|\bwatch (?:for|that|this|the|my)\b|\b(?:signed|logged) (?:back )?(?:in|into|on)\b")
GOODBYE_STRONG = re.compile(r"\b(?:ttyl|ttys|bye+|byebye|goodnight|good night|gn|nighty night|night night|signing off|logging off|"
                            r"(?:heading|headed|going|off|getting ready|gotta go|time to go) (?:to )?(?:bed|sleep)|bed ?time|sleep well|"
                            r"gotta (?:run|go|jump|bounce)|got to (?:run|go)|heading out|talk (?:later|tomorrow|tmrw|soon|in a bit)|"
                            r"catch you (?:later|tomorrow)|cya|see ya|see you (?:later|tomorrow|soon)|have a good (?:night|evening|one|weekend)|"
                            # the other common ways to sign off
                            r"call(?:ing|ed)? it (?:a )?(?:night|day)|done for (?:the day|today|tonight|the night)|(?:hit|hitting) the (?:hay|sack)|"
                            r"turn(?:ing)? in(?= for the night\b| now\b|$)|lights out|night (?:all|everyone|everybody|yall|folks)|"
                            r"(?:im|i am) (?:out|off|done) for (?:the night|tonight|the day|today)|knock(?:ing)? off(?: for the day| for tonight)?|"
                            r"phones? (?:is )?(?:going )?off|going offline|time for bed|sleep time|(?:going|heading|off) to (?:get some )?sleep|"
                            r"wrapping up for (?:the day|tonight)|off to bed|hitting the pillow)\b")
GOODBYE_WEAK = re.compile(r"^(?:ok |okay |alright |well )?(?:night|nite|nighty|later|laters|out|im out|off to bed|"
                          r"crashing|im crashing|crashing now|crash time|zzz+|bed|bedtime|sleep)(?: all" + _NAME_ALT + r"| now)?$")    # "crashing" only alone
TRAVEL = re.compile(r"\b(?:till|til|until|thru|through)\b|\bmade it to (?!bed\b|sleep\b)\w+")
ARRIVAL = re.compile(r"^(?:(?:im|i am|we are|were|just|finally|ok|okay|all|and|safely|safe)\s+)*"
                     r"(back|home|landed|made it(?: back| home)?|got (?:back|home|in)|just got (?:back|home|in))\b(.*)$")
ARRIVAL_REST = frozenset('home now safe safely again in town back the trip finally at last yay phew sound and all good '
                         'from too yet'.split()) | NAME_WORDS
GREET_CORE = frozenset('gm morning mornin hi hey heya hello hiya yo howdy sup afternoon evening'.split())
GREET_ALL = GREET_CORE | NAME_WORDS | frozenset('good there happy all monday tuesday wednesday thursday friday saturday sunday to you '
                                   'whats up hows it going'.split())
ACK_CORE = frozenset('ok okay k kk thanks thank ty thx tysm cool nice perfect great awesome appreciate appreciated gotcha noted'.split())
ACK_ALL = ACK_CORE | NAME_WORDS | frozenset('you u got it so much a lot that thats huge lol haha very man bud will do roger copy that '
                               'love it this one'.split())
# "is anything waiting on me?": a yes-or-no question about what needs the owner.
YESNO = re.compile(r"\b(?:is|are) (?:there )?(?:anything|something) (?:waiting|pending|for me|on me|that needs me|i (?:need|have) to)\b|"
                   r"^(?:is |are |got )?anything (?:waiting|need(?:s|ing)? (?:me|my)|for me|on me|from me)\b|"
                   r"\banything (?:i|we) (?:need|have|got|gotta|ought|should) (?:to )?(?:do|deal with|handle|sign|approve|decide|answer|"
                   r"look at|worry about|take care of)\b|\b(?:do you |you )?need (?:me|anything from me|something from me)\b|"
                   r"\b(?:am i|are we) good\b|\banything for me to\b|\bwhat (?:needs|is waiting on|do you need from) me\b")
# An open question for the whole picture ("what is new", "where do things stand").
RUNDOWN = re.compile(r"\bwhats (?:going on|new|happening|the latest|up with (?:everything|you|things)|cooking|"
                     r"on (?:my|your|the) (?:plate|list|radar)|on (?:for )?(?:today|tonight|tomorrow|this week)|on deck|"
                     r"the (?:plan|rundown|status|story|scoop))\b|"
                     r"\banything (?:new|going on|happening|i should know|i need to know|to know|worth knowing|i should hear)\b|"
                     r"\bany (?:news|updates?)\b|\bwhat (?:did|have) (?:i|we) miss(?:ed)?\b|\bdid (?:i|we) miss anything\b|"
                     r"\banything (?:i|we) missed\b|\bfill me in\b|\bcatch me up\b|\bbring me up to speed\b|\bupdate me\b|"
                     r"\bgive me (?:the|a) (?:rundown|update|recap|run down)\b|^(?:status|update|updates|recap|rundown|news)\b|"
                     r"\bwhere (?:are we|do (?:we|things) stand|are things|we at)\b|\bhow are things\b|\bhows (?:everything|things)\b|"
                     r"\bwhat do i (?:need|have) to know\b|\bwhat are you (?:working on|up to)\b")
# What may follow a rundown question and still leave it about everything ("where are we on everything", "anything i
# need to know about"). Anything else after on/with/about/for names one thing ("where are we on the deck"): not a rundown.
GENERIC = frozenset('stuff things everything all it this that me my end your you the day week today tonight tomorrow list plate front '
                    'home at lately else so far now here there yet anything other of on with about for guys in general '
                    'overall around going since i was gone away while out this morning afternoon evening up to speed'.split()) | NAME_WORDS
POINTERS = frozenset('on with about for re regarding from'.split())
IDEAS = re.compile(
    r"\b(?:ideas?|suggestions?|recommend(?:ations?)?|gift|present for|what (?:should|do|can) i (?:get|buy|bring|make|cook|say|write|text|tell)|"
    r"help me (?:word|write|phrase|figure out|pick|choose|plan|think of)|how (?:should|do|can) i (?:word|say|ask|phrase|put|tell)|"
    # asking for the words to nudge, remind or turn someone down is wording help too
    r"how (?:should|do|can|would) i (?:nudge|remind|chase|approach|bring up|follow up with|hint|let \w+ know|break it to|turn down|decline)|"
    r"without sounding|w/o sounding|"
    r"(?:word|phrase) (?:a|an|the|this|my) (?:text|message|note|email|reply)|what should we do)\b", re.I)


def _about_everything(t: str, m) -> bool:
    """After the rundown words: nothing, or only words that keep it about everything."""
    rest = t[m.end():].replace('?', ' ').split()
    if not rest:
        return True
    if rest[0] in POINTERS:
        return all(w in GENERIC for w in rest)
    return all(w in GENERIC for w in rest) or len(rest) <= 2 and not (set(rest) & POINTERS)


def kind(message, confirm=None) -> str:
    """What the owner's message is, for the plan. confirm = the reply checks' own reading of "where they are" (ask_kind ==
    'confirm'), when the caller has it. Unrecognised: '' (the turn stays exactly as it was)."""
    t = norm(message)
    if not t or len(t.split()) > 16:
        return ''
    words = t.replace('?', ' ').split()
    if WATCH.search(t):
        return 'confirm'
    if set(words) <= ACK_ALL and set(words) & ACK_CORE and '?' not in t:
        return 'ack'
    if (GOODBYE_STRONG.search(t) or GOODBYE_WEAK.match(t)) and not REQUEST.search(GOODBYE_STRONG.sub(' ', t)):
        return 'goodbye'
    if (confirm is not None and confirm(message)) or (TRAVEL.search(t) and not REQUEST.search(t) and len(words) <= 10):
        return 'confirm'
    a = ARRIVAL.match(t)
    if a and not REQUEST.search(t) and len(words) <= 9:
        rest = a.group(2).split()
        if all(w in ARRIVAL_REST for w in rest) or (rest[:1] == ['from'] and 2 <= len(rest) <= 3) \
                or (rest[:2] == ['in', 'town'] and all(w in ARRIVAL_REST for w in rest[2:])):
            return 'arrival'
    if set(words) <= GREET_ALL and set(words) & GREET_CORE and '?' not in t.replace('whats up?', ''):
        return 'greeting'
    m = YESNO.search(t)
    if m and _about_everything(t, m):
        return 'yesno'
    m = RUNDOWN.search(t)
    if m and _about_everything(t, m):
        return 'rundown'
    if IDEAS.search(str(message or '').replace('’', "'")):
        return 'ideas'
    return ''


# ---- when is a thing due ---------------------------------------------------------------------------------------------
DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
CLOCK = re.compile(r"\b(\d{1,2})(?::(\d\d))?\s*([ap])\.?m\b|\b(noon|midnight)\b", re.I)
DAY = re.compile(r"\b(today|tonight|tomorrow|tmrw|eod|end of (?:the )?day|this (?:morning|afternoon|evening)|"
                 r"mon|tues?|wed|thu(?:rs)?|fri|sat|sun)(?:day|nesday|urday|sday)?\b|"
                 r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.? (\d{1,2})\b", re.I)


NEXT_BEFORE = re.compile(r"\b(?:next|following)\s+$", re.I)
# "by Wednesday, Jun 30" said on Wednesday Jun 16 must not read as TODAY (the weekday alone, today included), or an ask
# two weeks out looks due before the owner's next morning text. A weekday written right in front of a month and day is
# part of that date, not a day of its own.
DATED_AFTER = re.compile(r"^[\s,]{0,3}(?:the\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", re.I)


def _weekday_of_a_date(d) -> bool:
    """Is this match a weekday word standing right before a month and day ("Wednesday, Jun 30")?"""
    w = (d.group(1) or '').lower()
    return bool(w) and w[:3] in DAYS and bool(DATED_AFTER.match(d.string[d.end():d.end() + 24]))


def _day_of(m, now: datetime):
    """The date a day word names, from now (a weekday is its next one, today included)."""
    if m.group(2):
        month = MONTHS.index(m.group(2).lower()[:3]) + 1
        try:
            d = datetime(now.year, month, int(m.group(3)))
        except ValueError:
            return None
        return d if d.date() >= now.date() - timedelta(days=60) else d.replace(year=now.year + 1)
    w = m.group(1).lower()
    if w.startswith(('today', 'tonight', 'eod', 'end', 'this')):
        return now
    if w.startswith(('tomorrow', 'tmrw')):
        return now + timedelta(days=1)
    ahead = (DAYS.index(w[:3]) - now.weekday()) % 7
    if ahead == 0 and NEXT_BEFORE.search(m.string[max(0, m.start() - 12):m.start()]):
        ahead = 7        # "by next Wednesday" said on a Wednesday is a week away, never today
    return now + timedelta(days=ahead)


def deadline(text, now: datetime, not_before: datetime | None = None):
    """The soonest time this text names as a deadline, or None. "by 8am" said at 10pm is tomorrow at 8; "by Fri" is the
    end of Friday; "tonight" is 9pm; "today" is the end of today. not_before: times before it are left out."""
    s = str(text or '').replace('’', "'")
    found = []
    clocks = list(CLOCK.finditer(s))
    used_days = set()
    for c in clocks:
        if c.group(4):
            hour, minute = (12 if c.group(4).lower() == 'noon' else 23), (0 if c.group(4).lower() == 'noon' else 59)
        else:
            hour, minute = int(c.group(1)) % 12 + (12 if c.group(3).lower() == 'p' else 0), int(c.group(2) or 0)
        near = [d for d in DAY.finditer(s) if abs(d.start() - c.start()) <= 30 and not _weekday_of_a_date(d)]
        if near:
            d = min(near, key=lambda d: abs(d.start() - c.start()))
            used_days.add(d.start())
            day = _day_of(d, now)
            if day is None:
                continue
            at = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        else:
            at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at < now - timedelta(hours=1):
                at += timedelta(days=1)
        found.append(at)
    for d in DAY.finditer(s):
        if d.start() in used_days or _weekday_of_a_date(d):
            continue
        day = _day_of(d, now)
        if day is None:
            continue
        w = (d.group(1) or '').lower()
        hour = 21 if w == 'tonight' else 17 if w.startswith(('eod', 'end')) else 23
        found.append(day.replace(hour=hour, minute=59 if hour == 23 else 0, second=0, microsecond=0))
    if not_before is not None:
        found = [at for at in found if at >= not_before]
    return min(found) if found else None


def next_morning(now: datetime) -> datetime:
    """The owner's next morning text: the morning hour (quiet hours' end) today if it is not that yet, else tomorrow."""
    at = now.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
    return at if now < at else at + timedelta(days=1)


def due_soon(items: list, now: datetime) -> list:
    """The open ask that must be answered before the owner's next morning text, soonest first ([] or [one item]).
    Turned off: the owner is asked once, by the note that asked; a reply about something else never carries another
    question. Every caller treats [] as "nothing due"."""
    return []


SOON_LINE = (f'Whatever {POSS} message is, code checked your list: one thing must happen before their next morning text. Unless '
             'your answer is already about it, after your answer add it in one short line with who, how much and by when, '
             'and the ask in its own words: ')


# ---- the plan ----------------------------------------------------------------------------------------------------------
HEAD = "ABOUT THE OWNER'S MESSAGE"      # the plugin's own TURN_HEAD: the guard already counts it as machinery
PLANNED = ('rundown', 'yesno', 'greeting', 'goodbye', 'arrival', 'confirm', 'ideas')     # 'ack' and '' are left as they were


def role(it: dict) -> str:
    """'snag', 'news', 'ask' (waiting on the owner's word, or finished with a decision not yet heard), 'coming', 'done'."""
    s = str(it.get('state') or '')
    if 'failed' in s:
        return 'snag'
    if s.startswith('you told them') and 'waiting on their word' in s:
        return 'ask'
    if s.startswith('you finished'):
        return 'ask' if it.get('owners_call') else 'news'
    if s.startswith(('you are working', 'you have not started')):
        return 'coming'
    return 'done'


def _clip(text, n) -> str:
    t = ' '.join(str(text or '').split()).rstrip(' .')
    return t if len(t) <= n else t[:n].rsplit(' ', 1)[0] + '...'


CUE = re.compile(r"\b(?:by|before|due|closes?|closing|ends?|deadline|until|till|no later than|expires?|cutoff)\b\W{0,3}(.{0,40})", re.I)


def due_of(it: dict, now: datetime):
    """When a thing must be answered: a time after "by", "before", "due", "closes" ... anywhere in it (a job's own
    date, "the yes for $560 on Dec 9", is not its deadline). No such words: no deadline."""
    # "by Friday Feb 12. They were told Sunday Feb 7" is a Friday deadline. The words after "by" stop at the end of
    # their sentence, and a time already past is not a deadline: an ask whose only deadline has passed is due at no
    # time, never "before the owner's next morning text".
    windows = [re.split(r'(?<=[.!?])\s+(?=[A-Z])', m.group(1), maxsplit=1)[0]
               for field in ('owners_call', 'facts', 'you_found', 'what') for m in CUE.finditer(str(it.get(field) or ''))]
    windows = [w for w in windows if deadline(w, now) is not None]
    cued = [d for d in (deadline(w, now, not_before=now) for w in windows) if d is not None]
    if cued:
        return min(cued)
    return None      # only a time after a deadline word (by, before, due, closes ...) is a deadline; a clock time that merely appears in the facts is not


def _deadline_sentence(it: dict) -> str:
    """The one sentence of its ask or facts that says by when ("She needs an answer by 6pm today."), or ''."""
    for field in ('owners_call', 'facts'):
        for s in re.split(r'(?<=[.!?])\s+', str(it.get(field) or '')):
            if CUE.search(s) and deadline(s, datetime(2000, 1, 3)) is not None:
                return _clip(s, 110)
    return ''


# A finished thing's own details (what it cost, when it is ready, that a day is not set yet) are what make the news
# usable; without them the model tells an unsettled repair as if it were settled. Its facts go on its line, except a
# sentence about telling the owner.
_WHO = r"(?:him|her|them|the owner" + _OWNER_ALT + r")"
TOLD = re.compile(r"\b(?:told " + _WHO + r"|(?:he|she|they|the owner" + _OWNER_ALT + r") (?:was|were) told|has not answered|"
                  r"hasn'?t answered|asked " + _WHO + r"|(?:his|her|their|the owner'?s) first reply|(?:he|she|they) heard)\b", re.I)


def _news_facts(it: dict) -> str:
    keep = [s for s in re.split(r'(?<=[.!?])\s+', str(it.get('facts') or '').strip()) if s.strip() and not TOLD.search(s)]
    return _clip(' '.join(keep), 110) if keep else ''


def say_line(it: dict, short: bool = False) -> str:
    """What one planned thing is, for the model to say in its own words: never a ref, a draft or a status word.
    short (the plan's own lines): only what the line needs, because the model says every fact it is handed. News: what
    was found. An ask: what was found, its ask, and the one sentence that says by when."""
    r = role(it)
    if short:
        bits = [_clip(it.get('you_found') or it.get('what') or 'one thing', 150)]
        if r == 'ask':
            bits += [x for x in ('Their call: ' + _clip(it.get('owners_call'), 90) if it.get('owners_call') else '', _deadline_sentence(it)) if x]
        elif r == 'snag' and it.get('facts'):
            bits.append(_clip(it['facts'], 110))
        elif r == 'news' and _news_facts(it):
            bits.append(_news_facts(it))
        elif r == 'coming' and it.get('he_hears_about_it'):
            bits.append(f'{O} hears the full answer ' + str(it['he_hears_about_it'])[:16].replace('T', ' at '))
        return '. '.join(b.rstrip('.') for b in bits) + '.'
    bits = [_clip(it.get('what') or 'one thing', 120)]
    if it.get('you_found'):
        bits.append(_clip(it['you_found'], 160))
    if r == 'ask' and it.get('owners_call'):
        bits.append('Their call: ' + _clip(it['owners_call'], 160))
    if it.get('facts'):
        bits.append(_clip(it['facts'], 200))
    if r == 'coming' and it.get('he_hears_about_it'):
        bits.append(f'{O} hears the full answer ' + str(it['he_hears_about_it'])[:16].replace('T', ' at '))
    return '. '.join(bits) + '.'


def _named(message: str, it: dict, by_name: bool) -> bool:
    """Is the owner's message about this thing? One word of its title in common (not a stop word). by_name=False: a
    person's or shop's name alone does not count ("how do i tell jordan no" is not about Jordan's gift money)."""
    msg_words = {w for w in re.findall(r"[a-z]{3,}", norm(message)) if w not in STOP}
    title = {w for w in re.findall(r"[a-z]{3,}", norm(it.get('what'))) if w not in STOP}
    if not by_name:
        title -= names_of(it)
        return bool(msg_words & title)
    return bool(msg_words & (title | names_of(it)))


STOP = frozenset('the and for with from that this your you him her his she they them about reply answer send what when where '
                 'should could would can how help word text message email note some any anything thing things need needs '
                 'have has had get got are was were will just like into over out not but yes said says tell ask idea ideas '
                 'suggestion suggestions should what lmk know let keep eye watch made here till until back'.split())
NOT_A_NAME = frozenset('reply send answer plan confirm verify sort check prepare find review book renew pay order the your his her '
                       'monday tuesday wednesday thursday friday saturday sunday january february march april may june july '
                       'august september october november december jan feb mar apr jun jul aug sep sept oct nov dec today '
                       'tonight tomorrow thanksgiving christmas easter halloween new year years eve day you '
                       'and but also she him they this that there then when what how why who yes not one two three'.split()) | NAME_WORDS


def names_of(it: dict) -> set:
    """The capitalised words that name this thing (people, shops, places), lower case."""
    out = set()
    for field in ('what', 'owners_call', 'you_found', 'you_noticed', 'facts'):
        out |= {w.lower() for w in re.findall(r"\b[A-Z][a-z]{2,}\b", str(it.get(field) or ''))}
    return out - NOT_A_NAME


def plan(k: str, items: list, now: datetime, message: str = '', stopped: bool = False, zone: str = '') -> dict | None:
    """-> {'kind', 'keys' (the planned things, in order), 'listing' (what replaces the list: '' for none),
    'note' (the line after it)} or None (a kind this plan does not cover: the turn stays as it was).
    items = every open thing, as the plugin reads them (open_items)."""
    if k not in PLANNED:
        return None
    by = {r: [it for it in items if role(it) == r] for r in ('snag', 'news', 'ask', 'coming')}
    order = {id(it): i for i, it in enumerate(items)}
    far = now + timedelta(days=3650)
    asks = sorted(by['ask'], key=lambda it: (due_of(it, now) or far, order[id(it)]))
    if k in ('rundown', 'yesno'):
        return _whole_picture(k, by, asks, now, message, stopped)
    if k in ('greeting', 'goodbye', 'arrival'):
        soon = due_soon(asks, now)      # always [] now: a greeting is answered in kind and nothing is added
        what = {'greeting': 'it is only a greeting. Answer it in kind in one short line',
                'goodbye': f'{O} is signing off (bed, a meeting, heading out). Say goodbye in kind in one short line',
                'arrival': 'it is a welcome back, a greeting, not news to act on. ' + zone + 'Welcome them back warmly in one short line'}[k]
        if soon:
            note = (f'{HEAD}: {what}. Code checked everything on your list: exactly one thing must happen before their next '
                    f'morning text, so after that line say only this one, in one short line with who, how much and by when, '
                    f'and the ask in its own words: {say_line(soon[0], short=True)} Nothing else from your work.')
        else:
            note = f'{HEAD}: {what} and stop. Say nothing from your work, not even that nothing needs them.'
        return {'kind': k, 'keys': [it['key'] for it in soon], 'listing': '', 'note': note}
    # confirm and ideas: only the things the owner's message is about stay in view
    # A trip ("in X till Y") keeps nothing: what is booked for the same trip is not news (a hotel named for the town is
    # the trap). A watch or a sign-in keeps the things the message names.
    keep = [] if k == 'confirm' and not WATCH.search(norm(message)) else \
        [it for it in items if _named(message, it, k == 'confirm') and (k == 'confirm' or role(it) != 'done')]
    if k == 'confirm':
        note = (f'{HEAD}: it says where {O} is, that they signed in again, or what to keep an eye on. Act on it with your '
                'tool, then answer in one short line: what is now set, in your own words from what the tool answered, or '
                'plainly why not. Nothing about their quiet hours, their clock or what it changes, no offer, and nothing else '
                'from your work.')
    else:
        note = (f'{HEAD}: {O} asks for ideas or for help with wording. Give one or two specific suggestions, your pick first, '
                f'built on what {O} has told you about the people involved: what they love or are into comes before what is '
                'going on with them. Two or three short sentences, at most 45 words: your pick and why it fits them, and one '
                f'thing to avoid only if {O} told you one; no list, no prices, shops or facts you were not given. Help with a '
                f'text is one draft in {POSS} voice, with no second version, no tips and no offer to send it. Nothing from your '
                'work ' + ('except what the message is about (below).' if keep else '(none of it is about the message).') + DRAFT_RULE)
    soon = [it for it in due_soon(items, now) if it not in keep]      # the overnight ask is never left out (see due_soon)
    if soon:
        keep += soon
        note += ' ' + SOON_LINE + say_line(soon[0], short=True)
    listing = ''
    if keep:
        listing = (f'WHAT IS OPEN RIGHT NOW (only what {POSS} message is about; the rest is left out for this reply)\n'
                   + '\n'.join(f"- {say_line(it)} [ref {it['key']}]" for it in keep))
    return {'kind': k, 'keys': [it['key'] for it in keep], 'listing': listing, 'note': note, 'context_only': True}


def _whole_picture(k, by, asks, now, message, stopped) -> dict:
    snags, news = by['snag'][:2], by['news'][:3]
    asks = asks[:3]
    coming = by['coming'][:3] if (re.search(r"\bworking on\b|\bup to\b|\bcooking\b", norm(message)) or not (snags or news or asks)) else []
    lines, keys = [], []
    if stopped:
        lines.append('First: your background watching is paused, so nothing new is being noticed.')
    if k == 'yesno':
        lines.append('Start with "Yes"' if (asks or snags) else f'Start with "No", nothing needs {O}')
    for label, group in (('What hit a snag', snags), (f'Finished, and {O} has not heard yet', news if k == 'rundown' or not asks else news[:2])):
        for it in group:
            lines.append(f'{label}: {say_line(it, short=True)}')
            keys.append(it['key'])
    if k == 'yesno' and asks:          # a yes-or-no question about what needs the owner: the asks come right after the answer
        lines = lines[:1 + bool(stopped)] + [f'Needs {POSS} word: {say_line(it, short=True)}' for it in asks] + lines[1 + bool(stopped):]
        keys = [it['key'] for it in asks] + keys
    elif asks:
        lines += [(f'Needs {POSS} word' + (' (soonest deadline)' if len(asks) > 1 and i == 0 else '') + f': {say_line(it, short=True)}') for i, it in enumerate(asks)]
        keys += [it['key'] for it in asks]
    for it in coming:
        lines.append(f'Still being worked on (a few words, with when {O} hears): {say_line(it, short=True)}')
        keys.append(it['key'])
    if not (snags or news or asks or coming):
        lines.append('Nothing is open: say so in one short line.')
    words = min(70, max(15, 6 + 10 * len(snags) + 14 * len(news) + 13 * len(asks) + 5 * len(coming)))   # news carries its details
    asked = 'a yes-or-no question about what needs them' if k == 'yesno' else 'for the whole picture'
    note = (f'{HEAD}: {O} asks {asked}. Code has picked what to say from everything on your list. Say these, in this order, '
            f'each in one short sentence in your own words, and nothing else from your work: not what {O} already heard, '
            'not what you are still working on unless it is below. For a thing that needs their word: who, how much, by when, '
            'and the ask in its own words, with no draft quoted. A finished thing keeps its details (how much, when, what is '
            f'not settled yet). The lines below speak of {O} in the third person: say them to {O} directly ("you", "your"). '
            'Plain sentences in one or two short paragraphs: no numbered list, no '
            f'count of how many things, no lead-in line, about {words} words in all.\n'
            + '\n'.join(f'- {x}' for x in lines))
    return {'kind': k, 'keys': keys, 'listing': '', 'note': note}


# ---- after the reply: did it say the plan --------------------------------------------------------------------------------
SENT = re.compile(r'(?<=[.!?])\s+|(?<=[.!?]["”\')\]])\s+|\n+')


def _pieces(reply: str) -> list:
    out = []
    for line in str(reply or '').split('\n'):
        out += [s for s in SENT.split(line.strip()) if s.strip()]
    return out


def _mentions(text: str, it: dict) -> bool:
    caps = {w.lower() for w in re.findall(r"\b[A-Z][a-z]{2,}\b", text)}
    if names_of(it) & caps:
        return True
    said = set(re.findall(r"[a-z]{4,}", norm(text)))
    return any(len(w) >= 5 for w in title_words(it) & said) or len(title_words(it) & said) >= 2


def check(reply: str, p: dict, items: list, message: str = '') -> dict:
    """-> {'missing': planned keys the reply never names, 'unplanned': open things outside the plan it names,
    'order_kept': the planned things appear in plan order}. Read by code from names and title words; logged only."""
    planned = [] if (p or {}).get('context_only') else [next((it for it in items if it['key'] == k), None) for k in (p or {}).get('keys', [])]
    planned = [it for it in planned if it]
    text = str(reply or '')
    missing = [it['key'] for it in planned if not _mentions(text, it)]
    others = _unplanned_names(p, items, message)
    unplanned = sorted({k for s in _pieces(text) for k, (ns, tw) in others.items() if _talks_about(s, ns, tw)})
    at = []
    for it in planned:
        hits = [m.start() for n in names_of(it) for m in re.finditer(r"\b" + re.escape(n) + r"\b", text, re.I)]
        if hits:
            at.append(min(hits))
    return {'missing': missing, 'unplanned': unplanned, 'order_kept': at == sorted(at)}


def title_words(it: dict) -> set:
    """The words of a thing's title that say what it is ("snow chains"), lower case, no stop words."""
    return {w for w in re.findall(r"[a-z]{4,}", norm(it.get('what'))) if w not in STOP and w not in NOT_A_NAME}


def _unplanned_names(p: dict, items: list, message: str) -> dict:
    """key -> (the names, the title words) that belong ONLY to that open thing outside the plan: not to a planned thing,
    not in the owner's message."""
    keys = set((p or {}).get('keys', []))
    planned = [it for it in items if it['key'] in keys]
    keep = set().union(*[names_of(it) | title_words(it) for it in planned]) if planned else set()
    msg_words = set(norm(message).replace('?', ' ').split())
    out = {}
    for it in items:
        if it['key'] in keys:
            continue
        ns = {n for n in names_of(it) if len(n) >= 4} - keep - msg_words
        tw = title_words(it) - keep - msg_words
        if ns or len(tw) >= 2:
            out[it['key']] = (ns, tw)
    return out


def _talks_about(sentence: str, names: set, words: set) -> bool:
    """A sentence talks about a thing when it names it (a capitalised name of it) or uses two words of its title (the
    one word, for a one-word title of five letters or more: "Hotel")."""
    caps = {w.lower() for w in re.findall(r"\b[A-Z][a-z]{2,}\b", sentence)}
    need = 1 if len(words) == 1 and len(next(iter(words))) >= 5 else 2
    return bool(caps & names) or bool(words) and len(words & set(re.findall(r"[a-z]{4,}", norm(sentence)))) >= need


def _due_soon(sentence: str, now) -> bool:
    """Does this sentence say something must happen before the owner's next morning text ("expires at 11:30pm tonight")?"""
    if now is None:
        return False
    if re.search(r"\btonight\b", sentence, re.I):
        return True
    cued = [deadline(m.group(1), now) for m in CUE.finditer(sentence)]
    return any(d is not None and d < next_morning(now) for d in cued)


def cut_unplanned(reply: str, p: dict, items: list, message: str = '', now=None, soon=None):
    """-> the reply without the sentences that only talk about a list thing outside the plan, or None: nothing to cut,
    nothing would be left, the reply OPENS with such a sentence (it may be answering something real: left alone, as the
    greeting guard does), or a sentence to be cut is about a thing still open (an ask, a snag, work under way) and says it is due
    before the owner's next morning text (then it may be the one thing they must hear tonight, so the whole reply stays). Quoted words are never cut.
    soon = the plugin's own test for "names a time before the owner's next morning text" (keep_the_greeting uses it); without
    it, a narrower one here."""
    if not p or p.get('kind') not in PLANNED:
        return None
    others = _unplanned_names(p, items, message)
    if not others:
        return None
    keys = set(p.get('keys', []))
    planned = [it for it in items if it['key'] in keys]
    by_key = {it['key']: it for it in items}
    paras, cut, first = [], 0, True
    for para in re.split(r'\n\s*\n', str(reply or '').strip()):
        lines = []
        for line in para.split('\n'):
            lead = re.match(r'^(\s*(?:\d+[.)]|[-•*])\s+)?', line).group(0)
            kept = []
            for s in SENT.split(line[len(lead):].strip()):
                about = [k for k, (ns, tw) in others.items() if s and _talks_about(s, ns, tw)]
                if about and not any(_talks_about(s, names_of(it), title_words(it)) for it in planned) and not re.search(r'["“”]', s):
                    needs_owner = any(role(by_key[k]) in ('ask', 'snag', 'coming') for k in about)
                    if first or (needs_owner and (soon(s) if soon is not None else _due_soon(s, now))):
                        return None
                    cut += 1
                    continue
                first = first and not s
                if s:
                    kept.append(s)
            if kept:
                lines.append(lead + ' '.join(kept))
        if lines:
            paras.append('\n'.join(lines))
    out = '\n\n'.join(paras).strip()
    return out if cut and out else None


LIST_MARK = re.compile(r'^\s*(?:\d+[.)]|[-•*])\s+')
COUNT_LEAD = re.compile(r"^\W*(?:(?:here(?:'s| is| are)|so|ok|okay|yes|yep|also)\W+)*(?:(?:one|two|three|four|five|a few|some|\d+) (?:more )?things?|"
                        r"what (?:needs|is waiting on) (?:you|your word)|(?:still )?waiting on you|(?:the )?news|done|finished)[^.!?]{0,70}:\s*$", re.I)


COUNT_SENTENCE = re.compile(r"(?:(?:so|ok|okay|also|and)\W+)?(?:(?:one|two|three|four|five|a few|\d+) (?:more )?things? "
                            r"(?:need|needs|are|is|waiting|came|for)[^.!?:\n]{0,30}|here(?:'s| is) (?:where things stand|the rundown|what's new))(?:\.|:(?=[ \t]*\n))")   # "...: the pies" keeps its colon


def flatten_list(reply: str, yesno: bool = False):
    """A rundown that came back as a numbered or bulleted list -> the same words as plain sentences: the markers go,
    a lead-in line that only counts ("Two things need your word:") goes, and each paragraph's lines are joined.
    None when the reply has no list. Nothing else changes: no word of a sentence is touched. yesno: the lead-in line may
    hold the answer ("Yes, two things:"), so it stays, ending in a full stop."""
    text = str(reply or '').strip()
    if not yesno:            # an open rundown: a sentence that only counts goes too ("Two things need your word.")
        text = re.sub(r"(?:(?<=\n)|^)[ \t]*" + COUNT_SENTENCE.pattern + r"[ \t]*(?=\S)", '', text, flags=re.I | re.M).strip()
    if not any(LIST_MARK.match(line) for line in text.split('\n')):
        return text if text and text != str(reply or '').strip() else None
    paras = []
    for para in re.split(r'\n\s*\n', text):
        lines = [LIST_MARK.sub('', line).strip() for line in para.split('\n')]
        lines = [(x[:-1].rstrip() + '.' if yesno else '') if COUNT_LEAD.match(x) else x for x in lines]
        lines = [x for x in lines if x]
        if lines:
            paras.append(' '.join(lines))
    out = '\n\n'.join(paras).strip()
    return out if out and out != text else None


# A closing offer, where none belongs: ideas or wording the owner will send themself, and a watch (they asked to be told).
CLOSING_OFFER = re.compile(r"^(?:(?:so|and|or|also|then|just)\W+)*(?:(?:just )?say (?:yes|the word)|(?:do you )?want me to|would you like me to|"
                           r"should i|shall i|i can (?:also )?(?:send|text|forward|pass|email|message|grab|buy|order|book)|"
                           r"happy to|if you(?:d| would)? (?:want|like|prefer)|let me know if)\b", re.I)


def drop_closing_offer(reply: str, k: str, message: str = ''):
    """-> the reply without a last sentence that only offers more, or None. Only for ideas and wording, and for a
    watch; never a sentence with a quoted draft in it, and never the whole reply."""
    if k not in ('ideas', 'confirm') or (k == 'confirm' and not WATCH.search(norm(message))):
        return None
    text = str(reply or '').rstrip()
    pieces = _pieces(text)
    if len(pieces) < 2:
        return None
    last = pieces[-1]
    if not CLOSING_OFFER.match(last.replace('’', '').replace("'", '')) or re.search(r'["“”]', last) or len(last.split()) > 22:
        return None
    if CUE.search(last) or re.search(r"\b(?:tonight|tomorrow|\d{1,2}(?::\d\d)?\s*(?:am|pm))\b", last, re.I):
        return None          # an ask with a deadline is a real decision, not an empty offer
    at = text.rfind(last)
    out = text[:at].rstrip()
    return out or None


# ---- names the owner misspelled ------------------------------------------------------------------------------------------------
WORDS_FILE = '/usr/share/dict/words'
_WORDS = None


def dictionary() -> frozenset:
    """Every ordinary word of the system word list, lower case (empty when it cannot be read: then only near-exact
    matches of long names are taken)."""
    global _WORDS
    if _WORDS is None:
        try:
            with open(WORDS_FILE, encoding='utf-8', errors='ignore') as f:
                _WORDS = frozenset(w.strip().lower() for w in f if w.strip())
        except OSError:
            _WORDS = frozenset()
    return _WORDS


def distance(a: str, b: str) -> int:
    """Edits between two words (a swap of two neighbours counts as one: "jordna" / "jordan")."""
    d = [[i + j if not i * j else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def respell(message: str, P, life_home) -> str:
    """The owner's message with a name they misspelled ("jordnas", "taylr", "morgna") written as the known person's name
    ("Jordan's", "Taylor", "Morgan"), so the engine's own matcher finds them. Only names of people the owner has told
    the agent about; only a word that is not an ordinary word; first letter the same; one letter off (two for names of
    seven letters or more); and exactly one person fits. Anything else: the message as it was."""
    text = str(message or '')
    try:
        index = P.load(life_home)
    except Exception:
        return text
    if not index:
        return text
    everyday = getattr(P, 'EVERYDAY', set())
    found = set(P.match(life_home, text) or [])
    names = {}                                   # name word -> slugs
    for slug, person in index.items():
        if slug in found:
            continue
        for n in [person.get('name') or ''] + list(person.get('aliases') or []):
            for w in re.findall(r"[A-Za-z]+", str(n)):
                if len(w) >= 4 and w.lower() not in everyday:
                    names.setdefault(w.lower(), {})[slug] = w
    if not names:
        return text
    words = dictionary()

    def fix(m):
        tok = m.group(0)
        base = tok.lower().replace("'", '').replace('’', '')
        forms = [(base, '')] + ([(base[:-1], "'s")] if base.endswith('s') and len(base) > 4 else [])
        for form, tail in forms:
            exact = names.get(form)
            if exact and len(exact) == 1 and base not in words:
                return next(iter(exact.values())) + tail
        if base in words or (len(forms) > 1 and forms[1][0] in words):
            return tok
        hits = {}
        for form, tail in reversed(forms):          # "jordnas" reads as Jordan's before Jordan
            for n, slugs in names.items():
                if form[0] != n[0] or abs(len(form) - len(n)) > 2:
                    continue
                if distance(form, n) <= (1 if len(n) <= 6 else 2) and (words or distance(form, n) <= 1):
                    for slug, shown in slugs.items():
                        hits.setdefault(slug, (shown + tail))
        return next(iter(hits.values())) if len(hits) == 1 else tok
    return re.sub(r"[A-Za-z][A-Za-z'’]{3,}", fix, text)


# ---- answers, read-backs and drafts ------------------------------------------------------------------------------------
# - "sounds good" to a heads-up that asked nothing must not become "do you want me to tell Jordan yes?". Code reads what
#   the agent's last message to the owner was about (the engine's lastsaid.py) and plans the turn from it (answer_plan):
#   a yes or "sounds good" right after an ask answers that ask; "sounds good" after news is an acknowledgement (nothing
#   sent, nothing asked, nothing older raised); a bare yes or no after news is asked about once.
# - after the owner's yes, the reply must not read the whole setup back. The answer's line forbids it, and trim_readback
#   cuts a sentence that recites three or more of the step's details beyond its name.
# - a nudge the owner asked for must not fold another open yes ("And yes, count me in for the 20th!") into the draft.
#   The wording line says a draft covers only what was asked about, and keep_draft_to_its_point cuts a sentence in a
#   draft that accepts or commits to a different open ask.
# - after "made it to lisbon" the reply must not ask the owner to "name a city I know". drop_name_a_city cuts that sentence.
ANSWER_LINE = (f'Run approve on that one only, with {POSS} message, and do exactly what it says. Then answer in one short line: '
               'that it is done, or, if it is not done or not on yet, what is not on yet and what happens next. Never read the '
               f'step or its settings back to {O}: they have seen them. Nothing else from your work.')
DRAFT_RULE = (f' A draft covers only what {O} asked about: it never answers, accepts or commits to anything else that is waiting '
              f'on {O}, even from the same person.'
              f' It promises nothing in {POSS} name that they have not said they will do: no offer to help, to find someone, or '
              'to do it another time.')


def _listing(keep: list) -> str:
    return (f'WHAT IS OPEN RIGHT NOW (only what {POSS} message is about; the rest is left out for this reply)\n'
            + '\n'.join(f"- {say_line(it)} [ref {it['key']}]" for it in keep)) if keep else ''


JUST_ASKED = 15 * 60      # the engine's own window (onesend.JUST_ASKED): with other asks open, only a fresh ask binds


def answer_plan(answer: str, last: dict | None, items: list, now: datetime) -> dict | None:
    """The owner's message is only an answer word. answer = 'agree' ("sounds good"), 'yes' or 'no' (the engine's own
    reading); last = what the agent's last message to the owner was about (lastsaid.last: 'asks', 'told', 'question', 'news'). -> a plan, or
    None (not known, or a shape this does not cover: the turn stays as it was). Code decides; approve still has the last word."""
    if answer not in ('agree', 'yes', 'no') or not last:
        return None
    by_key = {it['key']: it for it in items}
    asks = [by_key[k] for k in last.get('asks') or [] if k in by_key and role(by_key[k]) == 'ask']
    soon = [it for it in due_soon(items, now) if it['key'] not in (last.get('asks') or [])]
    net = (' ' + SOON_LINE + say_line(soon[0], short=True)) if soon else ''
    others = [it for it in items if role(it) == 'ask' and it['key'] not in (last.get('asks') or [])]
    fresh = now.timestamp() - float(last.get('at') or 0) <= JUST_ASKED or not others
    if len(asks) == 1 and len(last.get('asks') or []) == 1 and fresh and (answer != 'agree' or last.get('question')):
        it = asks[0]
        return {'kind': 'answer', 'keys': [it['key']] + [s['key'] for s in soon], 'listing': '', 'keep_list': True, 'about': it,
                'note': f'{HEAD}: {POSS} message answers the question you last asked them, about this: {say_line(it, short=True)} ' + ANSWER_LINE + net}
    if last.get('asks') or not last.get('news'):
        return None
    if answer == 'agree':
        told = [k for k in last.get('told') or [] if k in by_key]
        return {'kind': 'answer-ack', 'keys': told + [s['key'] for s in soon], 'listing': _listing(soon),
                'note': (f'{HEAD}: it acknowledges the last thing you told {O}, which asked them nothing. It is not a yes to anything: '
                         'nothing is sent, nothing is asked, and nothing else from your work is raised. Answer it in a few words '
                         'and stop.' + net)}
    waiting = [it for it in items if role(it) == 'ask']
    name = f', naming it ({_clip(waiting[0].get("what"), 80)})' if len(waiting) == 1 else ''
    return {'kind': 'answer-which', 'keys': [], 'listing': '', 'keep_list': True,
            'note': (f'{HEAD}: {POSS} "{answer}" does not answer a question: the last thing you told them asked them nothing. Do '
                     f'nothing yet and raise nothing else. Ask {O} once, in a few words, which thing they mean{name}.' + net)}


def _rebuild(reply: str, keep) -> str:
    """The reply with only the sentences keep(sentence) says yes to; paragraphs kept."""
    paras = []
    for para in re.split(r'\n\s*\n', str(reply or '').strip()):
        lines = []
        for line in para.split('\n'):
            kept = [s for s in SENT.split(line.strip()) if s.strip() and keep(s)]
            if kept:
                lines.append(' '.join(kept))
        if lines:
            paras.append('\n'.join(lines))
    return '\n\n'.join(paras).strip()


def quiet_reply(reply: str, p: dict, items: list, now=None, soon=None):
    """answer-ack: every sentence about an open thing outside the plan goes (the opening one too: an acknowledgement has
    nothing else to say), and so does a closing question or offer. Kept: quoted words, and a sentence about a thing still
    open that names a time before the owner's next morning text (the one they must hear tonight). Nothing left: "Got it."
    -> the new reply, or None when nothing changes."""
    if not p or p.get('kind') != 'answer-ack':
        return None
    others = _unplanned_names(p, items, '')
    by_key = {it['key']: it for it in items}

    def keep(s):
        if re.search(r'["“”]', s):
            return True
        about = [k for k, (ns, tw) in others.items() if _talks_about(s, ns, tw)]
        if not about:
            return True
        due = any(role(by_key[k]) in ('ask', 'snag', 'coming') for k in about) and (soon(s) if soon is not None else _due_soon(s, now))
        return due
    out = _rebuild(reply, keep)
    pieces = _pieces(out)
    if pieces:
        tail = pieces[-1]
        asks = tail.rstrip().endswith('?') or CLOSING_OFFER.match(tail.replace('’', '').replace("'", ''))
        if asks and not re.search(r'["“”]', tail) and not (soon(tail) if soon is not None else _due_soon(tail, now)):
            out = out[:out.rfind(tail)].rstrip()
    out = out or 'Got it.'
    return out if out != str(reply or '').strip() else None


DETAIL_SKIP = STOP | NAME_WORDS | frozenset('there here been being also again still them they then than into onto upon each every '
                                            'very got yes now just set switch switched turn turned on off its his him'.split())


def _stems(text) -> set:
    """Plain word stems of a text: lower case, a final s, es, ed or ing dropped ("closes" -> "clos", "texts" -> "text")."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", norm(text)):
        if len(w) < 3 and not any(c.isdigit() for c in w):
            continue
        out.add(re.sub(r'(?:ing|ed|es|s)$', '', w) if len(w) > 4 else w)
    return out


# After the owner's yes, the plan asks for "what is not on yet and what happens next". A reply that says when they will
# hear, then ", so <it> can start at <the set time>", must not lose the whole sentence to the read-back count (three of
# the step's own words), or the owner gets a dead end. A sentence that says when they will hear stays; only a trailing
# clause of it that recites the settings goes.
NEXT_STEP = re.compile(r"\byou(?:'ll| will) (?:hear|get a text)\b|\bi(?:'ll| will) (?:text|tell|let you know|update|ping|message|"
                       r"get back to|check back|confirm)\b|\bhear (?:from me|back)\b", re.I)
TAIL_CLAUSE = re.compile(r",\s+(?=(?:so|which|starting|from|between|every|meaning)\b)", re.I)


def _without_readback(s: str, detail: set) -> str:
    """One sentence -> itself, itself without a trailing clause that recites the settings, or '' (only a read-back)."""
    if re.search(r'["“”]', s) or len(_stems(s) & detail) < 3:
        return s
    if not NEXT_STEP.search(s.replace('’', "'")):
        return ''
    parts = TAIL_CLAUSE.split(s.strip())
    kept = [parts[0]] + [x for x in parts[1:] if NEXT_STEP.search(x.replace('’', "'")) or len(_stems(x) & detail) < 2]
    end = s.strip()[-1] if s.strip()[-1:] in '.!?' else '.'
    return ', '.join(kept).rstrip(' ,.;:!?') + end


def _remap(reply: str, fix) -> str:
    """The reply with each sentence replaced by fix(sentence) ('' drops it); paragraphs kept."""
    paras = []
    for para in re.split(r'\n\s*\n', str(reply or '').strip()):
        lines = []
        for line in para.split('\n'):
            kept = [x for x in (fix(s) for s in SENT.split(line.strip()) if s.strip()) if x]
            if kept:
                lines.append(' '.join(kept))
        if lines:
            paras.append('\n'.join(lines))
    return '\n\n'.join(paras).strip()


def trim_readback(reply: str, it: dict | None):
    """answer: a sentence that recites the step's settings back to the owner (three or more of its detail words beyond its own
    name, e.g. "at 10pm every night if it was left open") is cut, while something else is left. Quoted words stay.
    -> the new reply, or None."""
    if not it:
        return None
    name = _stems(' '.join(title_words(it) | names_of(it)) + ' ' + str(it.get('what') or ''))
    detail = _stems(' '.join(str(it.get(f) or '') for f in ('you_found', 'facts', 'owners_call', 'prepared'))) - name - _stems(' '.join(DETAIL_SKIP))
    if not detail:
        return None
    out = _remap(reply, lambda s: _without_readback(s, detail))         # the next step stays
    return out if out and out != str(reply or '').strip() else None


COMMIT = re.compile(r"\b(?:count (?:me|us|them) in|(?:i'?m|i am|we'?re|we are) in\b|(?:i'?ll|i will|we'?ll|we will) "
                    r"(?:be there|come|join|help out|volunteer|take it|take you up)|sign (?:me|us) up|(?:that )?works for (?:me|us)|"
                    r"see you (?:there|then|on)|(?:i'?d|we'?d|i would|we would) love to (?:come|join|help|be there|volunteer)|"
                    r"happy to (?:help out|come|join|volunteer)|(?:yes|yep|yeah)\W+(?:i'?m|i am|we'?re|count|i'?ll|absolutely|definitely|please)\b|"
                    r"i accept|we accept|you can count on (?:me|us)|i can (?:make it|come|be there|volunteer))", re.I)
QUOTED = re.compile(r'"([^"\n]{3,})"|“([^”\n]{3,})”')


def _numbers(text) -> set:
    return {n.lstrip('0') or '0' for n in re.findall(r"\b(\d{1,4})(?:st|nd|rd|th)?\b", str(text or ''), re.I)}


def _other_ask_in(sentence: str, others: list, msg_words: set) -> bool:
    """Does this sentence of a draft accept or commit to one of these other open asks? It must accept something
    (COMMIT), and then name that ask (its title words, a date or number in it, or most of its own prepared words), or be a
    bare acceptance (seven words or fewer) while an ask from someone the owner's message names is open."""
    if not COMMIT.search(sentence):
        return False
    said = set(re.findall(r"[a-z]{4,}", norm(sentence)))
    nums = _numbers(sentence)
    for it in others:
        text = ' '.join(str(it.get(f) or '') for f in ('what', 'you_found', 'facts', 'owners_call'))
        words = (title_words(it) | names_of(it)) - msg_words
        drafted = set(re.findall(r"[a-z]{4,}", norm(str(it.get('prepared') or '').split(':', 1)[-1])))
        if words & said or (nums and nums & _numbers(text + ' ' + str(it.get('prepared') or ''))) \
                or (drafted and len(drafted & said) >= max(2, len(drafted) // 2)):
            return True
        if len(sentence.split()) <= 7 and names_of(it) & msg_words:
            return True
    return False


def keep_draft_to_its_point(reply: str, p: dict, items: list, message: str = ''):
    """Wording help: a draft the owner asked for covers only what they asked about. A sentence inside the quoted draft (or, when
    nothing is quoted, anywhere after the first sentence) that accepts or commits to a different open ask is cut. A
    quote left empty goes with its marks. -> the new reply, or None."""
    if not p or p.get('kind') != 'ideas':
        return None
    keys = set(p.get('keys') or [])
    others = [it for it in items if role(it) == 'ask' and it['key'] not in keys]
    if not others:
        return None
    msg_words = set(re.findall(r"[a-z]{3,}", norm(message)))
    text = str(reply or '')

    def clean(inner: str) -> str:
        return ' '.join(s for s in SENT.split(inner.strip()) if s.strip() and not _other_ask_in(s, others, msg_words))

    if QUOTED.search(text):
        def fix(m):
            inner = m.group(1) if m.group(1) is not None else m.group(2)
            new = clean(inner)
            if new == ' '.join(s for s in SENT.split(inner.strip()) if s.strip()):
                return m.group(0)
            return ('"%s"' if m.group(1) is not None else '“%s”') % new if new else ''
        out = QUOTED.sub(fix, text)
    else:
        first = [True]

        def keep(s):
            if first[0]:
                first[0] = False
                return True
            return not _other_ask_in(s, others, msg_words)
        out = _rebuild(text, keep)
    out = re.sub(r'[ \t]+\n', '\n', re.sub(r'[ \t]{2,}', ' ', out)).strip()
    return out if out and out != text.strip() else None


# ---- a draft promises nothing the owner did not offer ------------------------------------------------------------------
# Asked how to word a no, a draft may end "I'm happy to help with the handoff and pitch in on the big events": a promise
# in the owner's name they never made. Softening a no with an offer is a reflex; whether to offer anything is the
# owner's call, not the draft's. After the reply, a sentence or trailing clause of a quoted draft in which the owner
# offers or promises something goes, unless the owner's own message raises an offer. A draft is never emptied.
HELPS = (r"(?:help|pitch in|chip in|step in|fill in|cover|lend a hand|support|train|show (?:them|her|him|the)|walk (?:them|her|him)|"
         r"hand (?:it|things|everything) over|find|look for|volunteer|stay on|take (?:it )?over|make it up|do it (?:again|next|another))")
OFFER = re.compile(
    r"\b(?:i|we)(?:'m| am|'re| are|'d be| would be|'ll be| will be)\s+(?:always\s+|still\s+|more than\s+|really\s+|so\s+|also\s+|totally\s+)?"
    r"(?:happy|glad|willing|keen)\s+to\s+" + HELPS + r"\b"
    r"|\b(?:i|we)(?:'m| am|'re| are|'ll be| will be)\s+(?:always\s+|still\s+)?(?:around|available|here)\s+(?:to help|if you need|for anything)"
    r"|\b(?:i|we)\s*(?:'ll|will|can|could|'d|would)\s+(?:still\s+|definitely\s+|gladly\s+|happily\s+|always\s+|also\s+|totally\s+)?"
    r"(?:love to\s+)?" + HELPS + r"\b"
    r"|\b(?:happy|glad|willing) to " + HELPS + r"\b"
    r"|\blet me know if (?:i can|you need|there'?s anything)\b|\b(?:count on me|reach out if|just shout|holler if)\b"
    r"|\b(?:maybe|perhaps|hopefully) (?:i can|i could|we can|we could|next (?:year|time)|another (?:year|time)|the (?:year|time) after)\b", re.I)
HIS_OFFER = re.compile(r"\b(?:help|helping|pitch in|chip in|step in|fill in|cover|covering|hand ?off|handover|hand over|train|training|"
                       r"volunteer|offer|offering|next time|another time|still)\b")
CLAUSE_CUT = re.compile(r",\s*(?:and|but|so|plus)\s+|\s+and\s+(?=(?:i|we)\b)|;\s*|\s+[-\u2013]\s+|,\s+(?=(?:i|we)\b)", re.I)


def _without_offer(sentence: str) -> str:
    """One sentence of a draft without the clause that offers something: the words before it when they stand alone
    (three words or more), else nothing. A sentence with no offer comes back as it was."""
    s = sentence.replace('’', "'")
    m = OFFER.search(s)
    if not m:
        return sentence
    cuts = list(CLAUSE_CUT.finditer(s[:m.start()]))
    if cuts:
        head = sentence[:cuts[-1].start()].rstrip(' ,;:-\u2013')
        if len(head.split()) >= 3:
            end = sentence.rstrip()[-1:]
            return head + (end if end in '.!' else '.')
    return ''


def drop_unoffered(reply: str, message: str = ''):
    """Wording help: each quoted draft (four words or more) without the offers the owner never made. -> (the new reply,
    [what went]) or (None, []) when nothing changes, or when the owner's own message raises an offer."""
    if HIS_OFFER.search(norm(message)):
        return None, []
    gone = []

    def fix(m):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        if len(inner.split()) < 4:
            return m.group(0)
        parts = [s for s in SENT.split(inner.strip()) if s.strip()]
        new = [_without_offer(s) for s in parts]
        if new == parts or not any(new):
            return m.group(0)
        gone.extend(s for s, n in zip(parts, new) if n != s)
        kept = ' '.join(n for n in new if n)
        return ('"%s"' if m.group(1) is not None else '“%s”') % kept

    out = QUOTED.sub(fix, str(reply or ''))
    return (out, gone) if gone and out != str(reply or '') else (None, [])


NAME_A_CITY = re.compile(r"\b(?:name|give me|tell me|send me|pick|choose|try)\b[^.!?\n]{0,60}\b(?:city|cities|place|town|time ?zones?|zones?)\b"
                         r"[^.!?\n]{0,40}\b(?:i|we) (?:know|recogni[sz]e|support|have on file)\b"
                         r"|\b(?:a|any|one of the|another) (?:city|place|town|time ?zone) (?:that )?(?:i|we) (?:know|recogni[sz]e)\b", re.I)


def drop_name_a_city(reply: str, message: str = ''):
    """The owner just said where they are: a sentence asking them to name "a city I know" goes. Nothing left: one plain line.
    -> the new reply, or None."""
    if not TRAVEL.search(norm(message)) and not re.search(r"\b(?:in|at|to)\s+[a-z]", norm(message)):
        return None
    if not NAME_A_CITY.search(str(reply or '')):
        return None
    out = _rebuild(reply, lambda s: not NAME_A_CITY.search(s))
    return out or "I'm still on home time for now."


# ---- when code cannot tell what the message is, the model picks; code still makes each plan ----------------------------
# kind() above misses many everyday phrasings (a two-word request to talk, an open "how did it go", a trip with no
# "till", a joke, a brag, a long wording request), and a turn with no plan lets the model choose its own content: a
# rundown that keeps only the ask due today, a trip reply that recites quiet hours, commentary after a draft.
# Recognising every phrasing is a judgment call, and a model call in the turn hook is too slow, so the reply model
# decides which kind it is, from a short menu whose every part code built from the list.
MENU_HEAD = ('code could not tell which kind of message this is. Decide which ONE of these it is, follow only that part, '
             'and never mention these parts or this choice:')


def menu(items: list, now: datetime, message: str = '', stopped: bool = False) -> str:
    """-> the menu line for a message kind() does not know (items = every open thing, as the plugin reads them)."""
    whole = plan('rundown', items, now, message, stopped)['note']
    whole = whole.replace(f'{HEAD}: {O} asks for the whole picture. ', '', 1)
    soon = due_soon(items, now)
    if soon:
        small = ('answer it in kind in one short line, like a friend. Then add only this one thing, which must happen before '
                 'their next morning text, in one short line with who, how much and by when, and the ask in its own words: '
                 + say_line(soon[0], short=True) + ' Nothing else from your work.')
    else:
        small = ('answer it in kind in one short line, like a friend, and stop: nothing from your work, not even that '
                 'nothing needs them.')
    return (f'{HEAD}: {MENU_HEAD}\n'
            f'1. {O[:1].upper() + O[1:]} wants the whole picture, in any words (what is going on, what is new, how things went, '
            'or only a nudge to talk): ' + whole + '\n'
            '2. Small talk with no question about your work (a hello, a goodbye or heading out, a welcome back, thanks or '
            '"ok", a joke, a brag, news about their day or their plans): ' + small + '\n'
            '3. They say where they are or that they are away: act on it with your tool, then answer in one short line: what '
            'is now set, in your own words from what the tool answered, or plainly why not. Nothing about their quiet hours, '
            'their clock or what it changes, nothing already booked for the trip, no offer, and nothing else from your work.\n'
            '4. They ask for ideas, a gift, a plan, or the words for a message to someone: one or two specific suggestions, '
            'your pick first, built on what they have told you about the people involved (what they love or are into before '
            'what is going on with them), at most 45 words, no list. Words for a message are one draft in their voice and '
            'nothing after it: no note on why it works or how they will take it, no second version, no tips, no offer to '
            'send it.' + DRAFT_RULE + '\n'
            '5. Anything else (a question about one thing, a request, a correction, an answer to something you asked): '
            'answer just that, from the list above, then stop.')


def answer_guard(reply: str, p: dict, items: list, now=None, soon=None):
    """The guard for an answer plan: no setup read back after the owner's yes; an acknowledgement stays one."""
    if not p:
        return None
    if p.get('kind') == 'answer':
        return trim_readback(reply, p.get('about'))
    if p.get('kind') == 'answer-ack':
        return quiet_reply(reply, p, items, now=now, soon=soon)
    return None

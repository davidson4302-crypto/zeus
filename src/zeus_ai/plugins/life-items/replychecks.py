"""replychecks: code looks at every chat reply before it reaches the owner, for the style slips a model keeps making.

Why: slips a person reading the text would spot at once, and code can spot too. A reply ends "Want me to draft the
note?" (a closing offer, or asking permission to help). One says "I'll flag it to my background work" (talk about the
machinery inside). Replies run long (a good personal assistant's text runs about 30 words). The wording rules already
forbid all of this; the model still does it now and then. So code checks.

The idea follows common assistant voice checks (word cap, banned stock phrases, no headings or em dashes, no
read-count receipts or tool names). The lists here are this plugin's own.

Standard library only, no Hermes, no model, nothing read or written:
  check(reply, context)   -> findings: what tripped, the matched words, where, and what code may do about it
  repair(reply, findings) -> (new reply, what was done): only the two changes code can make safely
  rewrite_problems(before, after, context) -> why a model's rewrite of a reply cannot be used (empty = it can);
                             the plugin calls this when the rewrite switch is on. Code decides, not the model.

What code may change, and nothing else:
  1. delete a whole LAST sentence that is only an offer or a stock phrase ("Want me to draft the note?",
     "Let me know if you need anything else."): either one of a closed set of sign-offs, or an offer whose first
     verb is plainly free work (draft, look into, compare). Anything else that looks like an offer may be a question
     only the owner can answer ("Want me to request the extension?"), so it is noted and left.
  2. turn an em dash into a comma
It never adds a word. It never cuts a sentence that carries a number, a time, a name, a quoted draft, a step only the
owner can say yes to (send, book, pay, cancel ...), or a statement of what is not confirmed. It never touches text
inside quotation marks: a quoted draft is the exact words the owner is being asked to approve. Everything else it finds
(too long, a machinery word inside a sentence that says something) is reported with fix 'rewrite' or 'none' and left as
it is.

The agent's own character stays: the owner's name or nickname and an occasional emoji are never findings, and an emoji
after a deleted sentence is kept. A closing offer is deleted whether or not it has the owner's name in it (the name is
style, it does not protect an offer); the setting keep_owner_name turns that back.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


def _load_settings():
    mod = sys.modules.get('settings')
    if mod is not None and hasattr(mod, 'OWNER_FIRST'):
        return mod
    spec = importlib.util.spec_from_file_location('settings', Path(__file__).resolve().parents[2] / 'engine' / 'settings.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return sys.modules.setdefault('settings', mod)


_cfg = _load_settings()          # the engine's settings module (named _cfg: `settings` below is the checks' own dict)

# ---- settings ------------------------------------------------------------------------------------------------------
# The word caps:
# - chat_words 60: an ordinary reply. Most good replies to an ordinary message are 60 words or under, and a good
#   assistant's typical text is about 30, so 60 is roomy, not tight.
# - rundown_words 75, plus words_per_ask 20 for each question in the reply: when the owner asks for the whole picture
#   ("anything need me today?", "what do you need from me") the reply has to cover every open thing, and each thing
#   waiting on the owner's word needs who, how much, by when and the ask. A flat cap cannot tell a rundown with two
#   asks from a padded one; a cap that grows with the asks does. Length is never repaired by code, only reported.
# - words inside a quoted draft (four words or more between quotation marks) are not counted: they are the message
#   the owner is approving, not the agent talking.
# - when the owner asked for something long by nature (a draft, an explanation, a comparison, steps) length is not
#   checked.
# - no_rewrite_over 150: a reply that far over a cap is not a slip, it is a long answer; it is logged, never rewritten.
DEFAULTS = {'chat_words': 60, 'rundown_words': 75, 'words_per_ask': 20, 'no_rewrite_over': 150, 'keep_owner_name': False}
# - confirm_words 30: telling the owner a small thing is set (they are away or home, they signed in again, they asked
#   to be told when a page changes). Good answers here are one line.
DEFAULTS['confirm_words'] = 30


def settings_from(given=None) -> dict:
    """DEFAULTS with any whole-number or true/false values from `given` laid over them; anything else is ignored."""
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        value = (given or {}).get(key) if isinstance(given, dict) else None
        if isinstance(default, bool):
            if isinstance(value, bool):
                out[key] = value
        elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2000:
            out[key] = value
    return out


# ---- what the owner asked for: an ordinary reply, the whole picture, or something long by nature ---------------------------
RUNDOWN = re.compile(
    r"\banything (?:else )?(?:need(?:s|ing)?|waiting (?:on|for)|for|from) me\b|\bwhat(?:'s| is|s)? (?:waiting on|needed from) me\b|"
    r"\bwhat do you need from me\b|\bneed anything from me\b|\bdo you need me\b|"
    r"\bwhat(?:'s| is|s) (?:going on|open|on (?:your|my|the) (?:plate|list|radar)|new|the latest|happening|pending|outstanding|left)\b|"
    r"\bwhat are you (?:working on|up to|doing)\b|\bwhat(?:'d| did| have) you (?:knock(?:ed)? out|get done|got done|do|done|finish(?:ed)?|handle[d]?)\b|"
    r"\bwhat(?:'s| is|s) (?:on |up |happening )?(?:for )?(?:today|tonight|tomorrow|this (?:week|weekend|morning|afternoon|evening))\b|"
    r"\bwhat(?:'s| is|s) (?:the (?:plan|agenda)|on (?:deck|tap))\b|\bwhat do i have (?:on )?(?:today|tomorrow)\b|"
    r"\bcatch me up\b|\bfill me in\b|\bbring me up to speed\b|\b(?:run ?down|recap|status|updates?|overview|summary|whole picture|big picture)\b|"
    r"\bwhere (?:do|are) (?:things|we) stand\b|\bwhere are we\b(?! (?:on|with|at)\b)|"
    # the same question in other everyday words
    r"\banything (?:that )?i (?:need|have|should|ought) (?:to )?(?:deal with|know|do|handle|look at|worry about)\b|"
    r"\bwhat do i (?:actually |really )?(?:have|need) to (?:deal with|do|handle|know)\b|"
    r"\bwhat(?:'ve| have| do)? ?you (?:have |got |have got )?(?:going|cooking)\b|\bin the works\b|\b(?:whats|what's|anything) still open\b|"
    r"\banything (?:going on|new|open|pending|slipping)\b|\bwhat are you keeping an eye on\b|"
    r"\bhow(?:'s| is|s) (?:everything|it|the \w+ stuff|your (?:pile|plate|list)) (?:going|looking)\b|"
    r"\beverything (?:you(?:'re| are|'ve| have)|that(?:'s| is)) |\bevery (?:entry|item|record)\b|\b(?:full|whole|entire) (?:\w+ )?(?:state|list|tracker)\b", re.I)
DETAIL = re.compile(
    r"\b(?:draft|write|rewrite|reword|compose|summari[sz]e|explain|walk me through|step by step|steps|in detail|details?|"
    r"tell me more|more (?:on|about)|elaborate|full (?:text|list|version|story)|list (?:all|every|out|them)|compare|comparison|"
    r"break ?down|pros and cons|research|look up|find me|read (?:me|(?:it|that|this) (?:to me|back|out))|paste|show me|"
    r"how (?:do|does|did|would|should|can) |why |itinerary|agenda|script|outline|report)\b", re.I)

# A message that tells the agent where the owner is or asks it to keep watch. The answer is one short line.
CONFIRM = re.compile(
    r"^(?:(?:i'?m|im|i am|we'?re|just|ok|okay)\W+)*(?:heading|headed|flying|driving|off|leaving|going|in|at|landed in|back in)\b[^.?!]{0,40}\b(?:till|til|until|thru|through)\b|"
    r"^(?:(?:i'?m|im|i am|we'?re|just)\W+)*(?:heading|headed|flying|driving|off) (?:out |back |down |up )?to \w+(?: \w+)?\W*$|"
    r"\b(?:signed|logged) (?:back )?(?:in|into|on)\b|"
    r"\b(?:tell|let) me (?:know )?(?:when|if|the minute|as soon as)\b|\bkeep an eye on\b|\bping me (?:when|if)\b|\bwatch (?:for|that|this|the|my)\b", re.I)


def ask_kind(asked) -> str:
    """'unknown' (no message to go on), 'detail', 'rundown' or 'chat'."""
    a = str(asked or '').replace('’', "'").strip()
    if not a:
        return 'unknown'
    if DETAIL.search(a) or len(a.split()) > 60:
        return 'detail'
    planned = _plan_kind(a)                  # the plan's reading ("where are we on everything" is a rundown)
    if planned in ('rundown', 'yesno'):
        return 'rundown'
    if planned in ('goodbye', 'arrival', 'greeting'):
        return 'chat'                          # "on calls till 4, ttyl" is a goodbye, not a confirmation
    if not RUNDOWN.search(a) and len(a.split()) <= 15 and CONFIRM.search(a):     # or 'confirm'
        return 'confirm'
    return 'rundown' if RUNDOWN.search(a) else 'chat'


# ---- the lists -----------------------------------------------------------------------------------------------------
# Stock assistant phrases, narrowed where the bare word turns up in honest sentences ("anything else", "certainly"),
# plus the customer-service ones.
STOCK = [re.compile(p, re.I) for p in (
    r"^certainly\b[,!.]", r"^absolutely\b[,!.]", r"\b(?:great|good|excellent) question\b",
    r"\bi(?:'d| would) be (?:more than )?(?:happy|glad) to\b", r"\b(?:happy|glad) to help\b",
    r"\b(?:i )?hope (?:this|that|it) helps\b", r"\bfeel free\b", r"\bdon't hesitate\b", r"\bno problem at all\b",
    r"\bis there anything else\b", r"\banything else (?:i can|you need|you'd like|i should|for you)\b",
    r"\bhow (?:else )?(?:can|may) i (?:help|assist)\b", r"\bi(?:'m| am) here to help\b",
    r"\bas an ai\b|\bas a language model\b", r"\bi apologi[sz]e for (?:the|any) (?:confusion|inconvenience)\b",
    r"\bthank you for (?:your patience|reaching out)\b", r"\bi understand your (?:frustration|concern)s?\b",
    r"\bplease (?:note|be advised)\b", r"\brest assured\b")]
# Offers and asking permission to help. Each is matched inside one sentence, with quoted text blanked out.
OFFER = [re.compile(p, re.I) for p in (
    r"^(?:want|need) me to\b", r"\b(?:would|do|did) you (?:like|want|need) (?:me|for me) to\b",
    r"\byou(?:'d| would)? (?:want|need|like) me to\b", r"^(?:should|shall) i\b", r"\b(?:should|shall) i\b(?=[^.!\n]*\?)",
    r"\bcan i help\b", r"\bi (?:can|could) (?:also )?[^.!?\n]{0,60}\bif you(?:'d| would)? (?:want|like|prefer|need)\b",
    r"\bif you(?:'d| would)? (?:want|like|prefer)\b,? i(?:'ll| can| could| will)\b",
    r"\b(?:happy|glad) to\b(?! (?:hear|see|report|say|know)\b)", r"\b(?:just )?say the word\b(?! and\b)",      # "say the word and I'll book it" is an ask
    r"\blet me know (?:if|when|whether|what|which|how) (?:you(?:'d| would)? (?:want|need|like|prefer|have)|i can|there(?:'s| is) anything)\b",
    r"\blet me know\s*[.!]?\s*$", r"\bhere if you need\b", r"\bjust (?:ask|holler|shout)\b")]
# Which of those code may DELETE when it is the last sentence. Much narrower than the lists above, on purpose: leaving
# an offer in costs a flag, cutting a real question costs the owner a decision. Two shapes only (after "So," "And," ...):
# 1. FILLER: the whole sentence is one of a closed set of sign-offs with nothing in it ("Let me know if you need
#    anything else.", "Hope this helps!"). "Let me know if you want the earlier flight" is not in the set: that is a
#    question about a real choice. Neither is a bare "Let me know."
# 2. OFFER_LEAD then FREE_VERB: an offer whose very first verb is plainly free work (draft, look into, compare ...),
#    "Want me to request the extension?" names no free work, so it may be the owner's decision and it stays.
# On top of either shape the sentence must carry nothing (see carries below).
FILLER = re.compile(
    r"(?:let me know if you(?:'d| would)? (?:want|need|like|have) (?:anything|something|any questions|questions|more|more help|help|a hand)(?: else)?(?: at all)?|"
    r"let me know if (?:there's|there is) anything(?: else)?(?: i can do| i can help with| you need)?|"
    r"let me know (?:if|how) i can help(?: with anything(?: else)?)?|(?:i )?hope (?:this|that|it) helps|"
    r"(?:please )?(?:feel free|don't hesitate) to (?:ask|reach out|ping me|shout|holler)(?: anytime| any time)?"
    r"(?: if you (?:need|want|have) (?:anything|something|questions|any questions)(?: else)?)?|"
    r"(?:is there )?anything else(?: i can (?:do|help with)| you need| you'd like| for you)?(?: today| right now)?|"
    r"how (?:else )?(?:can|may) i (?:help|assist)(?: you)?(?: today)?|(?:just )?say the word|"
    r"(?:i'm |i am )?(?:always )?here if you need (?:me|anything|a hand)|just (?:ask|holler|shout)|"
    r"(?:always )?(?:happy|glad) to help(?: with anything(?: else)?| out)?|no problem at all)[.!?]*", re.I)
OFFER_LEAD = re.compile(
    r"(?:(?:do you |would you |did you )?(?:want|need|like) (?:me|for me) to|(?:should|shall) i|"
    r"if you(?:'d| would)? (?:want|like|prefer|need),? i(?:'ll| can| could| will)|"
    r"let me know if you(?:'d| would)? (?:want|like|need) me to|"
    r"(?:i'd be |i would be |i'm |i am |always |more than )*(?:happy|glad) to|i (?:can|could))\s+", re.I)
FREE_VERB = re.compile(
    r"(?:(?:also|just|quickly|go ahead and)\s+)*(?:draft|write(?!\s+(?:back|to|him|her|them)\b)|sketch|outline|put together|pull (?:up|together)|"
    r"work up|find|look (?:up|into|for|at|around|through|again)|take a look|check(?!\s+(?:with\b|(?:(?:you|us|him|her|them|it)\s+)?(?:in|out)\b))|"
    r"research|search|scan|compare|price (?:out|both|them|those|a few|some|options)|dig (?:up|into|in|around|through|out|deeper)|summari[sz]e|"
    r"keep (?:looking|an eye|watch|track|tabs)|track|watch|remind|jot|map (?:it )?out|gather|brainstorm|suggest|come up with|narrow|"
    r"help|explain|walk you through|think (?:about|through))\b", re.I)
ASKS_HIM = re.compile(r"(?:(?:do you |would you |did you )?(?:want|need|like) (?:me|for me) to|(?:should|shall) i|let me know|"
                      r"(?:i'd be |i would be |i'm |i am |always |more than )*(?:happy|glad) to|if you(?:'d| would)? (?:want|like|prefer|need))\b", re.I)
LEAD_IN = re.compile(r"^(?:(?:so|and|or|but|also|anyway|otherwise|either way|as always|again)\b,?\s+)+", re.I)
WARM_OPENING = re.compile(r"(?:always )?(?:happy|glad) to help\b", re.I)


def free_offer(core: str) -> str:
    """The words that make this sentence deletable filler (a closed sign-off, or an offer of plainly free work that
    reaches for nothing else), or '' when it is not. core = the sentence with any "So," lead-in set aside."""
    if FILLER.fullmatch(core.strip()):
        return core.strip()
    lead = OFFER_LEAD.match(core)
    verb = FREE_VERB.match(core, lead.end()) if lead else None
    if verb and not HIS_TO_OK.search(FREE_WORK.sub(' ', core[lead.end():])):
        return core[:lead.end()]
    return ''

# Talk about its own storage ("I couldn't update my saved notes", "I couldn't
# lock that preference in"). The plugin guard cuts the same words (STORAGE in __init__.py); listed in MACHINERY below.
STORAGE = (r"\b(?:my|saved|stored) (?:notes?(?! (?:to|for|from)\b)|memory|memories|preferences?|settings|records)\b"   # not "my note to Dana"
           r"|\bI(?:'ve| have)? (?:made|jotted|taken|took) (?:a )?notes?\b|\bnoted (?:it|that|this)(?: down)? in my\b|\bnoted (?:it|that|this) down\b"
           r"|\block(?:ed|ing)? (?:that|this|it|the) (?:preference|rule|setting)s? in\b|\block(?:ed|ing)? in (?:that|this|the|your) (?:preference|rule|setting)s?\b"
           r"|\b(?:save|saved|saving|store|stored|storing|record|recorded|log|logged) (?:that|this|it) as (?:a )?(?:preference|setting|rule|note)\b"
           r"|\bpreferences? (?:is |are |was |were )?(?:saved|stored|locked|updated|file|list)\b"
           r"|\b(?:claim|say|said)(?:ing)? (?:it's|it is|it was|that's|that is) (?:saved|stored|locked in|recorded)\b"
           r"|\b(?:it|that|this) (?:may|might) not stick\b|\b(?:it|that|this) (?:won't|will not) stick\b"
           r"|\b(?:could(?:n't| not)|can(?:'t|not)|did(?:n't| not)|was(?:n't| not) able to|was unable to) (?:save|store|record|log) "
           r"(?:that|this|it|the correction|the change)\b|\bupdate (?:the|my) (?:records?|notes?)\b|\b(?:her|his|their|my) records? (?:still )?(?:says?|shows?)\b"
           r"|\b(?:update|updated|updating|change|changed|edit|edited) my (?:notes|records|memory)\b")
# Machinery: how the agent works inside. Kept narrow so that everyday words stay everyday: "tick" is a tick on the dog
# unless it is "every tick", "session" is a session with the trainer unless it is "this session", and "background
# watching" is left alone because it is the wording this plugin's own code-built summary uses with the owner.
MACHINERY = [re.compile(p, re.I) for p in (
    r"\bbackground (?:work|job|task|run|process|loop|engine|pipe|worker|agent|thread)s?\b",       # not "background check": that is hiring
    r"\bcron(?:[ -]?job)?s?\b|\blaunchd\b|\bdaemon\b|\bwebhooks?\b|\bsub-?agents?\b|\bplugins?\b",
    r"\b(?:next|every|each|last|per) tick\b|\bticks? (?:ran|runs|fired|fires|loop|cycle)\b|\b\d+[- ]minute tick\b",
    r"\b(?:this|my|chat|fresh|separate|current) session\b|\bsession (?:id|reset|restart|history|context|store|log)\b",
    r"\b(?:in|on|to|from|into) (?:my|the|a) queue\b|\bqueued(?: up)?\b|\bqueu(?:e|es|ing) (?:it|this|that|up)\b|\bwork queue\b",
    r"\btool (?:call|result|error|output|name)s?\b|\bmy tools?\b|\b(?:one|none|any|all) of my tools\b|\btoolsets?\b|"
    r"\b(?:browser|terminal|search|memory|mail|email|calendar|file|shell|web) tool\b|\btools? (?:failed|errored|returned|timed out)\b",
    r"\bMCP\b|\bComposio\b|\bAPI (?:call|key|error|limit|request|response)s?\b|\bjson\b|\byaml\b|\bpayload\b|\bschema\b|\bdedupe[ds]?\b",
    r"\bzeus-life\b|\bhermes-[a-z]+\b|\blaunchctl\b|\bcrontab\b|\bsqlite\b|\bstdout\b|\bstderr\b|\btraceback\b|"
    r"\bstack trace\b|\bexit code\b|\bstatus code\b|\berror code\b|\bstate\.db\b",
    r"\bsystem prompt\b|\bcontext window\b|\bprivate reference\b|\bmy instructions\b|\b(?:my|life|proactive|background) pipes?\b|"
    r"\bmy ledger\b|\blife[- ]items?\b|\bitem list\b|\bcronjob response\b",
    r"\[ref\s[^\]]*\]|\b(?:job|run|task|session|ref|item)(?: id)?[ :#]+(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    STORAGE,                                     # its own notes and preferences (the plugin guard has the same list)
    r"`[^`\n]+`")]
# A field or tool name: lower-case words joined by underscores (open_items, web_search). Nobody texts like that.
SNAKE = re.compile(r"(?<![\w@./-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w@])")
PATHS = re.compile(r"(?<![\w/])(?:~|/(?:Users|tmp|var|private|opt|usr|etc|Library|Applications|home))/[\w./~-]*|"
                   r"(?<![\w/@])[\w-]+\.(?:jsonl?|py|md|sqlite3?|ya?ml|sh|plist|mjs|db|log|toml|env)\b")
LINKS = re.compile(r"https?://\S+|www\.\S+|\b[\w.+-]+@[\w-]+\.[\w.-]+")
# Read-count receipts ("I checked 12 emails"): bookkeeping about the reading, not what was found.
RECEIPT = re.compile(
    r"\b(?:read|checked|searched|scanned|reviewed|consulted|went through|looked (?:at|through)|combed(?: through)?)\b[^\n.!?]{0,60}?"
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a dozen|dozens of|hundreds of)\s+(?:of (?:your|the) )?"
    r"(?:(?:saved|recent|new|unread|different|more)\s+){0,2}"
    r"(?:profiles?|memories|memory entries|searches|sources?|messages?|emails?|e-mails?|inboxes|threads?|tools?|pages?|results?|files?|"
    r"records?|calendars?|sites?|tabs?|documents?|docs)\b", re.I)
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S.*$|^\s*(?:tl;?dr|summary)\s*:|\*\*[^*\n]+\*\*|(?<!\w)__[^_\n]+__(?!\w)", re.I | re.M)
# An em dash, a spaced en dash, or two hyphens used as one. A range like "8–10am" is not one.
DASH = re.compile(r"[ \t]*(?:[—―]|(?<=\s)–(?=\s)|(?<=\s)--(?=\s))[ \t]*")

# ---- what a sentence may carry that code must never cut ------------------------------------------------------------
TIME = re.compile(
    r"\b(?:today|tonight|tomorrow|yesterday|mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday)?|fri(?:day)?|sat(?:urday)?|"
    r"sun(?:day)?|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?|noon|midnight|mornings?|afternoons?|evenings?|weekends?|weeks?|months?|years?|hours?|minutes?|"
    r"o'clock|am|pm|deadline|due|by then)\b", re.I)
# Steps that change something outside, or that only the owner can say yes to. A question about one of these is an ask
# the owner must answer, however it is phrased.
HIS_TO_OK = re.compile(
    r"\b(?:send|sending|sent|reply|respond|answer|e-?mail|text|message|call|phone|tell|ask|book|reserve|schedule|reschedule|move|"
    r"cancel|confirm|accept|decline|rsvp|pay|buy|order|purchase|charge|renew|return|refund|register|sign|submit|file|delete|remove|"
    r"forward|share|invite|post|publish|transfer|approve|add|change|update|switch|stop|pause|resume|turn|unsubscribe|subscribe|"
    r"start|place|apply|donate|tip|close|open|go ahead|proceed|calendar|appointment|reservation|meeting|payment|card|account|"
    r"subscription)\b", re.I)
FREE_WORK = re.compile(r"\b(?:draft|write(?: up)?|prepare|prep|sketch|outline|put together|pull together)\s+"
                       r"(?:(?:a|an|the|that|this|some|your|up|quick|short)\s+){0,3}[a-z-]+", re.I)   # "draft a reply" is not "reply"
NOT_CONFIRMED = re.compile(
    r"n't\b|\b(?:not|no|never|none|nothing|nobody|cannot|unable|unconfirmed|unverified|unknown|unsure|unless|without|yet|until|still|"
    r"might|may not|maybe|probably|likely|apparently|seems?|appears?|assum\w+|guess\w*|think|believe)\b", re.I)
EMOJI = re.compile('[\u2190-\u21ff\u2300-\u27bf\u2900-\u297f\u2b00-\u2bff\ufe0f\U0001f000-\U0001faff]')
# The owner's own names (first name, full name parts, owner.nicknames). None configured: nothing matches.
_OWN_NAMES = sorted({w for w in ' '.join([str(_cfg.OWNER_NAME or '')] + [str(n) for n in (_cfg.get('owner.nicknames', []) or [])]).split()
                     if w.lower() not in ('the', 'owner') and re.fullmatch(r"[A-Za-z][\w'-]*", w)}, key=len, reverse=True)
HIS_NAME = re.compile(r"\b(?:" + '|'.join(re.escape(w) for w in _OWN_NAMES) + r")\b") if _OWN_NAMES else re.compile(r'(?!x)x')
NAME = re.compile(r"(?<=[a-z0-9,;:)] )[A-Z][a-z]+")       # a capitalised word that is not the first word of the sentence
MAX_CUT_WORDS = 20                                        # a longer last sentence is saying something

# ---- reading the reply: quotes, sentences ---------------------------------------------------------------------------
ABBREV = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|Ave|Blvd|Rd|Jr|Sr|Inc|Co|Corp|Ltd|vs|etc|approx|est|Mt|Ft|Apt|Dept|a\.m|p\.m|e\.g|i\.e|U\.S)\.|"
                    r"\bNo\.(?=\s*\d)|\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.(?=\s+\d)|\b[A-Z]\.(?=\s+[A-Z])")
LIST_LEAD = re.compile(r"^\s*(?:\d+[.)]|[-•*])\s+")
SENTENCE_END = re.compile(r"(?<=[.!?])[\"”')\]]*\s+(?=[^a-z\s])")


def plain(text) -> str:
    """The reply with curly apostrophes made straight, so one pattern reads both. Same length: positions still line up."""
    return str(text or '').replace('’', "'").replace('‘', "'")


def quote_spans(text: str):
    """-> ([(start, end)] of each stretch inside double quotation marks, marks included; True when every mark has a
    partner). An odd mark out (inches, a typo) means nobody can say what is quoted, so the caller repairs nothing."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"“[^“”]*”", text)]
    paired = text.count('“') == text.count('”') == len(spans)
    marks = [m.start() for m in re.finditer(r'"', text)]
    if len(marks) % 2:
        paired = False
    for a, b in zip(marks[0::2], marks[1::2]):
        if '\n\n' in text[a:b]:
            paired = False          # a "quote" that runs across a blank line is two stray marks
        spans.append((a, b + 1))
    return sorted(spans), paired


def blank_quotes(text: str, spans) -> str:
    """The reply with what is inside quotation marks replaced by x's: same length, nothing in there for a pattern to find."""
    out = list(text)
    for a, b in spans:
        for i in range(a + 1, b - 1):
            if not out[i].isspace():
                out[i] = 'x'
    return ''.join(out)


def sentences(text: str, blanked: str) -> list:
    """The reply as sentences: [{'start', 'end', 'text', 'line', 'alone', 'decor'}], positions in the reply itself.
    A line break always ends a sentence, a list marker ("1.", "-") is not part of one, a full stop inside quotation
    marks or a known abbreviation does not end one. 'alone' = the only sentence of a list item. 'decor' = no letter
    or digit in it (an emoji on its own): not a sentence, never counted and never cut."""
    marked = ABBREV.sub(lambda m: m.group(0).replace('.', '\x00'), blanked)
    out, pos = [], 0
    for n, line in enumerate(marked.split('\n')):
        lead = LIST_LEAD.match(line)
        at, in_line = (lead.end() if lead else 0), []
        cuts = [m.end() for m in SENTENCE_END.finditer(line, at)] + [len(line)]
        for cut in cuts:
            a, b = at, cut
            while a < b and line[a].isspace():
                a += 1
            while b > a and line[b - 1].isspace():
                b -= 1
            front = re.match(r"[^\w\s\"“'(\[$€£]+\s+(?=\S)", line[a:b])     # an emoji in front of a sentence is its own piece
            for a2, b2 in (((a, a + len(front.group(0).rstrip())), (a + front.end(), b)) if front else ((a, b),)):
                if b2 > a2:
                    piece = text[pos + a2:pos + b2]
                    in_line.append({'start': pos + a2, 'end': pos + b2, 'text': piece, 'line': n, 'alone': False,
                                    'decor': not re.search(r'[A-Za-z0-9]', piece)})
            at = cut
        real = [s for s in in_line if not s['decor']]
        if lead and len(real) == 1:
            real[0]['alone'] = True
        out += in_line
        pos += len(line) + 1
    return out


def count_words(text: str, spans) -> int:
    """Words in the reply, leaving out quoted drafts (four words or more inside quotation marks)."""
    out, last = [], 0
    for a, b in spans:
        if len(text[a:b].split()) >= 4:
            out.append(text[last:a])
            last = b
    out.append(text[last:])
    return len(' '.join(out).split())


def carries(sentence: str, settings: dict, matched: str = '') -> str:
    """Why code must not delete this sentence, in plain words; '' when nothing stands in the way.
    matched = the stock words that put it up for deletion ("don't hesitate" must not count as a 'not')."""
    rest = sentence.replace(matched, ' ', 1) if matched else sentence
    if '"' in sentence or '“' in sentence:
        return 'it has quoted words in it'
    if re.search(r"\d|[$€£%]", sentence):
        return 'it has a number in it'
    if LINKS.search(sentence):
        return 'it has a link or an address in it'
    if TIME.search(rest):
        return 'it names a time'
    step = HIS_TO_OK.search(FREE_WORK.sub(' ', rest))
    if step:
        return 'it is about a step only the owner can say yes to (' + step.group(0).lower() + ')'
    if NOT_CONFIRMED.search(rest):
        return 'it says what is not done or not confirmed'
    if HIS_NAME.search(sentence):
        if settings.get('keep_owner_name', True):
            return "it has the owner's name in it"
    if NAME.search(HIS_NAME.sub('you', sentence)):
        return 'it names someone or something'
    if EMOJI.search(sentence):
        return 'it has an emoji in it'
    if len(sentence.split()) > MAX_CUT_WORDS:
        return 'it is too long to be only an offer'
    return ''


def _rangeish(token: str) -> bool:
    """A number, a time or a day: the kind of word that stands on either side of a range."""
    token = token.strip('.,;:()')
    return bool(re.search(r'\d', token) or TIME.fullmatch(token))


def _where(i: int, real: list, s: dict) -> str:
    n = len(real)
    return 'the only sentence' if n == 1 else 'last sentence' if i == n - 1 else 'first sentence' if i == 0 else f'sentence {i + 1} of {n}'


# ---- check ---------------------------------------------------------------------------------------------------------
def check(reply, context=None) -> list:
    """What in this reply would make it read like a machine, as a list of findings (empty = clean). Each finding:
      kind      'long' | 'stock' | 'offer' | 'machinery' | 'receipt' | 'heading' | 'em_dash'
      words     the matched words
      where     where they are, in plain words ("last sentence", "sentence 2 of 5", "inside quoted words")
      start,end their position in the reply
      fix       'cut' (code may delete the whole sentence), 'comma' (code may swap the dash), 'rewrite' (only a
                rewrite could fix it), 'none' (noted; leave it)
      sentence  the sentence they are in; note = why code leaves it, when it does
    context: {'asked': the owner's message (decides which word cap applies), 'settings': overrides for DEFAULTS}."""
    context = context if isinstance(context, dict) else {}
    settings = settings_from(context.get('settings'))
    raw = str(reply or '')
    if not raw.strip():
        return []
    text = plain(raw)
    spans, paired = quote_spans(text)
    blanked = blank_quotes(text, spans)
    sents = sentences(text, blanked)
    real = [s for s in sents if not s['decor']]
    found = []

    def inside_quotes(a: int, b: int) -> bool:
        return any(qa < a and b < qb for qa, qb in spans)

    def sentence_at(a: int):
        for i, s in enumerate(real):
            if s['start'] <= a < s['end']:
                return i, s
        return None, None

    # Offers and stock phrases, sentence by sentence, with quoted words blanked out.
    flagged = {}                                           # index of the sentence -> [(kind, matched words, start, end)]
    opens = {}                                             # index of the sentence -> the offer or stock words it OPENS with
    for i, s in enumerate(real):
        body = blanked[s['start']:s['end']]
        m = LEAD_IN.match(body)
        at = s['start'] + (m.end() if m else 0)            # where the sentence starts once "So," or "And" is set aside
        core = blanked[at:s['end']]
        if i == 0 and WARM_OPENING.match(core):
            continue                                       # "Happy to help!" as the first words back is a thank-you, not filler
        for kind, patterns in (('offer', OFFER), ('stock', STOCK)):
            for p in patterns:
                hit = p.search(core)
                if hit:
                    flagged.setdefault(i, []).append((kind, text[at + hit.start():at + hit.end()], at + hit.start(), at + hit.end()))
                    break
        free = free_offer(core)
        if free:
            opens[i] = text[at:at + len(free)]
        elif ASKS_HIM.match(core):
            opens[i] = None                                # it opens like an offer, but not of plainly free work
    # Which of them code may delete: only a run of such sentences at the very end, with something left before them.
    cut = set()
    if paired:
        for i in range(len(real) - 1, 0, -1):
            s = real[i]
            if i not in flagged or not opens.get(i) or s['alone'] or carries(s['text'], settings, matched=opens[i]):
                break
            cut.add(i)
    for i, hits in sorted(flagged.items()):
        s = real[i]
        for kind, words, a, b in hits:
            f = {'kind': kind, 'words': words, 'where': _where(i, real, s), 'start': a, 'end': b, 'sentence': s['text']}
            if i in cut:
                f.update(fix='cut', cut=[s['start'], s['end']])
            elif opens.get(i):                             # plainly filler, but not where or how code may delete it
                f.update(fix='rewrite', note=('quotation marks do not pair up, so code changes nothing' if not paired else
                                              'it is the whole reply' if len(real) == 1 else
                                              'it is a whole list item' if s['alone'] else
                                              carries(s['text'], settings, matched=opens[i]) or 'a sentence after it has to stay'))
            elif i in opens:
                f.update(fix='none', note='it may be a question only the owner can answer (it is not plainly an offer of free work), so it stays')
            else:
                f.update(fix='rewrite', note='it is part of a sentence that says something else, or it is not a closing sentence')
            found.append(f)

    # Machinery words, anywhere, quoted or not: a quoted error message is still machinery on the owner's phone.
    links = [(m.start(), m.end()) for m in LINKS.finditer(text)]
    seen = set()
    for p in MACHINERY + [SNAKE, PATHS]:
        for m in p.finditer(text):
            words = m.group(0)
            if not words.strip() or words.lower() in seen or any(a <= m.start() < b for a, b in links):
                continue
            seen.add(words.lower())
            i, s = sentence_at(m.start())
            found.append({'kind': 'machinery', 'words': words, 'start': m.start(), 'end': m.end(), 'fix': 'rewrite',
                          'where': 'inside quoted words' if inside_quotes(m.start(), m.end()) else _where(i, real, s) if s else 'the reply',
                          'sentence': s['text'] if s else '', 'note': 'the sentence says something, so code cannot just delete it'})

    for m in RECEIPT.finditer(blanked):
        i, s = sentence_at(m.start())
        found.append({'kind': 'receipt', 'words': text[m.start():m.end()], 'start': m.start(), 'end': m.end(), 'fix': 'rewrite',
                      'where': _where(i, real, s) if s else 'the reply', 'sentence': s['text'] if s else '',
                      'note': 'the sentence may also say what was found, so code cannot just delete it'})

    for m in HEADING.finditer(text):
        if inside_quotes(m.start(), m.end()):
            continue
        found.append({'kind': 'heading', 'words': m.group(0).strip()[:80], 'start': m.start(), 'end': m.end(), 'fix': 'rewrite',
                      'where': f"line {text.count(chr(10), 0, m.start()) + 1}", 'sentence': '', 'note': 'a heading, a label or bold: a text has none'})

    for m in DASH.finditer(text):
        dash = m.group(0).strip()
        a, b = m.start(), m.end()
        before, after = text[:a].rstrip(' \t')[-1:], text[b:b + 1]
        f = {'kind': 'em_dash', 'words': dash, 'start': a, 'end': b, 'where': 'inside quoted words' if inside_quotes(a, b) else 'the reply',
             'sentence': text[max(0, a - 25):b + 25].replace('\n', ' ').strip()}
        left, right = text[:a].split()[-1:], text[b:].split()[:1]
        if inside_quotes(a, b) or not paired:
            f.update(fix='none', note='quoted words are the exact draft; code never changes them')
        elif left and right and _rangeish(left[0]) and _rangeish(right[0]):
            f.update(fix='none', note='between two numbers or days it may be a range ("8 to 10"), and a comma would change what it says')
        elif before and after and before not in '\n' and after not in '\n' and re.match(r"[\w)\"”'%.!?,;:]", before) and re.match(r"[\w(\"“$']", after):
            f.update(fix='comma', put=' ' if before in ',;:.!?' else ', ')
        else:
            f.update(fix='rewrite', note='the dash is at the edge of a line, so a comma would not read right')
        found.append(f)

    # Length, by what the owner asked for.
    kind = ask_kind(context.get('asked'))
    words = count_words(text, spans)
    if kind != 'detail':
        offers = {f['sentence'] for f in found if f['kind'] == 'offer' and f['fix'] != 'none'}      # an offer of help is not an ask
        asks = sum(1 for s in real if s['text'].rstrip(' "”)').endswith('?') and s['text'] not in offers)
        cap = settings['chat_words'] if kind == 'chat' else settings['rundown_words'] + settings['words_per_ask'] * asks
        if kind == 'confirm':                       # a one-line confirmation
            cap = settings['confirm_words']
        if words > cap:
            label = {'chat': 'an ordinary reply', 'rundown': f"a rundown the owner asked for with {asks} question{'' if asks == 1 else 's'} in it",
                     'confirm': 'a one-line confirmation (where the owner is, a sign-in, a watch)',
                     'unknown': f"a reply to an unknown message, so the roomier rundown cap, with {asks} question{'' if asks == 1 else 's'} in it"}[kind]
            over = words > settings['no_rewrite_over']
            total = len(text.split())
            found.append({'kind': 'long', 'words': f'{words} words' + (f' of its own ({total} with the quoted draft)' if total != words else ''), 'where': 'the whole reply', 'start': 0, 'end': len(raw), 'count': words, 'cap': cap,
                          'asked_for': kind, 'fix': 'none', 'sentence': '',
                          'note': f"cap {cap} for {label}" + ('; this far over it is a long answer, not a slip, so it is only noted' if over else
                                                              '; length alone never triggers a rewrite')})
    # A rundown that opens with what needs the owner's word while something finished is still unheard.
    # context['news'] = how many finished things the owner has not heard about (the plugin counts them from the list).
    try:
        news = int(context.get('news') or 0)
    except (TypeError, ValueError):
        news = 0
    if kind == 'rundown' and news > 0 and real and _plan_kind(context.get('asked')) != 'yesno' and (ASK_FIRST.search(blanked[real[0]['start']:real[0]['end']])
                                                    or real[0]['text'].rstrip(' "”)').endswith('?')):
        found.append({'kind': 'order', 'words': real[0]['text'][:80], 'where': 'first sentence', 'start': real[0]['start'],
                      'end': real[0]['end'], 'fix': 'rewrite', 'sentence': real[0]['text'],
                      'note': f'it opens with what needs the owner\'s word while {news} finished thing{"s" if news > 1 else ""} they have not heard about should come first'})
    found.sort(key=lambda f: (f['start'], f['kind']))
    return found


_PLAN = None


def _plan_kind(asked) -> str:
    """chatplan.kind (beside this file) on the owner's message; '' when it cannot be read."""
    global _PLAN
    try:
        if _PLAN is None:
            import importlib.util
            from pathlib import Path
            spec = importlib.util.spec_from_file_location('life_items_chatplan_checks', Path(__file__).with_name('chatplan.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _PLAN = mod
        return _PLAN.kind(asked)
    except Exception:
        return ''


# The opening of a rundown that leads with an ask.
ASK_FIRST = re.compile(r"\bsay yes\b|\bneeds? (?:you|your)\b|\bwaiting on (?:you|your)\b|\byour (?:word|call|yes|ok|okay|go-ahead)\b", re.I)


# ---- repair --------------------------------------------------------------------------------------------------------
def repair(reply, findings):
    """-> (the reply after the changes code may make, [what was done]). Deterministic: the same reply and findings
    always give the same result. Only findings with fix 'cut' or 'comma' are acted on, and each is checked against
    the reply first; one that no longer lines up is skipped and said so. Never returns an empty reply."""
    raw = str(reply or '')
    if not raw.strip():
        return raw, []
    text = plain(raw)
    edits, done = [], []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        if f.get('fix') == 'cut' and isinstance(f.get('cut'), (list, tuple)) and len(f['cut']) == 2:
            a, b = int(f['cut'][0]), int(f['cut'][1])
            if 0 < a < b <= len(text) and text[a:b] == f.get('sentence'):
                if not any(e[0] == a and e[1] == b for e in edits):
                    edits.append((a, b, None, f))
            else:
                done.append({'kind': f.get('kind'), 'did': 'skipped: the sentence is not where the finding says', 'text': str(f.get('sentence'))[:200]})
        elif f.get('fix') == 'comma':
            a, b = int(f.get('start', -1)), int(f.get('end', -1))
            if 0 < a < b < len(text) and DASH.fullmatch(text[a:b]):
                edits.append((a, b, f.get('put') if f.get('put') in (' ', ', ') else ', ', f))
            else:
                done.append({'kind': 'em_dash', 'did': 'skipped: the dash is not where the finding says', 'text': str(f.get('sentence'))[:200]})
    cuts = [(a, b) for a, b, put, _ in edits if put is None]
    out = raw
    for a, b, put, f in sorted(edits, key=lambda e: -e[0]):
        if put is None:
            head, tail = out[:a].rstrip(), out[b:]
            if not head.strip():
                done.append({'kind': f['kind'], 'did': 'skipped: nothing would be left', 'text': f['sentence'][:200]})
                continue
            out = head + (' ' + tail.strip() if tail.strip() else '')
            done.append({'kind': f['kind'], 'did': 'deleted the last sentence (only an offer or a stock phrase)', 'text': f['sentence']})
        elif not any(ca <= a < cb for ca, cb in cuts):
            out = out[:a] + put + out[b:]
            done.append({'kind': 'em_dash', 'did': 'dash to a comma', 'text': f.get('sentence', '')})
    if not out.strip():
        return raw, [{'kind': 'none', 'did': 'left alone: nothing would be left', 'text': ''}]
    done.reverse()
    return out, done


# ---- a rewrite by the model is only kept if code can see it invented nothing ---------------------------------------
NUMBER = re.compile(r"[$€£]?\d[\d,.:/-]*\d|\d")


def protected_parts(reply) -> dict:
    """What a rewrite must carry over word for word: quoted drafts, questions the owner has to answer, and statements of what
    is not done or not confirmed (unless that sentence is itself the one being fixed)."""
    text = plain(reply)
    spans, _ = quote_spans(text)
    blanked = blank_quotes(text, spans)
    real = [s for s in sentences(text, blanked) if not s['decor']]
    bad = {f['sentence'] for f in check(reply) if f.get('fix') in ('rewrite', 'cut') and f.get('sentence')}
    return {'quotes': [text[a:b] for a, b in spans if len(text[a:b].split()) >= 3],
            'asks': [s['text'] for s in real if s['text'].rstrip(' "”)').endswith('?') and s['text'] not in bad],
            'unconfirmed': [s['text'] for s in real if NOT_CONFIRMED.search(blanked[s['start']:s['end']]) and s['text'] not in bad
                            and not s['text'].rstrip(' "”)').endswith('?')]}


def rewrite_problems(before, after, context=None) -> list:
    """Why a model's rewrite of `before` cannot be used, in plain words (empty = it can). Code decides, not the model:
    nothing new (no number, name, time or question), nothing protected lost, not longer, and clean on a second check.
    What code cannot see: a fact that was simply left out. Shortening means leaving things out; the protected parts
    are the ones that must not go."""
    a, b = plain(before), plain(after)
    if not b.strip():
        return ['it came back empty']
    out = []
    if len(b.split()) > len(a.split()):
        out.append('it is longer than what it replaces')
    have = {m.group(0).strip('.,:;') for m in NUMBER.finditer(a)}
    new = sorted({m.group(0).strip('.,:;') for m in NUMBER.finditer(b)} - have)
    if new:
        out.append('it has numbers that were not there: ' + ', '.join(new[:5]))
    known = set(re.findall(r"[A-Za-z][A-Za-z'-]+", a))
    known_lower = {w.lower() for w in known}
    names = sorted({m.group(0) for m in NAME.finditer(b) if m.group(0) not in known and m.group(0).lower() not in known_lower})
    if names:
        out.append('it has names that were not there: ' + ', '.join(names[:5]))
    times = sorted({m.group(0).lower() for m in TIME.finditer(b)} - {m.group(0).lower() for m in TIME.finditer(a)})
    if times:
        out.append('it names times that were not there: ' + ', '.join(times[:5]))
    spans, _ = quote_spans(b)
    asks = [s['text'] for s in sentences(b, blank_quotes(b, spans)) if not s['decor'] and s['text'].rstrip(' "”)').endswith('?') and s['text'] not in a]
    if asks:
        out.append('it asks the owner something the reply did not ask: ' + asks[0][:120])
    parts = protected_parts(before)
    for label, kind in (('a quoted draft', 'quotes'), ('a question the owner has to answer', 'asks'), ('a statement of what is not confirmed', 'unconfirmed')):
        lost = [p for p in parts[kind] if p not in b]
        if lost:
            out.append(f'it changed or dropped {label}: ' + lost[0][:120])
    left = [f for f in check(repair(after, check(after, context))[0], context) if f.get('fix') == 'rewrite']
    if left:
        out.append('it still trips: ' + '; '.join(f"{f['kind']} ({f['words']})" for f in left[:4]))
    return out

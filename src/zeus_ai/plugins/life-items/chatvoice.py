"""chatvoice: teach the chat turn HOW to sound by example, not by more rules.

Rules in the reference can get the main point right and still leave the sound stiff: rundowns past their length,
a welcome back that lists three things, a formal confirmation. chatplan.py decides WHAT to say in code; this module
shows HOW to say it with a few short example replies.

What it does to a turn, only with the switch file chat-examples-on (and chat-rules-v2, whose plan it rides on):
  1. examples   the kind of the owner's message (chatplan.kind, plus two small readers here for an answer to an ask
                and a correction) picks a type in the example library. The 3 or 4 closest examples of that type, by
                the words the owner's message shares with theirs, shortest first on a tie, with every source in the
                library represented when the type has more than one, go into the turn under one heading that says
                they show length, order and warmth only, and that every fact in them is never to be repeated.
                A message of no known kind gets no examples.
  2. shorter    the long "how to talk about it" rules are replaced by their essentials. The machinery rule is kept
                word for word (it is the safety one); the rest says the same things in a fraction of the words, and
                the plan and the reply checks (code) still decide content and catch what slips.
Facts and safety stay in code: nothing here reads the item list, decides content or touches the reply.

The example library. Config key `chat.library_file` names a JSON file (absolute, ~, or relative to the Hermes
profile folder). Unset or missing: the bundled chat-library.example.json beside this file. Neither readable: no
examples (the shorter rules still apply). Shape:

    {"examples": [{"id": "g1", "type": "greeting", "source": "a",
                   "message": "the owner's text (optional)", "reply": "the example reply"}, ...]}

Types: greeting, goodbye, arrival, thanks, ack, rundown, wording, ideas, confirm, travel, correction. `source` is any
label (for example which assistant or which person wrote the reply); with several labels each type mixes them so the
voice is the shared core, not one writer's character. Use replies you have the right to use.

Standard library only. Reads the library once (and again when it changes). Any trouble: the turn as it was.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'engine'))
    import settings
except Exception:          # settings missing: the bundled example library still works
    settings = None

SWITCH = 'chat-examples-on'
EXAMPLE_LIBRARY = Path(__file__).with_name('chat-library.example.json')
HEAD = 'HOW IT SHOULD SOUND'
MAX_EXAMPLES = 4
MAX_ONE = 380          # a longer real reply is never picked: the example must itself be the length we want
_lib = {}


def library_path() -> Path | None:
    """-> the example library to read: `chat.library_file` if set and present, else the bundled example, else None."""
    try:
        name = settings.get('chat.library_file') if settings is not None else None
        if name:
            path = Path(str(name)).expanduser()
            if not path.is_absolute() and settings is not None:
                path = Path(settings.PROFILE_DIR) / path
            if path.is_file():
                return path
    except Exception:
        pass
    return EXAMPLE_LIBRARY if EXAMPLE_LIBRARY.is_file() else None

# ---- what type of example fits the owner's message ------------------------------------------------------------------------
THANKS = re.compile(r"\b(?:thanks|thank|ty|thx|tysm|appreciate|appreciated|huge|lifesaver|best)\b")
WORDING = re.compile(r"\b(?:word|words|phrase|wording|say|tell|write|text|reply|respond|message|note|email|draft|nudge|remind|chase|sounding)\b")
ANSWER = re.compile(r"^(?:yes|yep|yeah|yup|ya|y|sure|ok do it|okay do it|do it|go ahead|go for it|send it|sounds good|approved?|"
                    r"no|nope|nah|not now|skip(?: it)?|hold off|pass|cancel it|dont)\b")
CORRECTION = re.compile(r"\bactually\b|\bi meant\b|\bthats wrong\b|\bcorrection\b|\bnot \d|\b\d[\d:apm ]* not\b")


def norm(text) -> str:
    t = str(text or '').replace('’', "'").replace('‘', "'").lower().replace("'", '')
    return ' '.join(re.sub(r"[^a-z0-9:?\s]", ' ', t).split())


def voice_type(message, kind: str, watch=None) -> str:
    """-> the library type for the owner's message, or '' for none. kind = chatplan.kind(message); watch = chatplan.WATCH."""
    t = norm(message)
    if not t:
        return ''
    if kind in ('greeting', 'goodbye', 'arrival'):
        return kind
    if kind == 'ack':
        return 'thanks' if THANKS.search(t) else 'ack'
    if kind in ('rundown', 'yesno'):
        return 'rundown'
    if kind == 'ideas':
        return 'wording' if WORDING.search(t) else 'ideas'
    if kind == 'confirm':
        return 'confirm' if watch is not None and watch.search(t) else 'travel'
    if len(t.split()) <= 14 and ANSWER.match(t):
        return 'confirm'
    if len(t.split()) <= 16 and CORRECTION.search(t):
        return 'correction'
    return ''


# ---- the closest examples ----------------------------------------------------------------------------------------------
STOP = frozenset('a an the i im me my you your to of and or for in on at is it its this that be so just do did can will was '
                 'are with up what how hey ok okay'.split())


def library() -> list:
    """-> the usable examples (each with a reply no longer than MAX_ONE), or [] when there is no library."""
    path = library_path()
    if path is None:
        return []
    key = (str(path), path.stat().st_mtime)
    if _lib.get('key') != key:
        data = json.loads(path.read_text(encoding='utf-8'))
        _lib.update(key=key, examples=[e for e in data.get('examples') or []
                                       if isinstance(e, dict) and e.get('reply') and e.get('type')
                                       and len(str(e['reply'])) <= MAX_ONE])
    return _lib['examples']


def closest(message, kind: str, n: int = MAX_EXAMPLES) -> list:
    """The n examples of that type that share the most words with the owner's message, shortest first on a tie, with
    every source of that type in when there is room (so the voice is the shared core, not one writer's character)."""
    words = set(norm(message).replace('?', ' ').split()) - STOP
    pool = [e for e in library() if e['type'] == kind]

    def score(e):
        theirs = set(norm(e.get('message') or '').replace('?', ' ').split()) - STOP
        return (-len(words & theirs), len(str(e['reply'])), str(e.get('id') or ''))
    ranked = sorted(pool, key=score)
    picked = ranked[:n]
    src = lambda e: str(e.get('source') or '')
    for want in list(dict.fromkeys(src(e) for e in ranked))[:n]:     # one of each source when the type has several
        if any(src(e) == want for e in picked):
            continue
        spare = [i for i in range(len(picked) - 1, -1, -1) if sum(src(e) == src(picked[i]) for e in picked) > 1]
        if spare:
            picked[spare[0]] = next(e for e in ranked if src(e) == want)
    return sorted(picked, key=score)


def _quote(text) -> str:
    return '"' + ' '.join(str(text).split()) + '"'


def block(message, kind: str) -> str:
    ex = closest(message, kind)
    if not ex:
        return ''
    lines = [f'{HEAD} (example texts in the voice the owner likes, picked because the owner\'s message is like the '
             'one each answered. Take only their length, their order (the answer first) and their plain warmth. Every '
             'fact, name, time and plan in them is made up or old and not the owner\'s now: never repeat or mention any '
             'of it, never let them change what you say from above, and never mention that you have examples. Sound '
             'like yourself.)']
    for e in ex:
        lines.append(f"- Owner: {_quote(e['message'])} Reply: {_quote(e['reply'])}" if e.get('message')
                     else f"- Unprompted: {_quote(e['reply'])}")
    return '\n'.join(lines)


# ---- the shorter rules -------------------------------------------------------------------------------------------------
TALK = 'HOW TO TALK ABOUT IT.'
ACT = '\nHOW TO ACT ON THE OWNER\'S REPLIES'
MACHINERY_RULE = '- The owner must never see machinery'
SHORT_RULES = (
    'HOW TO TALK ABOUT IT. This thread is the owner\'s phone, whoever seems to be asking.\n'
    '{machinery}\n'
    '- The first line answers exactly what the owner asked ("did you see X" starts with yes or no). Then only what they need, then stop. '
    'About 25 words, unless they asked for something long by nature (a draft, an explanation, a comparison, steps).\n'
    '- One subject per reply: theirs. Never add a question or a reminder about something else to it.\n'
    '- When you cannot do what they ask: one line that it is theirs to do, and what you can do instead. Give the steps only if they ask for them.\n'
    '- Text like a person: short and plain. No labels, bold, headers or em dashes. Say "today" and "tomorrow".\n'
    '- Whole picture, when no plan is given below: finished news they have not heard first, one line each; then what needs '
    'their word (who, how much, by when), no draft quoted; nothing they already heard; about 40 words. If your watching is '
    'paused, say that first. A yes-or-no question with nothing waiting on them starts with "No". With something waiting '
    'on them or a snag, never open with "nothing needs you".\n'
    '- Waiting on their word: the key facts, your pick, and what their yes will do ("say yes and I\'ll send it"). Not started '
    'or being worked on: say so, what you know in one line, and when they will hear; never as waiting on them. Hit a snag: '
    'what failed, and no claim of work you are not doing.\n'
    '- Say only what is written below or what you just checked; add no fact and no timing. Nothing on it: say so '
    'plainly, and never say you are checking unless you are.\n'
    '- Never ask whether they want help or end on an offer ("want me to", "let me know"). Do it, or say when they will '
    'have it. The one ask allowed is a prepared decision that is theirs (money, a message to someone outside, their '
    'calendar, a delete).')


def shorter(text: str) -> str | None:
    """-> the context with the talk rules cut to their essentials, or None when the section is not exactly where it
    should be (then nothing is changed)."""
    if text.count(TALK) != 1 or text.count(ACT) != 1:
        return None
    a, b = text.index(TALK), text.index(ACT)
    section = text[a:b]
    rule = [ln for ln in section.split('\n') if ln.startswith(MACHINERY_RULE)]
    if a > b or len(rule) != 1:
        return None
    return text[:a] + SHORT_RULES.format(machinery=rule[0]) + text[b:]


def turn(text: str, message, kind: str, watch=None, before=()) -> str | None:
    """-> the turn context with the shorter rules and, for a message of a known type, the examples (placed before the
    first of `before` found, else at the end), or None (nothing to change, or any trouble)."""
    try:
        out = shorter(text)
        if out is None:
            return None
        ex = block(message, voice_type(message, kind, watch))
        if ex:
            at = min((i for i in (out.find(h) for h in before) if i >= 0), default=-1)
            out = out[:at] + '\n' + ex + out[at:] if at >= 0 else out + '\n' + ex     # each `before` starts with its own newline
        return out
    except Exception:
        return None

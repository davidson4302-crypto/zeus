"""tapback: a thumbs-up tapback as the owner's yes, only for a small, free thing.

A 👍 tapback on the message that asked is a yes ONLY when a yes leads to free, undoable steps. Never for money,
purchases, bookings, a message to anyone else, calendar changes, deletes, accepting a standing OK or a rule offer: there
Zeus asks once in words. Any other emoji, or a tapback on an older message, stays not-a-yes.

How a tapback reaches Zeus (read in the Photon adapter and the owner's chat store): an ordinary message from the owner
whose text is `reaction:added:<emoji>`, stored with the provider's id `<id of the tapped message>:reaction:<n>:<n>`.

What this does, only with the switch file `tapback-yes-on` (without it nothing here is loaded and every reading is
exactly as before). hisreply.read is NOT changed: it still says a tapback is neither yes nor no, so every other reader
(the purchase lock's `zeus-buy`, a rule offer's `life rule`, a standing-OK offer, ideas, trips, the "which one stands"
question, the iMessage approvals plugin) keeps refusing it by construction. Only onesend.approve, the one road for a
prepared step, asks `read()` here, and a 👍 becomes the owner's yes there only when ALL of this holds:

  1. it is a 👍 (any skin tone). ❤️, 👎, ‼️, 😂 and the rest: not an answer, as before.
  2. the ask is free: the item is an ordinary prepared step (not an email reply, not an offer of a standing OK or of a
     rule, not an order, a refund, a trip, an idea or a clash), no step of it is one policy.py gates ("ask"), and no
     word of the ask, the prepared step or its steps reads as money, buying, a booking, a message to anyone, the
     calendar, a delete or a standing way of working (policy.guard_class, plus a deliberately wide word list here: a
     miss costs the owner one typed "yes", a wrong yes could cost money or a message in their name).
  3. it is on the message that asked: the tapped message is the note that told the owner about this item and no other
     open ask (Hermes's send journal has its id and time), or, when the tapped message is a chat reply (the journal
     has no row for it), Zeus's last message before the tapback asked about this one alone, at most 15 minutes earlier,
     with nothing from the owner in between. Any of it that cannot be read: not a yes.

When the ask is not free, the turn is told to ask the owner once, in words. Nothing here sends, writes or acts: onesend's
own code then records the yes against the owner's real message and carries out the stored step, once, as for a typed yes.
Standard library only. No model is asked.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

SWITCH = 'tapback-yes-on'
TAPBACK = re.compile(r'^reaction:added:(.*)$', re.S)
THUMBS_UP = re.compile(r'^\U0001F44D[\U0001F3FB-\U0001F3FF]?️?$')
TARGET = re.compile(r'^(.+?):reaction:')
JUST_ASKED = 15 * 60
SAME_NOTE = 5 * 60            # the note that told the owner: its send and the item's "told" stamp are this close
NOT_STEPS = ('offer:', 'standok:', 'order:', 'refund:', 'travel:', 'conflict:', 'idea:', 'ideas:', 'miss:', 'file:', 'scorecard:', 'loops:')
# Deliberately wide. Anything here keeps a 👍 from being a yes; the owner is asked once in words instead.
NEVER = re.compile(
    r"\$|\b\d+(?:\.\d\d)\b|\b(?:pay|paid|payment|buy|bought|purchas\w*|order\w*|checkout|check out|cart|price\w*|cost\w*|charge\w*|card|refund\w*|invoice\w*"
    r"|subscri\w*|renew\w*|upgrade\w*|tip|fee|fees|money|dollars?|bill\w*|book|booked|booking\w*|reserv\w*|ticket\w*|flight\w*|hotel\w*|rental"
    r"|email\w*|e-mail|mail|text|texts|texting|message\w*|reply|replies|replying|respond\w*|send|sends|sending|sent|forward\w*|call|calling|dm|ping"
    r"|contact\w*|invite\w*|rsvp|tell (?!(?:you|me)\b)\w+|ask (?!(?:you|me)\b)\w+|post|posting|publish\w*|share|sharing|submit\w*|sign|signs|signing"
    r"|calendar\w*|meeting\w*|event\w*|appointment\w*|schedul\w*|reschedul\w*|move the|accept\w*|decline\w*|cancel\w*|delete\w*|remov\w*|unsubscrib\w*"
    r"|archiv\w*|trash\w*|erase\w*|clear\w*|every time|from now on|always|standing|automat\w*|rule|rules)\b", re.I)
SAY_ASK = ('Their 👍 on that is not a yes: {why}, and that needs their yes in words. Nothing was sent or done, and this is still waiting on them. '
           'Ask them once, in a few words, whether that is a yes. If you already asked them since, do not ask again.')
SAY_NOT = ('Their {emoji} is {why}, so it is not their yes or their no. Nothing was sent or done, and this is still waiting on them. '
           'Reply only to what they did, briefly, and do not ask them for their yes again in this reply.')


ASK_IN_WORDS = ('Their message is a tapback, and a tapback is never their yes to this kind of thing (a standing way of working, or starting a '
                'build). Nothing was recorded or started, and this is still waiting on them. Ask them once, in a few words, whether that is a yes. '
                'If you already asked them since, do not ask again.')


def not_here(words, said) -> bool:
    """lifecli, for the answers that take the owner's quoted words (a rule offer's `life rule yes`, a build idea's approve): is this
    'yes' a tapback? Either the words quoted, or the owner's message they were found in. `said` is that message, or a
    function that finds it (only called with the switch). Trouble reading: only the quoted words decide."""
    try:
        if emoji_of(words) is not None:
            return True
        if callable(said):
            try:
                said = said()
            except Exception:
                return False                              # the caller's own reading then fails or decides, as before
        return bool(isinstance(said, dict) and emoji_of(said.get('text')) is not None)
    except Exception:
        return False


def on(life) -> bool:
    try:
        return (Path(life) / SWITCH).exists()
    except Exception:
        return False


def emoji_of(text):
    """'reaction:added:👍' -> '👍'; anything that is not a tapback -> None."""
    m = TAPBACK.match(str(text or '').strip())
    return m.group(1).strip() if m else None


def thumbs_up(emoji) -> bool:
    return bool(THUMBS_UP.match(str(emoji or '')))


# ---- 2. is a yes to this one free? -------------------------------------------------------------------------------------

def never_reason(key: str, p: dict, rec: dict | None):
    """-> None when a yes to this item leads only to free, undoable steps; else the plain reason it does not."""
    try:
        import policy as P
        if any(str(key).startswith(x) for x in NOT_STEPS) or p.get('offer') or p.get('standok') or p.get('rule_offer'):
            return 'it is an offer of a standing way of working or another kind of question, not a small free step'
        if (rec or {}).get('kind') == 'email_reply' or (p.get('packet') or {}).get('send'):
            return 'it sends a message to someone else'
        decision = (p.get('packet') or {}).get('decision') or {}
        texts = [str(decision.get('ask') or ''), str(decision.get('ready') or ''), str(p.get('title') or ''), str(p.get('proposed_action') or '')]
        steps = [s for s in ((p.get('decision') or {}).get('steps') or []) if isinstance(s, dict)]
        for s in steps:
            if s.get('autonomy') == 'ask' or P.guard_class(s) in P.STRICT or s.get('action_class') in P.STRICT:
                return 'one of its steps needs their yes in words (money, someone else, the calendar or a delete)'
            texts.append(str(s.get('do') or ''))
        if not any(t.strip() for t in texts[:2]):
            return 'nothing prepared says what their yes would do'
        for t in texts:
            if t.strip() and P.guard_class({'do': t, 'action_class': 'prepare_or_research'}) in P.STRICT:
                return 'it spends money, reaches someone else, changes the calendar or deletes something'
            hit = NEVER.search(t)
            if hit:
                return f'it is about "{hit.group(0)}", which is not a small free step'
        return None
    except Exception as e:
        return f'it could not be checked ({type(e).__name__})'


# ---- 3. is it on the message that asked? --------------------------------------------------------------------------------

def _store(life):
    import onesend as OS
    return OS._chat_store(life)


def _journal(life):
    return os.environ.get('LIFE_SEND_JOURNAL') or _store(life)


def target_of(life, msg: dict):
    """The provider id of the tapped message (from the tapback's own stored id) -> str, or None when unreadable."""
    store = _store(life)
    if store is None:
        return None
    with closing(sqlite3.connect(Path(store).as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        row = db.execute('SELECT platform_message_id FROM messages WHERE id=?', (msg['id'],)).fetchone()
    m = TARGET.match(str((row or [''])[0] or ''))
    return m.group(1) if m else None


def journal_row(life, provider_id: str):
    """Hermes's send journal, read-only: when the tapped message was sent -> its time, or None when it is not a note
    (a chat reply is not in the journal). Raises when the journal cannot be read."""
    path = _journal(life)
    if path is None:
        raise RuntimeError('no send journal to read')
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        row = db.execute("SELECT created_at FROM outbound_attempts WHERE provider_id=? AND kind='/send' ORDER BY created_at LIMIT 1", (provider_id,)).fetchone()
    return None if row is None else float(row[0])


def _told_at(p: dict):
    try:
        return float((p.get('told') or {}).get('at'))
    except (TypeError, ValueError):
        return None


def on_the_ask(con, life, key: str, p: dict, msg: dict, others: list) -> str | None:
    """-> None when the 👍 is on the message that asked about this one (and no other open ask); else why not."""
    import onesend as OS
    target = target_of(life, msg)
    if not target:
        return 'not on a message code can find'
    try:
        sent_at = journal_row(life, target)
    except Exception:
        return 'on a message code could not look up'
    if sent_at is not None:                               # the owner tapped a note: it must be the one that told them about this
        told = _told_at(p)
        if told is None or abs(sent_at - told) > SAME_NOTE:
            return 'on an earlier message, not the one that asked about this'
        for other in others:
            row = con.execute('SELECT payload FROM opps WHERE key=?', (other['key'],)).fetchone()
            at = _told_at(json.loads(row[0])) if row else None
            if at is None or abs(sent_at - at) <= SAME_NOTE:
                return 'on a message that asked about more than one thing'
        return None
    last = OS._last_said(con, msg, life)                   # a chat reply: it must be Zeus's last message, about this one alone
    if not last:
        return 'on a message code could not place'
    if last['asks'] != [key] or not last['question']:
        return 'on a message that did not ask about this one alone'
    if float(msg['at']) - float(last['at']) > JUST_ASKED or not OS._nothing_between(last['at'], msg, life):
        return 'on an earlier message, not the one that asked about this'
    return None


# ---- onesend.approve's hook -----------------------------------------------------------------------------------------------

def read(con, life, rec: dict, p: dict, msg: dict, others: list, reading: dict) -> dict:
    """onesend.approve, after the owner's message was read. Not a tapback, or no switch: the reading as it was. A tapback: a 👍
    that passes every check is read as a plain yes to this one ('points': it names it); anything else stays not an
    answer, with 'say': what the chat Zeus is told. Never raises: on trouble, the reading as it was."""
    try:
        if not on(life):
            return reading
        emoji = emoji_of(msg.get('text'))
        if emoji is None:
            return reading
        key = rec['item']
        out = dict(reading, answer='other', bare=False, points=False, tapback=emoji or 'a tapback')
        if not thumbs_up(emoji):
            return out | {'why': 'a tapback that is not a thumbs-up', 'say': SAY_NOT.format(emoji=emoji or 'tapback', why='a tapback, not a thumbs-up')}
        place = on_the_ask(con, life, key, p, msg, others)
        if place:
            return out | {'why': 'a thumbs-up ' + place, 'say': SAY_NOT.format(emoji='👍', why=place)}
        never = never_reason(key, p, rec)
        if never:
            return out | {'why': 'a thumbs-up, but ' + never, 'say': SAY_ASK.format(why=never)}
        return dict(reading, answer='yes', bare=True, points=True, all=False, extra=[], tapback=emoji,
                    why='a thumbs-up on the message that asked, and a yes here only does small free things')
    except Exception:
        return reading

"""standok: offer a standing OK after the same routine, non-money errand was finished twice with the owner's yes.

Why. The owner should not have to keep saying go for the same routine errand. The engine has standing OKs (`life grant`, grants.json,
policy.py turning ask into auto for a granted family), but nothing ever OFFERS one, so the owner re-approves the same routine
errand every time. Money is the other side of it: purchases stay one yes per order, for ever.

What this does, and nothing more:

  tend(con, life, now_dt)   the tick's step, after `recover`. Settles offers that were answered or ran out, then looks
                            at errands that JUST finished (the last 24 hours). An offer is made ONCE when all hold:
                              - the errand needed the owner's yes and got it (the yes is on record: the one-send record's
                                "approved" with the owner's real message, or the item's saved yes with the owner's words), and it is
                                finished (never mid-errand);
                              - Zeus has now finished that same narrow kind at least twice with the owner's yes. A kind is the
                                item's family (the same `kind_of` a standing OK is saved under) PLUS a narrow scope:
                                the kind of step and exactly who it goes to;
                              - the kind is routine and has nothing to do with money (see NEVER below);
                              - no standing OK covers it;
                              - it was never offered, or the owner's no (or 7 days of silence) is more than 30 days old.
                            The offer is ONE ordinary item, key `standok:<family>:<scope>`, ready with one yes/no
                            decision, so the note writer the owner already reads words it and every quiet rule, cap and fact
                            check applies. It rides the note that tells the owner the errand is done when one is going out
                            now; otherwise the engine's own policy times it for the next 9:00am note. Never sends.
  chat(...)                 `life grant <offer item> "<the owner's exact words>"` (lifecli hook; `approve` on an offer lands
                            here too). Code reads HIS real message (hisreply.py, the same reader every approval uses)
                            and only a clear yes writes the standing OK, through lifecli's own grants file writer, with
                            the owner's words and the message they came from. A no closes it for 30 days. Unclear: one confirm.
  step_covered(...)         policy.py hook. A standing OK that came from an offer carries `only`: the kind of step and
                            the people that were offered. A step of another kind, to anyone else, or with money, a
                            booking, the calendar or a delete in it, still waits for the owner's yes. Every other standing OK
                            (no `only`) behaves exactly as before.
  chat_line(...)            chat plugin hook: one line naming the exact command, only while an offer is open.

NEVER offered (code, not a model): a purchase, money in any form (a payment, a refund, an invoice, a price), a booking
(flights, anything with a card), a calendar change, a delete or cancel, a signature or sign-up, a message to someone
whose address is not on record or who was not the recipient both times. The purchase lock is untouched and reads only
its own buy-grants.json: nothing here can reach it.

Switch file `standing-ok-offer-on` in the life folder. Without it nobody calls tend or chat_line, nothing is read and
nothing is written. State: `standok.json` in the life folder (which kinds were offered, and what the owner answered).
Standard library only. No model is asked. Nothing here sends, and nothing here acts on an errand.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import settings

# The owner's own first name (and any configured nicknames) is never taken for a recipient's name.
_OWNER_WORDS = ''.join(re.escape(w) + '|' for w in [settings.OWNER_FIRST] + list(settings.get('owner.nicknames', []) or [])
                       if w and re.fullmatch(r'[A-Z][a-z]+', str(w)))


def _undecided(why) -> bool:
    """hisreply's reason for a reply that has not decided yet (matched by its ending, whatever subject it names)."""
    return str(why or '').endswith('not decided yet')


SWITCH = 'standing-ok-offer-on'
STATE = 'standok.json'
MARK = 'standok'
LIVE_LIFE = Path(settings.get('paths.life_home', str(settings.PROFILE_DIR / 'workspace' / 'life-engine')))   # the configured live folder (not the LIFE_HOME env override)
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
JUST_FINISHED = 24 * 3600     # "an errand just finished": closed within this long (covers a 9pm finish and the 9am note)
LOOK_BACK = 365 * 86400       # how far back finished errands of the same kind are counted
NEEDED = 2                    # finished this many times with the owner's yes
UNANSWERED_DAYS = 7           # told and no answer for this long: closed, counts like a no
QUIET_DAYS = 30               # after a no (or silence) this kind is not offered again for this long
JUST_ASKED = 15 * 60          # "sounds good" answers the offer only this soon after it (hisreply / onesend)
MESSAGE = 'message_outside_person'
NEVER_CLASSES = ('spend_money', 'calendar_change', 'delete_or_cancel')
ALLOWED = (MESSAGE,)          # the only kind of step a standing OK from an offer ever covers: a message to the same known people
# Items that are questions code put to the owner, not errands.
NOT_ERRANDS = ('offer', 'travel', 'idea', 'conflict', MARK, 'scorecard')
NOT_ERRAND_KEYS = ('offer:', 'travel:', 'conflict:', 'standok:', 'idea:', 'ideas:', 'order:', 'refund:', 'miss:', 'scorecard:', 'loops:')

# ---- what is never offered (words) -----------------------------------------------------------------------------------
# Deliberately wide: a missed offer costs the owner one more "yes"; an offer that touches money is a safety failure.
_NOT_MONEY = re.compile(r"\bin order (?:to|for|that)\b|\bin charge\b", re.I)
MONEY = re.compile(
    r"[$€£]\s?\d|\b\d[\d,.]*\s?(?:dollars?|usd|bucks|cents)\b"
    r"|\b(?:pay|pays|paid|paying|payment|payments|payable|prepaid|prepay|repay|repaid|payout|paycheck|payroll"
    r"|invoice|invoices|invoiced|invoicing|refund|refunds|refunded|refunding|reimburs\w+|purchas\w+|buy|buys|buying|bought"
    r"|checkout|shopping cart|price|prices|priced|pricing|fee|fees|deposit|deposits|charge|charges|charged|charging|billing|billed"
    r"|credit card|debit card|card on file|card number|venmo|zelle|paypal|cash ?app|wire transfer|subscription|subscriptions|subscribe"
    r"|renew|renews|renewal|renewed|upgrade|upgraded|donat\w+|cost|costs|quote|quotes|quoted|estimate|estimates|balance|overdue|past due"
    r"|owe|owes|owed|money|cash|cashier'?s check|loan|rent|lease|salary|tax|taxes|bank|budget|discount|coupon|rebate|rebates"
    r"|order|orders|ordered|ordering|reorder\w*|refill\w*|copay|deductible|premium|tuition|dues)\b", re.I)
BOOKING = re.compile(r"\b(?:book|books|booked|booking|bookings|rebook\w*|reserve|reserves|reserved|reservation|reservations|flight|flights|airline"
                     r"|airfare|fare|ticket|tickets|hotel|motel|airbnb|rental|check-?in|boarding|seat|seats|itinerary)\b", re.I)
CALENDAR = re.compile(r"\b(?:calendar|calendars|invite|invites|invitation|invitations|rsvp|reschedul\w*|decline|declines|declined"
                      r"|appointment|appointments)\b", re.I)
DELETE = re.compile(r"\b(?:delete|deletes|deleted|deleting|remove|removes|removed|erase|unsubscrib\w*|cancel|cancels|cancell?ed|cancell?ing"
                    r"|cancell?ation|terminate|terminates|close the account)\b", re.I)
COMMIT = re.compile(r"\b(?:sign (?:the|a|an|the owner's|your|it|this|that|up|and)|signs|signed|signing|e-?sign\w*|sign-?up|enroll\w*"
                    r"|register(?:s|ed|ing)?\b(?! that)|apply for|appl(?:y|ies|ied) to|authori[sz]e\w*|consent to|agree(?:s|d)? to"
                    r"|accept(?:s|ed)? the (?:terms|offer|contract|agreement|quote|estimate))\b", re.I)
ACTION_WORDS = ((MONEY, 'it touches money'), (BOOKING, 'it is a booking'), (CALENDAR, 'it changes or answers something on their calendar'),
                (DELETE, 'it deletes or cancels something'), (COMMIT, 'it signs the owner up for or commits the owner to something'))
# The words that were sent are looked at too: a message can pay, book, cancel or answer an invitation all by itself.
CALENDAR_SAID = re.compile(r"\b(?:calendar|calendars|invite|invites|invitation|invitations|rsvp|reschedul\w*|decline|declines|declined)\b", re.I)
BODY_WORDS = ((MONEY, 'the message itself is about money'), (BOOKING, 'the message itself books something'),
              (DELETE, 'the message itself cancels or deletes something'), (CALENDAR_SAID, 'the message itself answers or changes something on their calendar'))

ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w@.])\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
FROM_ADDRESS = re.compile(r"\bfrom\s+([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", re.I)
_NAME = (r"(?!(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b|(?:January|February|March|April|May|June|July|August|September|October|November|December)\b"
         r"|(?:" + _OWNER_WORDS + r"Zeus|The|This|That|These|Those|His|Her|Your|Their|You|Him|Them|It|If|When|Then|After|Before)\b)[A-Z][a-z]+\b")
SENT_TO = re.compile(r"\b(?i:to)\s+(" + _NAME + r"(?:\s+" + _NAME + r")?)")
NAMED = re.compile(r"\b(?i:to|cc|bcc|copy|copying|with|and|tell|ask|email|text|message|call|phone|contact|notify|remind|update|inform|ping|nudge|include"
                   r"|including|keeping|loop in|for)\s+(?:(?i:the|a|an)\s+)?(" + _NAME + r")")
# The offer must really have been put to the owner as a standing question, whatever words the note writer chose.
STANDING_Q = re.compile(r"\b(?:next time|from now on|from here on|going forward|in (?:the )?future|every time|each time|always|automatically"
                        r"|without (?:asking|checking|waiting)|just (?:handle|send|do)|on (?:my|its) own|by (?:myself|itself)|skip (?:the )?asking"
                        r"|stop asking|no need to ask|not ask|standing)\b", re.I)
# Said in answer to THIS question, "no need to ask" is the yes itself (lifecli.STANDING already counts it as the words of
# a standing OK), and "without asking" only repeats the question. Read alone, hisreply takes the first for a no and the
# second for a change, so both are set aside before the owner's message is read. Nothing else about reading a yes is touched.
NO_NEED = re.compile(r"\b(?:there(?:'s| is) )?no need to ask(?: me)?(?: first| again| anymore| any more| each time| every time)?\b", re.I)
ECHO = re.compile(r"\bwithout asking(?: me)?(?: first)?\b", re.I)
# Genuinely unclear answers to THIS question (the rule: one quick confirm, never a grant, never left sitting
# unasked). Thinking noise ("hmm", "hmmm...") carries no answer and is set aside first: read alone, hisreply's typo match
# takes "hmmm" for "mhmm", a yes, and a standing OK is never written on a hedge. A hedge with nothing else in it ("hmm
# maybe", "not sure", "let me think", "maybe later", "idk", "I don't know") is unclear. A 👍 tapback is never a yes to a
# standing OK: Zeus asks once in words. Any other tapback is not an answer.
HMM = re.compile(r"\b(?:h+m{2,}|u+m+|e+r+m+)\b[.…!?]*", re.I)       # not "uh": "uh huh" is a yes
HEDGE = re.compile(r"\b(?:maybe(?: later| not)?|perhaps|possibly|not sure(?: yet)?|unsure|idk|dunno|i (?:do ?n[o']?t|dont) know|no idea"
                   r"|we'?ll see|we will see|let me (?:think|check|see|get back to you)(?: about it| on it)?|(?:i'?ll|i will) think(?: about it)?"
                   r"|thinking about it|not yet|not now|ask me later)\b", re.I)
TAPBACK = re.compile(r"^\s*reaction:added:(.*)$", re.S)
THUMBS_UP = re.compile(r"^\U0001F44D[\U0001F3FB-\U0001F3FF]?️?$")
NOT_FOR_HIS_EYES = re.compile(r"[`{}\[\]<>_|]")
OWN_VOCAB = ('next time from now on going forward every each always without asking ask asks first handle these those them just send do yourself '
             'own keep needs need tell after standing same kind thing things again anymore more before').split()


def on(life) -> bool:
    try:
        return (Path(life) / SWITCH).exists()
    except (OSError, TypeError):
        return False


def is_offer(p) -> bool:
    return isinstance(p, dict) and isinstance(p.get(MARK), dict) and bool(p[MARK].get('kind'))


def _pacific():
    from ownertime import PACIFIC
    return PACIFIC


# ---- the state file ----------------------------------------------------------------------------------------------------

def _load(life) -> dict:
    try:
        d = json.loads((Path(life) / STATE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict) or not isinstance(d.get('kinds'), dict):
        d = {'kinds': {}}
    return d


def _save(life, d: dict):
    tmp = Path(life) / (STATE + '.tmp')
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
    tmp.replace(Path(life) / STATE)


def _mark(life, kind: str, **fields) -> dict:
    d = _load(life)
    rec = d['kinds'].setdefault(kind, {})
    rec.update(fields, at=time.time())
    _save(life, d)
    return rec


def unreadable_state(life) -> bool:
    """The state file is there and cannot be read: then nothing is offered (a lost "the owner said no" must never turn into
    asking again)."""
    p = Path(life) / STATE
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        return not (isinstance(d, dict) and isinstance(d.get('kinds'), dict))
    except (OSError, ValueError):
        return True


# ---- reading one finished errand -----------------------------------------------------------------------------------------

def _has_table(con, name: str) -> bool:
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _records(con, key: str) -> list:
    if not _has_table(con, 'actions'):
        return []
    import actions as A
    return A.of_item(con, key)


def _last(rec: dict, state: str) -> dict:
    return next((h for h in reversed(rec.get('history') or []) if isinstance(h, dict) and h.get('state') == state), {})


def owners_yes(p: dict, records: list) -> tuple:
    """Is the owner's yes to this item's step on record, and did the step finish? -> (status, evidence).
      'yes'      a one-send record whose "approved" carries the owner's real message and that went ("sent"; for a step the chat
                 Zeus carried out: the item is closed as done), or the item's saved yes with the owner's words on an item closed
                 as done
      'pending'  the owner's yes is there and the step is still under way or may have gone: mid-errand, nothing is offered
      'none'     no yes of the owner's on record (a standing OK ran it, the team rule sent it, chat closed it with no recorded
                 yes, the saved yes has no words, or code checked the words and found no message of the owner's)"""
    closed = p.get('closed') if isinstance(p.get('closed'), dict) else {}
    done = closed.get('state') == 'done'
    for rec in reversed([r for r in records if r.get('kind') in ('email_reply', 'step')]):
        ap = _last(rec, 'approved')
        details = rec.get('details') if isinstance(rec.get('details'), dict) else {}
        if not ap or ap.get('by') == 'team rule' or details.get('by_team') or ap.get('message_id') is None:
            continue
        ev = {'via': 'record', 'record': rec.get('id'), 'message_id': ap.get('message_id'), 'owners_words': str(ap.get('owners_words') or '')[:300],
              'at': ap.get('at'), 'kind': rec.get('kind'), 'to': [str(x) for x in (details.get('to') or []) + (details.get('cc') or []) if x],
              'body': str(details.get('body') or '') if rec.get('kind') == 'email_reply' else ''}
        if rec.get('state') == 'sent' or (rec.get('kind') == 'step' and done and rec.get('state') in ('sending', 'unknown')):
            return 'yes', ev
        if rec.get('state') in ('approved', 'sending', 'unknown'):
            return 'pending', None
    ap = p.get('approved')
    if isinstance(ap, dict) and done and not closed.get('no_recorded_yes'):
        ref = ap.get('authority_ref')
        if isinstance(ref, dict) and ref.get('ok') is False:
            return 'none', None                       # authority.py looked for the owner's message and did not find one
        send = ap.get('send')
        if isinstance(send, dict) and send.get('state') in ('sending', 'unknown'):
            return 'pending', None
        words = ' '.join(str(ap.get('owners_words') or '').split())
        if len(words) >= 2:
            mid = str((ref or {}).get('ref') or '') if isinstance(ref, dict) else ''
            return 'yes', {'via': 'item', 'message_id': mid[5:] if mid.startswith('chat:') else None, 'owners_words': words[:300], 'at': ap.get('at'),
                           'to': [], 'body': ''}
    return 'none', None


def gated_steps(p: dict) -> list:
    """The steps of this item that needed the owner's yes: [(text, class by the policy guard)]. A step a standing OK ran is
    not one. With no such step listed, the prepared decision itself is read (class None when the guard cannot tell)."""
    import policy as P
    out = []
    for s in ((p.get('decision') or {}).get('steps') or []):
        if isinstance(s, dict) and s.get('do') and s.get('autonomy') in ('ask', 'offer') and not s.get('granted'):
            out.append((str(s['do']), P.guard_class({'do': s['do'], 'action_class': s.get('action_class')})))
    if not out:
        d = (p.get('packet') or {}).get('decision') or {}
        text = ' '.join(str(d.get(k) or '') for k in ('ask', 'ready')).strip()
        if text and d.get('by') != 'owner':
            cls = P.guard_class({'do': text[:1500], 'action_class': None})
            out.append((str(d.get('ask') or text)[:400], cls if cls in NEVER_CLASSES + (MESSAGE,) else None))
    return out


def _own_addresses() -> set:
    try:
        return {str(a).lower() for a in __import__('travel').OWN_CALENDARS}
    except Exception:
        return set()


def _phones(text: str) -> list:
    out = []
    for m in PHONE.finditer(str(text or '')):
        digits = re.sub(r'\D', '', m.group(0))[-10:]
        if len(digits) == 10:
            out.append('+1' + digits)
    return out


def recipients(p: dict, ev: dict, steps: list) -> list:
    """Exactly who the approved step went to, as addresses (or phone numbers), lower case, sorted. From the one-send
    record when there is one, else the stored reply, else the addresses written in the prepared step. The owner's own
    sending address is never a recipient. Nothing found: [] (then nothing is offered)."""
    found = [m.group(0).lower() for a in (ev.get('to') or []) for m in [ADDRESS.search(str(a))] if m]
    if not found:
        send = (p.get('packet') or {}).get('send')
        if isinstance(send, dict):
            found = [m.group(0).lower() for a in list(send.get('to') or []) + list(send.get('cc') or []) for m in [ADDRESS.search(str(a))] if m]
    if not found:
        d = (p.get('packet') or {}).get('decision') or {}
        head = ' '.join([str(d.get('ask') or ''), str(d.get('ready') or '')[:400]] + [t for t, _ in steps])
        mine = {m.lower() for m in FROM_ADDRESS.findall(head)} | _own_addresses()
        found = [a.lower() for a in ADDRESS.findall(head) if a.lower() not in mine] + _phones(head)
    return sorted(set(found))


def names_in(steps: list, title: str = '') -> list:
    """First names the approved step is written to ("reply to Jordan ..."), for the wording and for telling the same
    person again later. Only what follows "to"."""
    out = []
    for text in [t for t, _ in steps] + [str(title or '')]:
        for m in SENT_TO.finditer(text):
            n = m.group(1).strip()
            if n and n not in out:
                out.append(n)
    return out[:4]


def _money_text(text: str) -> str:
    return _NOT_MONEY.sub(' ', str(text or ''))


def never_words(action_text: str, body_text: str = '') -> str:
    """'' or the plain reason this may never be handed over. action_text = what the step does (its own words, the
    title, the ask); body_text = the words that were sent (only money is looked for there)."""
    act = _money_text(action_text)
    for rx, why in ACTION_WORDS:
        if rx.search(act):
            return why
    said = _money_text(body_text)
    for rx, why in BODY_WORDS:
        if rx.search(said):
            return why
    try:
        import policy as P
        for part in re.split(r"(?<=[.;!?])\s+", str(action_text or '')):
            if part.strip() and P.guard_class({'do': part, 'action_class': 'prepare_or_research'}) in NEVER_CLASSES:
                return 'the policy guard reads it as money, a calendar change or a delete'
    except Exception:
        return 'it could not be checked against the policy guard'
    return ''


def _family(key: str) -> str:
    from lifecli import kind_of
    return kind_of(key)


def _slug(text: str, n: int = 44) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')
    return s if len(s) <= n else s[:n - 9].rstrip('-') + '-' + hashlib.sha1(s.encode()).hexdigest()[:8]


def errand(con, key: str, state: str, p: dict) -> dict | None:
    """One finished item read as an errand, or None when it is not one (a question code asked, a later check, an item
    with no step that needed the owner's yes, an item that is not closed as done)."""
    if state != 'done' or '#' in key or key.startswith(NOT_ERRAND_KEYS) or any(isinstance(p.get(k), dict) for k in NOT_ERRANDS):
        return None
    closed = p.get('closed') if isinstance(p.get('closed'), dict) else {}
    steps = gated_steps(p)
    if not steps or closed.get('state') != 'done':
        return None
    status, ev = owners_yes(p, _records(con, key))
    try:
        finished = float(closed.get('at') or 0)
    except (TypeError, ValueError):
        finished = 0.0
    e = {'key': key, 'title': ' '.join(str(p.get('title') or '').split())[:140], 'finished_at': finished, 'yes': status, 'evidence': ev,
         'family': '', 'scope': '', 'kind': '', 'classes': sorted({c or 'unknown' for _, c in steps}), 'to': [], 'names': [], 'why_not': ''}
    if status != 'yes':
        e['why_not'] = 'their yes is still being carried out' if status == 'pending' else 'no yes of their is on record for it'
        return e
    e['family'] = _family(key)
    e['to'] = recipients(p, ev, steps)
    e['names'] = names_in(steps, p.get('title'))
    d = (p.get('packet') or {}).get('decision') or {}
    send = (p.get('packet') or {}).get('send')
    body = ev.get('body') or (str(send.get('body') or '') if isinstance(send, dict) else '')
    # The prepared step as stored: its first paragraph says what is done ("Reply from ... to ... on the existing thread:"),
    # what follows a blank line is the words that were sent.
    head, _, rest = str(d.get('ready') or '').partition('\n\n')
    body = body or rest
    action = ' . '.join([t for t, _ in steps] + [e['title'], str(d.get('ask') or ''), head[:600]])
    classes = e['classes']
    if not e['family']:
        e['why_not'] = 'its kind is too wide to hand over wholesale'
    elif any(c in NEVER_CLASSES for c in classes):
        e['why_not'] = {'spend_money': 'it spends money', 'calendar_change': 'it changes their calendar', 'delete_or_cancel': 'it deletes or cancels something'}[
            next(c for c in classes if c in NEVER_CLASSES)]
    elif 'unknown' in classes:
        e['why_not'] = 'code could not tell what kind of step their yes covered'
    elif MESSAGE not in classes:
        e['why_not'] = 'it is not a message to a known person, so there is no narrow kind to name'
    else:
        e['why_not'] = never_words(action, body)
    if not e['why_not'] and not e['to']:
        e['why_not'] = 'who it went to is not on record as an address'
    if not e['why_not'] and not (ev.get('to') or isinstance(send, dict)):
        # No exact record of who it went to (the fallback): the addresses were read out of the prepared step's own words.
        # Every person the step is written to must be one of those addresses, or code does not know everyone it went to.
        mail_toks = {w for a in e['to'] if '@' in a for w in re.split(r'[^a-z]+', a.split('@')[0]) if len(w) >= 3}
        unmatched = [n for n in e['names'] if n.split()[0].lower() not in mail_toks]
        if len(unmatched) > sum('@' not in a for a in e['to']):
            e['why_not'] = 'not everyone it went to is on record as an address'
    if not e['why_not']:
        e['scope'] = _slug('msg-' + '-'.join(e['to']))
        e['kind'] = e['family'] + '|' + e['scope']
    return e


def errands(con, now: float, look_back: float = LOOK_BACK) -> list:
    """Every finished errand in the ledger that needed the owner's yes, oldest first."""
    out = []
    for key, state, payload in con.execute("SELECT key,state,payload FROM opps WHERE state='done' AND key NOT LIKE '%#%' AND updated>=?",
                                           (now - look_back,)).fetchall():
        try:
            e = errand(con, key, state, json.loads(payload))
        except Exception:
            e = None                                   # an item that cannot be read is not counted: fewer offers, never a wrong one
        if e is not None and 0 < e['finished_at'] <= now + 60:
            out.append(e)
    return sorted(out, key=lambda x: x['finished_at'])


# ---- is this kind already covered by a standing OK? ---------------------------------------------------------------------

def _grants(life) -> dict:
    try:
        g = json.loads((Path(life) / 'grants.json').read_text(encoding='utf-8'))
        return g if isinstance(g, dict) else {}
    except (OSError, ValueError):
        return {}


def _entry_for(g: dict, key: str):
    import policy as P
    fam = P.family(str(key).split('#')[0])
    return next(((f, v) for f, v in g.items() if isinstance(v, dict) and (fam == f or fam.startswith(f + ':'))), (None, None))


def covered(life, e: dict) -> bool:
    """Does a standing OK already cover this errand's kind? One without `only` (a plain `life grant`) covers its whole
    family. One that came from an offer covers only the scopes written on it."""
    fam, entry = _entry_for(_grants(life), e['key'])
    if entry is None:
        return False
    only = entry.get('only')
    if not isinstance(only, list):
        return True
    return any(isinstance(s, dict) and s.get('scope') == e['scope'] for s in only)


# ---- policy.py hook --------------------------------------------------------------------------------------------------------

def _tokens(scope: dict) -> set:
    toks = set()
    for a in scope.get('to') or []:
        a = str(a).lower()
        toks.add(a)
        toks |= {w for w in re.split(r'[^a-z]+', a.split('@')[0]) if len(w) >= 3}
    for n in scope.get('names') or []:
        toks |= {w for w in re.split(r'[^a-z]+', str(n).lower()) if len(w) >= 3}
    return toks


def _same_people(text: str, scope: dict) -> bool:
    """Is this step written to the people the standing OK names, and to nobody else? Every address in it must be one
    of theirs, every person it names after "to", "cc", "with", "and" ... must be one of them, and it must name at
    least one of them. Anything code cannot tell: no (the owner is simply asked, as before)."""
    toks = _tokens(scope)
    allowed = {str(a).lower() for a in scope.get('to') or []}
    addrs = {a.lower() for a in ADDRESS.findall(text)} | set(_phones(text))
    if not addrs <= allowed:
        return False
    named = {m.group(1).lower() for m in NAMED.finditer(text)}
    if not named <= toks:
        return False
    return bool(addrs) or bool(named)


def step_covered(grants, opp: dict, step: dict) -> bool:
    """policy.decide asks this only for a step a standing OK would turn from "ask" into "do". A standing OK without
    `only` (every one given with `life grant` on an ordinary item): True, as
    before. One that came from an offer: True only for the kind of step and the people that were offered, and never
    for a step with money, a booking, the calendar, a delete or a sign-up in it. Never raises; trouble means False
    (the owner is asked), except that a standing OK without `only` is never narrowed."""
    try:
        entry = grants.entry(opp.get('dedupe_key', '')) if grants is not None else None
        only = entry.get('only') if isinstance(entry, dict) else None
        if not isinstance(only, list):
            return True
    except Exception:
        return True
    try:
        cls, text = step.get('action_class'), str(step.get('do') or '')
        if cls not in ALLOWED or never_words(text + ' . ' + str(opp.get('title') or '')):
            return False
        for scope in only:
            if isinstance(scope, dict) and cls in (scope.get('classes') or []) and _same_people(text, scope):
                return True
    except Exception:
        pass
    return False


# ---- the offer: one ordinary life item ------------------------------------------------------------------------------------

def _clean(text) -> str:
    return ' '.join(NOT_FOR_HIS_EYES.sub(' ', str(text or '')).split())


def who_words(e: dict) -> str:
    names = [n for n in e.get('names') or [] if n]
    to = list(e.get('to') or [])
    if names and len(to) == 1:
        return f'{names[0]} ({to[0]})'
    return ', '.join(to)


def kind_words(group: list) -> str:
    """The narrow kind, in everyday words, built by code from the errands themselves."""
    examples = '; '.join(_clean(x['title']) for x in group[-2:])
    return f'messages to {who_words(group[-1])} of the same kind as the last ones the owner approved ({examples})'


def ask_text(group: list) -> str:
    """The question, as code words it. It starts in lower case on purpose (lastsaid and the notifier match capitalised
    words to tell which ask a message is about)."""
    return (f'next time a message like these goes to {who_words(group[-1])}, can Zeus just send it without asking first and tell the owner after? '
            'It is a plain yes or no.')


def packet_for(group: list) -> dict:
    n = len(group)
    examples = '; '.join(_clean(x['title']) for x in group[-2:])
    return {'status': 'needs_decision',
            'headline': f'That makes {n} of the same kind finished with their yes: {examples}.',
            'details': (f'From now on Zeus could do exactly this without asking first, and tell the owner after: {kind_words(group)}. Only this narrow kind, '
                        'and only to the same people. Anything that spends money, books or changes a booking, goes to anyone else, changes their '
                        'calendar or deletes something still waits for their yes every time.'),
            'decision': {'ask': ask_text(group),
                         'ready': ('On their yes, Zeus handles exactly this kind of thing by itself from now on and tells the owner after. '
                                   'On their no, nothing changes and the owner is not asked about it again for a month.'),
                         'default': '', 'by': 'zeus'},
            'followups': [], 'learned': []}


def _rides_now(con, errand_key: str, now_dt) -> bool:
    """Is a note going out now that tells the owner this errand is done? A finished-outcome line of this errand (its own
    small item, ready, due, with nothing to decide) between 9am and 9pm on the owner's clock."""
    if not 9 <= now_dt.hour < 21:
        return False
    for key, payload in con.execute("SELECT key,payload FROM opps WHERE state='ready' AND key LIKE ? AND surface_at<=?",
                                    (errand_key.split('#')[0] + '#%', now_dt.isoformat())).fetchall():
        try:
            if not (json.loads(payload).get('packet') or {}).get('decision'):
                return True
        except ValueError:
            continue
    return False


def make_offer(con, life, kind: str, group: list, now_dt) -> str | None:
    """Put one offer on the owner's list as an ordinary item: ready (no background job), one yes/no decision, timed by the
    engine's own policy for a routine item (the next 9:00am note) unless the note that tells the owner the errand is done is
    going out right now. -> the item key."""
    import policy as P
    newest = group[-1]
    words = kind_words(group)
    title = _clean(f'Handle without asking: messages to {who_words(newest)}')[:120]
    base = key = f"{MARK}:{newest['family']}:{newest['scope']}"
    n = 1
    while con.execute('SELECT 1 FROM opps WHERE key=?', (key,)).fetchone():
        n += 1
        key = f'{base}~{n}'                  # the same kind offered again after the owner's no ran out
    step = 'Ask the owner one yes-or-no question: may Zeus handle this narrow kind of thing without asking from now on?'
    opp = {'title': title, 'why': 'The owner has approved the same routine thing more than once; the owner could hand it over if the owner wants.',
           'evidence': '; '.join(_clean(x['title']) for x in group[-2:]), 'urgency': 'later', 'dedupe_key': key, 'confidence': 1.0,
           'owner_involved': 'yes', 'action_class': 'notify_owner_now', 'proposed_action': step, 'steps': [{'do': step, 'action_class': 'notify_owner_now'}]}
    decision = P.decide(opp, None, now_dt)      # routine item: the engine's own quiet-hours and morning-note rules set its time
    if decision.get('autonomy') == 'skip':
        raise RuntimeError('policy would not raise the offer item')
    surface, urgency = decision['surface_at'], 'later'
    if _rides_now(con, newest['key'], now_dt):
        surface, urgency = now_dt.isoformat(), 'today'
        decision = dict(decision, surface_at=surface, channel='text', reason='rides the note that tells the owner the errand is done')
    sk = {'kind': kind, 'family': newest['family'], 'scope': newest['scope'], 'classes': [MESSAGE],
          'to': list(newest['to']), 'names': list(newest['names']), 'words': words, 'ask': ask_text(group), 'day': now_dt.date().isoformat(),
          'examples': [{'key': x['key'], 'title': x['title'], 'finished_at': x['finished_at'],
                        'yes': {k: (x['evidence'] or {}).get(k) for k in ('via', 'message_id', 'owners_words', 'at')}} for x in group[-4:]]}
    _mark(life, kind, state='offered', item=key, offered_at=now_dt.timestamp(), family=sk['family'], scope=sk['scope'])   # marked BEFORE the item exists: a crash in between means never asked, not asked twice
    now = time.time()
    payload = {**opp, 'urgency': urgency, 'decision': decision, '_source': MARK, 'packet': packet_for(group), 'ready_at': now, MARK: sk}
    con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key, 'ready', decision['autonomy'], surface, urgency, 'notify_owner_now', json.dumps(payload, default=str), '[]', now, now))
    con.commit()
    return key


def _close(con, key: str, state: str, why: str, by: str = 'standing-OK offer', extra=None) -> bool:
    r = con.execute('SELECT state,payload FROM opps WHERE key=?', (key,)).fetchone()
    if r is None or r[0] not in OPEN:
        return False
    p = json.loads(r[1])
    p['closed'] = {'state': state, 'at': time.time(), 'by': by, 'why': why} | (extra or {})
    cur = con.execute('UPDATE opps SET state=?, payload=?, updated=? WHERE key=? AND state=?', (state, json.dumps(p, default=str), time.time(), key, r[0]))
    con.commit()
    return cur.rowcount == 1


def offer_rows(con) -> list:
    rows = con.execute("SELECT key,state,payload FROM opps WHERE json_extract(payload,'$.standok.kind') IS NOT NULL ORDER BY first_seen").fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


def _settle(con, life, now: float, out: dict):
    """Offers that were answered, closed some other way, or ran out. A no, a close without an answer, an offer that
    never reached the owner's list and 7 days of silence all mean: not offered again for 30 days."""
    kinds = _load(life)['kinds']
    rows = {key: (state, p) for key, state, p in offer_rows(con)}
    for kind, rec in list(kinds.items()):
        if rec.get('state') != 'offered':
            continue
        state, p = rows.get(rec.get('item'), (None, None))
        if p is None:
            _mark(life, kind, state='unanswered', quiet_from=now, why='its offer never reached their list')
            continue
        cl = p.get('closed') if isinstance(p.get('closed'), dict) else {}
        if state in OPEN and not cl:
            told = float((p.get('told') or {}).get('at') or 0)
            fam, entry = _entry_for(_grants(life), (p[MARK].get('examples') or [{}])[-1].get('key') or '')
            if entry is not None and (not isinstance(entry.get('only'), list) or any(isinstance(s, dict) and s.get('scope') == p[MARK].get('scope') for s in entry['only'])):
                if _close(con, rec['item'], 'moot', 'the owner already gave a standing OK for this kind'):
                    _mark(life, kind, state='covered')
                    out.setdefault('closed', []).append(rec['item'])
            elif state == 'surfaced' and told and now - told > UNANSWERED_DAYS * 86400:
                if _close(con, rec['item'], 'moot', f'no answer in {UNANSWERED_DAYS} days'):
                    _mark(life, kind, state='unanswered', quiet_from=now, why=f'told the owner and no answer in {UNANSWERED_DAYS} days')
                    out.setdefault('closed', []).append(rec['item'])
        elif cl.get(MARK) == 'granted':
            _mark(life, kind, state='yes')
        elif state == 'declined':
            _mark(life, kind, state='no', quiet_from=float(cl.get('at') or now), why=str(cl.get('why') or '')[:200])
        else:
            _mark(life, kind, state='unanswered', quiet_from=float(cl.get('at') or now), why=f'closed without their yes or no ({state})')


def blocked(con, life, kind: str, group: list, now: float) -> str:
    """'' when this kind may be offered now, else the plain reason it is not."""
    newest = group[-1]
    rec = _load(life)['kinds'].get(kind) or {}
    st = rec.get('state')
    if st in ('offered', 'yes', 'covered'):
        return 'it was already offered' if st == 'offered' else 'the owner already answered yes to it'
    if st in ('no', 'unanswered') and now - float(rec.get('quiet_from') or rec.get('at') or now) < QUIET_DAYS * 86400:
        return f'the owner said no (or did not answer) less than {QUIET_DAYS} days ago'
    if any(is_offer(p) and p[MARK].get('kind') == kind and state in OPEN for _, state, p in offer_rows(con)):
        return 'an offer for it is already open'
    if covered(life, newest):
        return 'a standing OK already covers it'
    try:
        import prefs as F
        if F.is_muted(life, newest['key']) or F.is_muted(life, f"{MARK}:{newest['family']}:{newest['scope']}"):
            return 'the owner said to stop raising this kind of thing'
    except Exception:
        return 'their preferences could not be read'
    if con.execute("SELECT 1 FROM opps WHERE key LIKE ? AND state='executing'", (newest['key'].split('#')[0] + '#%',)).fetchone():
        return 'a later check of that errand is still being worked'
    return ''


def candidates(con, now: float) -> dict:
    """kind -> the finished errands of that kind that may be handed over, oldest first (only kinds with one that just
    finished). Reads only; the replay uses it too."""
    if not con.execute("SELECT 1 FROM opps WHERE state='done' AND CAST(json_extract(payload,'$.closed.at') AS REAL)>=? LIMIT 1",
                       (now - JUST_FINISHED,)).fetchone():
        return {}                                   # nothing finished lately: the cheap way out, every other tick
    by_kind = {}
    for e in errands(con, now):
        if not e['why_not']:
            by_kind.setdefault(e['kind'], []).append(e)
    return {k: g for k, g in by_kind.items() if len(g) >= NEEDED and now - g[-1]['finished_at'] <= JUST_FINISHED}


def tend(con, life, now_dt=None, *, discover=True) -> dict:
    """Once a tick (tick.run_phases, after recover). -> a small report, {} when nothing happened. Never sends."""
    life = Path(life)
    if not on(life):
        return {}
    now_dt = now_dt or datetime.now(_pacific())
    now = now_dt.timestamp()
    out = {}
    if unreadable_state(life):
        return {'errors': ['the state file could not be read, so nothing was offered']}
    try:
        _settle(con, life, now, out)
    except Exception as e:                           # nothing is offered on a run that could not settle the earlier offers first
        return out | {'errors': [f'{type(e).__name__}: {str(e)[:120]}']}
    if discover:
        offered = due_offers(con, life, now_dt)
        errors = out.get('errors', []) + offered.pop('errors', [])
        out.update(offered)
        if errors:
            out['errors'] = errors
    return out


def due_offers(con, life, now_dt):
    """Stage a scoped offer. An accepted owner reply is still required before any grant."""
    if unreadable_state(life):
        return {'errors': ['the standing permission state could not be read']}
    now = now_dt.timestamp()
    out = {}
    try:
        found = candidates(con, now)
    except Exception as error:
        return {'errors': [f'{type(error).__name__}: {str(error)[:120]}']}
    for kind, group in found.items():
        try:
            if blocked(con, life, kind, group, now):
                continue
            key = make_offer(con, life, kind, group, now_dt)
            if key:
                out.setdefault('offered', []).append(key)
        except Exception as e:
            out.setdefault('errors', []).append(f'{kind[:60]}: {type(e).__name__}: {str(e)[:120]}')
    return out


# ---- the owner's answer (lifecli hook) -------------------------------------------------------------------------------------------

def _chat_store(life):
    store = os.environ.get('LIFE_CHAT_DB') or None
    if store is None and Path(life).resolve() != LIVE_LIFE.resolve():
        return None                                   # a sandbox never reads the owner's real chat
    import chats as C
    return store or C.STORE


def asks_this(text: str, sk: dict) -> bool:
    """Did this message really put the standing question to the owner, about this kind? It must ask about "next time" /
    "from now on" / "without asking" in some words, and name who it is about."""
    t = str(text or '')
    if not STANDING_Q.search(t):
        return False
    low = t.lower()
    return any(re.search(r'(?<![a-z0-9])' + re.escape(tok) + r'(?![a-z0-9])', low) for tok in _tokens(sk))


def _put_to_him(con, r, p: dict, read_chat) -> tuple:
    """When and in what words the offer was put to the owner -> (asked_at, [note texts on record], [Zeus's own chat messages
    since]). asked_at 0.0: it was never put to the owner (not told by a note, and no message of Zeus's in the owner's chat asks it)."""
    sk = p[MARK]
    told = p.get('told') if isinstance(p.get('told'), dict) else {}
    first = float(con.execute('SELECT first_seen FROM opps WHERE key=?', (r['key'],)).fetchone()[0] or 0)
    texts = []
    asked_at = float(told.get('at') or 0) if r['state'] == 'surfaced' else 0.0
    if asked_at:
        if told.get('note'):
            row = con.execute('SELECT text FROM outbox WHERE key=?', (told['note'],)).fetchone()
            if row and row[0]:
                texts.append(str(row[0]))
        if told.get('said'):
            texts.append(str(told['said']))
    said = [m for m in read_chat((asked_at - 120 if asked_at else first) - 1) if m['who'] == 'zeus']
    if not asked_at:
        hit = next((m for m in said if asks_this(m['text'], sk)), None)      # the chat Zeus put it to the owner itself
        if hit is None:
            return 0.0, [], []
        asked_at = float(hit['at'])
    return asked_at, texts, said


def _used_before(con, life, message_id, key: str) -> str:
    """Has this very message of the owner's already decided something else? One message decides one thing."""
    if message_id is None:
        return ''
    if _has_table(con, 'actions'):
        for item, history in con.execute('SELECT item, history FROM actions WHERE updated>=?', (time.time() - 8 * 86400,)).fetchall():
            try:
                hist = json.loads(history or '[]')
            except ValueError:
                continue
            if item != key and any(str(h.get('message_id')) == str(message_id) and h.get('state') in ('approved', 'declined', 'replaced') for h in hist if isinstance(h, dict)):
                return item
    row = con.execute("SELECT key FROM opps WHERE key!=? AND json_extract(payload,'$.approved.authority_ref.ref')=?", (key, f'chat:{message_id}')).fetchone()
    if row:
        return row[0]
    for fam, g in _grants(life).items():
        if isinstance(g, dict):
            ids = [g.get('message_id')] + [s.get('message_id') for s in (g.get('only') or []) if isinstance(s, dict)]
            if any(i is not None and str(i) == str(message_id) for i in ids):
                return 'the standing OK for ' + fam
    return ''


def _read(text: str) -> dict:
    """The owner's message read alone by hisreply, with the two phrases that only restate this question set aside first.
    "no need to ask" with nothing against it is the owner's yes."""
    import hisreply as H
    raw = str(text or '').replace('’', "'")
    tap = TAPBACK.match(raw) or (re.match(r'^\s*(\S+)\s*$', raw) if THUMBS_UP.match(raw.strip()) else None)   # a tapback, or a 👍 typed alone
    if tap:
        emoji = tap.group(1).strip()
        if THUMBS_UP.match(emoji):
            return {'answer': 'other', 'why': 'a thumbs-up is never their yes to a standing OK; ask once in words', 'hedge': True, 'said': ''}
        return {'answer': 'other', 'why': 'a tapback is not an answer', 'said': ''}
    rest = HMM.sub(' ', ECHO.sub(' ', NO_NEED.sub(' ', raw)))
    if HEDGE.search(rest) and not re.search(r'[a-z0-9]', HEDGE.sub(' ', rest), re.I):
        return {'answer': 'other', 'why': 'the owner has not decided yet', 'hedge': True, 'said': rest}
    if not re.search(r'[a-z0-9]', rest, re.I) and HMM.search(raw):
        return {'answer': 'other', 'why': 'the owner has not decided yet', 'hedge': True, 'said': rest}
    if rest == raw:
        reading = dict(H.read(text), said=raw)
    else:
        reading = H.read(rest)
        if NO_NEED.search(raw) and reading['answer'] == 'other' and reading['why'] in ('there are no words in it', 'it does not say yes or no') \
                and not reading.get('extra'):
            return dict(reading, answer='yes', bare=True, why='the owner said there is no need to ask', said=rest)
        reading = dict(reading, said=rest)
    if HEDGE.search(rest) and reading['answer'] == 'yes':
        return dict(reading, answer='other', why='it says yes and is not sure', hedge=True)    # "yes I guess, maybe": one confirm
    if reading['answer'] == 'other' and _undecided(reading.get('why')):
        reading = dict(reading, hedge=True)
    return reading


def _vocab(p: dict) -> set:
    import hisreply as H
    sk = p[MARK]
    parts = []
    for a in sk.get('to') or []:
        parts += re.split(r'[@._\-+]', str(a))
    return H.vocabulary(p.get('title'), sk.get('ask'), sk.get('words'), parts, sk.get('names'), list(OWN_VOCAB))


def held_for_offer(con, life, key: str) -> str:
    """lifecli hook, `life grant <an ordinary item>`: '' unless an offer about this very kind of item is open. Then the owner's
    answer belongs to the offer (the narrow standing OK the owner was actually asked about), and this says which command to run."""
    try:
        fam = _family(key)
        for okey, state, p in offer_rows(con):
            if state in ('surfaced', 'ready') and not p.get('closed') and is_offer(p) and fam and p[MARK].get('family') == fam:
                return (f'An offer about exactly this kind of thing is open, and their answer belongs to it: life grant {okey} "<their exact words>". '
                        'It records the narrow standing OK the owner was asked about, nothing wider. Nothing was changed.')
    except Exception:
        pass
    return ''


def show_extra(p: dict, key: str, state: str, life=None) -> dict:
    if state not in ('surfaced', 'ready') or p.get('closed'):
        return {}
    return {'when_he_says_yes': (f'Run: life grant {key} "<their exact words>". Code reads their real message and, on a clear yes, records the standing OK '
                                 'for exactly this narrow kind, nothing wider. Do not use approve or done for this one. An "ok" or a thank-you alone is not a yes.'),
            'when_he_is_unsure': (f'Run the same: life grant {key} "<their exact words>". A maybe, "not sure" or a 👍 gets ONE short question from you; code '
                                  'records that it was asked and says so if it already was.'),
            'when_he_says_no': f'Run: life drop {key} "<their words>". Nothing changes and the owner is not asked about this kind again for a month.'}


def chat_line(life, tool: str) -> str:
    """'' or one line for the chat Zeus, only while an offer is open (ready or told, not closed): the exact command for
    the owner's yes and for the owner's no. Reads the ledger read-only."""
    life = Path(life)
    if not on(life) or not (life / 'ledger.sqlite3').is_file():
        return ''
    with closing(sqlite3.connect((life / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=0.4)) as db:
        rows = db.execute("SELECT key,state,payload FROM opps WHERE state IN ('surfaced','ready') AND json_extract(payload,'$.standok.kind') IS NOT NULL "
                          'ORDER BY first_seen LIMIT 3').fetchall()
    lines = []
    for key, state, payload in rows:
        p = json.loads(payload)
        if p.get('closed') or not is_offer(p):
            continue
        told = 'the owner has it' if state == 'surfaced' else 'the owner has not heard it yet; put it to the owner only after the thing it is about is done and told'
        lines.append(f'One open thing is an offer, not a prepared step [ref {key}] ({told}): may you handle {p[MARK].get("words")} without asking from now on. '
                     f'Whenever the owner answers THAT question in any way (a yes, a no, a maybe or "not sure", a 👍): `{tool} grant {key} "<their message, word for word>"`. '
                     'It reads their real message itself and tells you what to do: a clear yes records the standing OK for exactly that narrow kind (then tell '
                     'the owner so in one short line); a no closes it; an unclear answer gets ONE short question from you, and code says whether you may still ask. '
                     f'For a plain no, `{tool} drop {key} "<their words>"` works too, and the owner is not asked again for a month. '
                     'An "ok" or thanks alone is not a yes, and a 👍 is never a yes to this. Never ask the owner about it twice on your own. Never use approve or done '
                     'for it, never widen it, and never offer this for anything else: only code makes these offers, and never for anything with money in it.')
    return '\n'.join(lines)


def chat(cmd: str, con, r, p: dict, words: str, life, close, grants_load, grants_save, read_chat=None, now: float | None = None) -> int:
    """Hook in lifecli.main for an item that is a standing-OK offer. `grant` (and `approve`): the owner's answer, read from the owner's
    real message. `done` / `redraft`: refused, they would lose the owner's answer. `ungrant`: the owner took it back."""
    life = Path(life)
    key = r['key']
    if cmd == 'ungrant':
        return _ungrant(con, life, key, p, grants_load, grants_save)
    if cmd in ('done', 'redraft'):
        print(f'This one is an offer to handle a kind of thing without asking from now on, not a prepared step. If the owner said yes: '
              f'life grant {key} "<their exact words>". If the owner said no: life drop {key} "<their words>". If the owner has not answered, leave it. Nothing was changed.')
        return 2
    return answer(con, r, p, words, life, close, grants_load, grants_save, read_chat=read_chat, now=now)


def answer(con, r, p: dict, words: str, life, close, grants_load, grants_save, read_chat=None, now: float | None = None) -> int:
    """`life grant <offer item> "<the owner's words>"`. Prints what the chat Zeus should do or tell the owner. Only a clear yes in a
    real message of the owner's, sent after the offer was put to the owner, writes anything."""
    import hisreply as H
    import lastsaid as LS
    life = Path(life)
    key, sk = r['key'], p[MARK]
    now = time.time() if now is None else float(now)
    if not on(life):
        print('Standing-OK offers are switched off, so nothing was changed.')
        return 2
    if p.get('closed') or r['state'] not in ('surfaced', 'ready'):
        print('This offer is not waiting on the owner any more, so nothing was changed. If the owner wants this kind of thing handled without asking, '
              'the owner can say so about the next one when it comes up.')
        return 3
    rec = _load(life)['kinds'].get(sk.get('kind')) or {}
    family = str(sk.get('family') or '')
    classes = [c for c in sk.get('classes') or []]
    if (rec.get('state') != 'offered' or rec.get('item') != key or family.count(':') < 1 or classes != [MESSAGE] or not sk.get('to')
            or rec.get('family') != family or rec.get('scope') != sk.get('scope')):
        print('This offer does not match what was put on record when it was made, so no standing OK was recorded. Nothing was changed. Tell the builder.')
        return 1
    if len(str(words or '').strip()) < 1:
        print(f'Quote what the owner said: life grant {key} "<their exact words>". Nothing was changed.')
        return 2
    store = _chat_store(life)
    try:
        if read_chat is None:
            if store is None:
                raise RuntimeError('a sandbox has no chat store of its own')
            import chats as C
            read_chat = lambda since: C.messages(since, path=store)      # noqa: E731
        asked_at, noted, said = _put_to_him(con, r, p, read_chat)
        if not asked_at:
            print('This offer has not been put to the owner yet, so their words cannot be an answer to it. Nothing was changed.')
            return 3
        msgs = [m for m in read_chat(asked_at - 1) if m['who'] == 'owner' and float(m['at']) <= now + 1]
    except Exception as e:
        print(f'Could not check their words against their real messages ({type(e).__name__}), so no standing OK was recorded. Nothing was changed.')
        return 1
    # The quoted words only help pick among the owner's messages since the offer: the newest one that carries them, and when
    # none does (a tidied typo, a short "y"), simply the owner's newest one. What the owner really wrote decides, never the quote (the
    # same rule as onesend.owners_reply).
    quote = H._flat(words)
    hits = [m for m in msgs if quote and quote in H._flat(m['text'])]
    msg = hits[-1] if hits else (msgs[-1] if msgs else None)
    if msg is None:
        print('The owner has sent no message since this was put to the owner, so no standing OK was recorded. Nothing was changed. If the owner did answer, ask the owner once more.')
        return 3
    for m in msgs:
        if (m['at'], m['id']) > (msg['at'], msg['id']):
            later = _read(m['text'])
            if later['answer'] in ('no', 'changed') or (_undecided(later['why']) or later['why'] == 'it says yes and is not sure'):
                print(f'After that the owner wrote: "{m["text"][:200]}". So that yes does not stand, and nothing was changed. Go by their newest message.')
                return 9
    others = {k: v for k, v in LS.open_asks(con).items() if k != key}
    reading = H.bind(_read(msg['text']), _vocab(p), [H.vocabulary(*v) for v in others.values()])
    try:
        last = LS.last(con, float(msg['at']), store=store) if store else None
    except Exception:
        last = None
    fresh_this = bool(last and last['asks'] == [key] and float(msg['at']) - last['at'] <= JUST_ASKED)
    ans = reading['answer']
    if ans == 'other' and not reading.get('hedge') and H.agrees(reading.get('said') or msg['text']):
        between = [m for m in msgs if last and last['at'] <= m['at'] and (m['at'], m['id']) < (msg['at'], msg['id'])]
        ans = 'yes' if fresh_this and last['question'] and not between else 'other'
    elif ans in ('yes', 'no') and others and not reading.get('points') and not fresh_this:
        print('The owner has more than one thing waiting on their word and this answer does not say which. Ask the owner once, in a few words, whether the owner means '
              f'handling {_short(sk)} without asking. Nothing was changed.')
        return 2
    elif ans in ('yes', 'no') and not reading.get('points') and last is not None and last['asks'] != [key] and not _just_asked_in_chat(said, msg, last, sk):
        # The same care onesend takes with a bare yes after news: Zeus's last message to the owner was about something
        # else, so this yes may not answer the offer at all. A standing OK is never written on a guess.
        print(f'Their "{msg["text"][:80]}" came right after a message of yours that was not about this offer, so it may not answer it. '
              f'Nothing was changed. Ask the owner once, in a few words, whether that is their {ans} to handling {_short(sk)} without asking from now on.')
        return 2
    if ans in ('yes', 'no'):
        used = _used_before(con, life, msg.get('id'), key)
        if used:
            print(f'That same message of their already decided another one ({used}). One message decides one thing, so nothing was changed here. '
                  'If the owner means this one too, the owner has to say so.')
            return 9
    if ans == 'no':
        close(con, r, 'declined', 'zeus chat, their no to a standing-OK offer', 'the owner said no: ' + msg['text'][:200])
        _mark(life, sk['kind'], state='no', quiet_from=now, owners_words=msg['text'][:300], message_id=msg.get('id'))
        print('Recorded their no. Nothing changes: this kind of thing keeps waiting for their yes each time, and the owner is not asked about it again for a month.')
        return 0
    if ans != 'yes':
        return _unclear(con, key, p, msg, reading if ans == reading['answer'] else dict(reading, why='it only agrees, and not right after the question'), sk)
    # What the owner was sent must really have asked this. The note's own text when it is on record, and anything Zeus said in
    # the owner's chat before the owner's answer; with no record of the note's words at all, the code-worded ask stands in (as travel.py does).
    seen = (noted or [str(sk.get('ask') or '')]) + [m['text'] for m in said if (m['at'], m['id']) < (msg['at'], msg['id'])]
    if not any(asks_this(t, sk) for t in seen):
        print('What the owner was sent did not ask this question in plain words, so their yes cannot be read as a standing OK. Nothing was changed. '
              f'Ask the owner once, in your own voice: {sk.get("ask")}')
        return 3
    # ---- the owner's clear yes, in a real message sent after the offer: the standing OK, exactly as wide as the offer ----
    g = grants_load()
    entry = g.get(family) if isinstance(g.get(family), dict) else None
    scope = {'scope': sk['scope'], 'classes': classes, 'to': list(sk.get('to') or []), 'names': list(sk.get('names') or []), 'words': sk.get('words'),
             'owners_words': msg['text'][:600], 'message_id': msg.get('id'), 'said_at': msg['at'], 'offer': key, 'asked': sk.get('ask')}
    if entry is not None and not isinstance(entry.get('only'), list):
        close(con, r, 'done', 'zeus chat, their answer to a standing-OK offer', 'a wider standing OK of theirs already covers this kind', {MARK: 'covered'})
        _mark(life, sk['kind'], state='covered')
        print('The owner already gave a wider standing OK for this kind of item, so nothing was added. Tell the owner it is already handled without asking.')
        return 0
    if entry is not None:
        entry['only'] = [s for s in entry['only'] if not (isinstance(s, dict) and s.get('scope') == sk['scope'])] + [scope]
    else:
        g[family] = {'at': datetime.now().astimezone().isoformat(), 'note': msg['text'][:600], 'owners_words': msg['text'][:600], 'message_id': msg.get('id'),
                     'said_at': msg['at'], 'title': ('Only ' + str(sk.get('words') or ''))[:300], 'from_item': key, 'only': [scope]}
    grants_save(g)
    try:
        import prefs as F
        F.note(life, f"Standing OK, only for {sk.get('words')}: \"{msg['text'][:200]}\"", key)
    except Exception:
        pass
    # Not closed "by zeus chat": the after-the-yes readback (actcheck.py) reads those as a prepared step that was carried out.
    close(con, r, 'done', 'zeus chat, their answer to a standing-OK offer', 'the owner said yes: this narrow kind is handled without asking from now on',
          {MARK: 'granted', 'family': family, 'scope': sk['scope'], 'message_id': msg.get('id')})
    _mark(life, sk['kind'], state='yes', owners_words=msg['text'][:300], message_id=msg.get('id'))
    print(f'Standing OK recorded, only for {sk.get("words")}. From now on your pipes do that step without asking and tell the owner after. '
          'It covers nothing else: anything with money, a booking, their calendar, a delete, or anyone else in it still waits for their yes. '
          f'Tell the owner that in one short line. If the owner takes it back: life ungrant {key}')
    return 0


def _just_asked_in_chat(said: list, msg: dict, last: dict, sk: dict) -> bool:
    """Was Zeus's newest message before the owner's answer this very question, asked in the owner's chat (an offer the chat put to the owner
    itself is not yet marked as told, so lastsaid does not list it)?"""
    before = [m for m in said if (m['at'], m['id']) < (msg['at'], msg['id'])]
    return bool(before) and float(before[-1]['at']) >= float(last['at']) - 90 and asks_this(before[-1]['text'], sk)


def _short(sk: dict) -> str:
    return f"messages to {', '.join(sk.get('names') or sk.get('to') or [])}"


def _unclear(con, key: str, p: dict, msg: dict, reading: dict, sk: dict) -> int:
    """Not a clear yes or no. A genuinely unclear reply (mixed, a hedge such as "hmm maybe" / "not sure" / "let me
    think", or a 👍 tapback) gets ONE quick confirm, asked by the chat Zeus in this same turn (the owner just texted, so it is
    part of answering the owner and every quiet rule holds by construction). After that a second unclear reply, or silence,
    leaves it open and the 7-day "unanswered" rule in _settle closes it (30 days of no offer). Anything else ("ok",
    thanks, another emoji, a question) is not an answer to it: left open, no question."""
    asked = p[MARK].get('confirm') if isinstance(p[MARK].get('confirm'), dict) else None
    why = str(reading.get('why') or 'it does not say yes or no')
    mixed = reading.get('answer') == 'changed' or why in ('it says yes and no',) or why.startswith('it has words in it') or bool(reading.get('hedge'))
    if not mixed:
        print(f'Their message ("{msg["text"][:200]}") is not a yes or a no to this ({why}), so no standing OK was recorded. Nothing was changed. '
              'Reply only to what the owner actually said, and leave this one open.')
        return 2
    if asked and str(asked.get('message_id')) != str(msg.get('id')):
        print('You already asked the owner once whether the owner means it. Leave it open and do not ask again. Nothing was changed.')
        return 2
    if not asked:
        p[MARK]['confirm'] = {'at': time.time(), 'message_id': msg.get('id')}
        con.execute('UPDATE opps SET payload=? WHERE key=?', (json.dumps(p, default=str), key))
        con.commit()
    print(f'Their answer is not a plain yes ({why}), so no standing OK was recorded. Ask the owner once, in one short line, whether the owner wants '
          f'{_short(sk)} handled without asking from now on, exactly as offered. Nothing was changed.')
    return 2


def _ungrant(con, life, key: str, p: dict, grants_load, grants_save) -> int:
    sk = p[MARK]
    g = grants_load()
    family = str(sk.get('family') or '')
    entry = g.get(family) if isinstance(g.get(family), dict) else None
    only = entry.get('only') if entry else None
    if not isinstance(only, list) or not any(isinstance(s, dict) and s.get('scope') == sk.get('scope') for s in only):
        print('There is no standing OK from this offer.')
        return 1
    entry['only'] = [s for s in only if not (isinstance(s, dict) and s.get('scope') == sk.get('scope'))]
    if not entry['only']:
        del g[family]
    grants_save(g)
    _mark(life, sk['kind'], state='no', quiet_from=time.time(), why='the owner took the standing OK back')
    try:
        __import__('recover').revoked(con, life, family)   # recover.py (switch recover-on): open items of this kind that have not started wait for the owner's yes again
    except Exception:
        pass
    print(f'Standing OK removed ({sk.get("words")}). This kind of thing waits for their yes again.')
    return 0


# ---- read-only look (the replay) -------------------------------------------------------------------------------------------

def replay(con, life=None, now: float | None = None) -> list:
    """Every finished item with a step that needed the owner's yes, oldest first, with the kind code gives it and whether an
    offer would have been made the moment it finished (walking through time, no state file, nothing written)."""
    now = time.time() if now is None else float(now)
    rows, seen, offered = [], {}, set()
    for e in errands(con, now, look_back=10 * LOOK_BACK):
        row = {k: e[k] for k in ('key', 'title', 'finished_at', 'yes', 'family', 'scope', 'kind', 'classes', 'to', 'why_not')}
        row['would_offer'] = False
        if not e['why_not']:
            seen.setdefault(e['kind'], []).append(e)
            if len(seen[e['kind']]) >= NEEDED and e['kind'] not in offered and not (life and covered(life, e)):
                row['would_offer'] = True
                offered.add(e['kind'])
            row['count_of_kind'] = len(seen[e['kind']])
        rows.append(row)
    return rows

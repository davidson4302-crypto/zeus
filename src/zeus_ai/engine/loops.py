"""loops: who is waiting on the owner, what the owner promised, and who owes THE OWNER an answer.

The noticer sees each new email once, inside a 2-day window, so a reply it passed over, or one the owner meant to get to,
would never come back, and "who's waiting on me?" would have no ranked answer. This module keeps a lasting list,
with ages, of email threads where someone is waiting on the owner, and it catches the owner's own sent-mail promises ("I'll send
the quote Friday") when the owner has allowed Zeus to read their sent mail.

Code decides which loops exist and when they close. The model only does two things: pull the owner's promises out of the owner's own
sent words (each kept only when its exact quote is found in what the owner typed), and, through the ordinary note writer,
word each line. Nothing here sends anything.

Three kinds of loop, one per email thread (the deterministic thread reading is `analyze`, no model):
  owe_reply  the last real message is THEIRS, it is addressed to the owner (not a copy), it asks the owner something, and it is
             from a real person (no lists, no bots, no calendar notices, not the owner's own agents or team). A thread the owner never
             wrote in counts only when the sender is someone the owner already writes to (the inbox feed tags "known
             sender" / "replied to you") or a colleague at one of the owner's own domains. Age counts from the OLDEST of
             their unanswered messages, so a "bumping this" never restarts the clock.
  waiting    the last real message is THE OWNER'S, it asks a real question (a "?"; "let me know if you need anything" waits on
             nobody), it went to real people, and they had written in the thread before: a first email of the owner's is
             outreach, not a conversation.
  promise    a sentence in the owner's own sent words where the owner commits to something ("I'll send the quote Friday"), with the
             due day when the owner named one. One model pass per thread; the quote must be in the owner's own words, in the first
             person, and a named weekday must match the due date, or it is not kept.

A loop closes by itself when the turn flips: the owner answers in the thread with more than a bare "thanks" or "got it"
("Will do" counts), they answer the owner's question, the owner writes again in the thread after a promise, the owner emails that person
anywhere after they wrote (the owner's sent list), or the life item made for it is closed in chat. Nothing is deleted:
closed loops keep why and when.

Things the owner already settled. If the owner tells Zeus "I already spoke to Jordan. Disregard that.", the reply to Jordan must
not come back as "Jordan, day 5" with a fresh draft. So a loop does not open, and an open one closes, when the owner already
settled that thread or that person AFTER their newest message in it (`settled`):
  - an earlier life item about the same conversation (one of its real messages, not a bounce or notice in the thread,
    or the whole thread) that the owner dropped, muted, finished in chat or answered themself (fresh.closed_by_him, the
    engine's own test for that, or a muted family);
  - an earlier dropped item whose closing words, the owner's own, say the owner handled it with that person ("I already spoke to
    Jordan", "disregard Jordan");
  - the owner's own chat words to Zeus (chats.py, read-only) saying the same: the owner spoke to, called, texted or answered the
    person, or said to drop it, naming them; or "I called them" / "disregard that" right after Zeus's message that
    named exactly one of these people. A question, a "not yet" or a "will" is not handled.
Code decides; no model is asked. A newer message from them after the owner settled it is new, so it can open again. A sandbox
without its own pretend chat store (LIFE_CHAT_DB) never reads the owner's real one.

The lines (no daily list): 9am to 8pm the owner's clock (ownertime), a loop becomes a line on the first run after it is worth
one (promises due today or late, then people waiting on the owner, then what others owe the owner), at most 3 new lines a day, a
loop at most once a day. Its thread is re-read first, so a line the owner already answered vanishes before the owner sees it. Each
line is ONE ordinary life item of its own, worded by the note writer like every other item. A person waiting on the owner
(at most 2 a day) gets an ordinary "reply to X" item instead: the background job drafts the reply in the owner's voice
(writingstyle.py), one-send freezes it, and it is sent only after the owner's yes, through the same path as any other reply.
That job is handed the thread's last few messages as the last read saw them, in the item's evidence, so a short honest
draft never waits on the job's own tools; no extra read is made for it. Nothing new is created before 8:30am or after
8pm the owner's clock, and nothing reaches the owner before quiet hours end (the notifier's own rule). A loop is shown on at most 3
days; after that it stays on `life loops`.

Left out by code: a thread the inbox feed marks as owned by someone else, mail the owner is only copied on, an answer that
repeats the owner's own questions back as headings, a long newest message that asks nothing (an answer or update supersedes
the question before it; a short nudge does not), conditional sales offers ("if you're building a room, I'll help you
plan it"), and the owner's outreach sequences (only the owner's replies are read from the owner's sent list). Each of these is pinned by a test.

Inbound mail comes from an optional inbox feed: a read-only SQLite file (config `mail.inbox_db`) with a table
`seen(mailbox, message_id, thread_id, at, sender, subject, verdict, reason)`, where verdict is 'surface', 'filtered'
or 'owned' and reason is a short tag line ("known sender", "replied to you", ...). Without it only the owner's sent mail is read.

Costs: every thread read is one Composio call. At most MAX_READS a tick (sent-mail lists included), open loops are
re-read at most every RECHECK_H hours, promises at most MAX_EXTRACT model calls a tick. Only the mailboxes in the
engine's accounts are read.

Switch: the file `loops-on` in the life folder. Without it nothing here runs, no table is created, and the engine is
exactly what it was. `life loops` lists them; `life loops close <n> "<why>"` closes one.
Tables in the ledger: loops, loop_threads, loop_sent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402

SWITCH = 'loops-on'
WINDOW_DAYS = 14             # how far back mail is read for loops
MAX_AGE_DAYS = 30            # a loop older than this closes quietly: too old to chase
MAX_READS = 4                # Composio calls a tick (thread reads and sent-mail lists)
MAX_EXTRACT = 2              # promise model calls a tick
RECHECK_H = 12               # an open loop's thread is re-read at most this often
FRESH_FOR_NOTE_H = 2         # a line's thread is re-read before it is told unless it was read this recently
SENT_EVERY_H = 6             # the owner's sent-mail list, per mailbox
OWE_SHOW_H, WAIT_SHOW_H, PROMISE_SHOW_H = 24, 72, 48
MAX_LINES = 3                # new lines a day (each its own item)
MAX_DRAFTS = 2               # "reply to X" items opened a day
MAX_SHOWN = 3                # days a loop is shown on
NUDGE_WORDS = 25             # a newest message this short ("bumping this, thanks!") keeps the question before it open
MAX_READ_FAILS = 3           # a thread that cannot be read this many times (deleted, moved) is left until new mail arrives
TAIL_MSGS, TAIL_CHARS = 3, 700   # the thread's last real messages kept for the reply draft, each clipped
PLAN_FROM, PLAN_UNTIL = (9, 0), (20, 0)     # the owner's clock
OPEN_ITEM = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
TABLES = """
CREATE TABLE IF NOT EXISTS loops(id TEXT PRIMARY KEY, kind TEXT NOT NULL, mailbox TEXT, thread_id TEXT, anchor TEXT, person TEXT,
  address TEXT, subject TEXT, what TEXT, due TEXT, opened REAL, state TEXT NOT NULL, closed REAL, why TEXT, checked REAL,
  shown INTEGER DEFAULT 0, item TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS loop_threads(mailbox TEXT NOT NULL, thread_id TEXT NOT NULL, hint TEXT, found REAL, read REAL,
  last TEXT, fails INTEGER DEFAULT 0, PRIMARY KEY(mailbox, thread_id));
CREATE TABLE IF NOT EXISTS loop_sent(mailbox TEXT NOT NULL, message_id TEXT NOT NULL, thread_id TEXT, at REAL, people TEXT,
  ack INTEGER, PRIMARY KEY(mailbox, message_id));
"""

# Who is not a person waiting on the owner: automated senders, plus the mail hosts agents write from
# (config `mail.bot_hosts` adds more).
ROBOT = re.compile(r'(^|[._-])(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|mailer-daemon|postmaster|newsletters?|news|'
                   r'digest|updates?|bounce[s]?|automated|system|service|orders?|order-update|help|support|mail|accounts?|receipts?|'
                   r'invoices?|billing|statements?|members?|rewards|offers|deals|shop|store|care|customer-?(service|care)|feedback|'
                   r'surveys?|marketing|promo(tions)?|auto-?confirm|confirm(ation)?s?|shipment-tracking|tracking|transaction|return|'
                   r'invite|info|hello|team|chatter|calendar|gemini-notes|drive-shares-dm-noreply)([._+-]|@)', re.I)
AUTO_SENDER = re.compile(r'e-?bill(ing)?|online ?biller|biller\b|bill ?pay|statements?\b', re.I)
HOST_ROBOT = re.compile(r'(^|\.)(info\d*|notifications?|notification|email|e?mails?\d*|em|news|reply|bounces?|marketing|mkt|comms|'
                        r'communications|alerts?|transactional|txn|ifly)\.', re.I)
BOT_HOSTS = tuple(str(h).lower() for h in ['agentmail.to', *(settings.get('mail.bot_hosts', []) or [])])
CAL_SUBJECT = re.compile(r'^\s*(invitation|updated invitation|accepted|declined|tentative(ly accepted)?|canceled( event)?|cancelled( event)?)\s*:', re.I)
BULK_LABELS = ('CATEGORY_PROMOTIONS', 'CATEGORY_SOCIAL', 'CATEGORY_FORUMS', 'SPAM', 'TRASH')
OWN_DOMAINS = tuple(str(d).lower() for d in settings.OWN_DOMAINS)     # the owner's own work domains: a colleague there is known
KNOWN_TAGS = ('known sender', 'replied to you', 'customer')
IMPORTANT_TAGS = ('money', 'buyer question', 'customer')
# A reply of the owner's that only acknowledges is not an answer. "Will do" is. It may end with the owner's sign-off (the owner's first
# name, the owner's initials, or a nickname from config `owner.nicknames`).


def _sign_offs() -> str:
    names = [settings.OWNER_FIRST] + list(settings.get('owner.nicknames', []) or [])
    if settings.OWNER_NAME != 'the owner':
        names.append(''.join(w[0] for w in settings.OWNER_NAME.split() if w))
    names = [n.lower() for n in names if n and n != 'the owner' and re.fullmatch(r"[A-Za-z'.-]+", n)]
    return r'(?:[-–]?\s*(?:' + '|'.join(re.escape(n) for n in names) + r'))?' if names else ''


ACK = re.compile(r"^\W*(?:(?:thanks|thank you|thx|ty|tysm|got it|great|perfect|ok|okay|sounds good|cool|awesome|appreciate it|"
                 r"much appreciated|noted|received|nice|love it|amazing|excellent|wonderful)\b[\s,!.]*(?:so much|again|a lot|a ton|man|"
                 r"buddy|sir)?[\s,!.]*)+" + _sign_offs() + r"\W*$", re.I)
_URL = re.compile(r'https?://\S+|www\.\S+', re.I)
REQUEST = re.compile(r"\b(can you|could you|would you|will you|are you able|do you (have|know|want|need)|let me know (when|what|which|whether|how|your|if you can|if you could)|"
                     r"please (confirm|advise|review|send|sign|call|reply|respond|approve|share|get back)|your thoughts|thoughts\?|any update|what do you think|"
                     r"when can|when would|when will you|are you free|are you available|does (this|that) work|is (this|that) ok)\b", re.I)
PROMISE_CUE = re.compile(r"\b(i'll|i will|i'm going to|i am going to|i can (get|send|have)|let me (get|send|check|look|find|circle)|we'll|"
                         r"we will|will (send|get|have|follow|circle|call|reach|share|loop))\b", re.I)
CONDITIONAL = re.compile(r"\b(if|whenever|once) (you|you're|you are|you'd|your|that|it|this|needed|interested|helpful|useful|anything)\b|"
                         r"\bhappy to\b|\bfeel free\b|\bif (so|not)\b", re.I)
FIRST_PERSON = re.compile(r"\b(i'll|i will|i'm going|i am going|i can|let me|we'll|we will|i'd|i would|i'm|i am|i)\b", re.I)
DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
NOT_FOR_HIS_EYES = re.compile(r'[\w.+-]+@[\w.-]+|https?://\S+|\b[0-9a-f]{12,}\b', re.I)


def on(life) -> bool:
    return (Path(life) / SWITCH).exists()


def tables(con):
    con.executescript(TABLES)


# ---- small facts -------------------------------------------------------------------------------------------------------

def _S():
    import sources as S
    return S


def _box(a: str) -> str:
    return _S().same_mailbox(a)


def _own(a: str) -> bool:
    S = _S()
    return _box(a) in {S.same_mailbox(x) for x in S.OWN_ADDRESSES}


def team(life) -> set:
    """The owner's team mailboxes (config `team.members`, through onesend.team_addresses). Never a person waiting on the owner."""
    try:
        return set(__import__('onesend').team_addresses(life) or set())
    except Exception:
        return set()


def bot(address: str, name: str = '', team_boxes=frozenset()) -> bool:
    a = str(address or '').strip().lower()
    if not a or '@' not in a:
        return True
    local, _, host = a.partition('@')
    W = __import__('writingstyle')
    return (bool(ROBOT.search(local + '@')) or bool(HOST_ROBOT.search(host)) or bool(AUTO_SENDER.search(f'{name} {host}')) or bool(W.AUTOMATED.search(a))
            or any(host == h or host.endswith('.' + h) for h in BOT_HOSTS) or _box(a) in team_boxes)


def _ts(value) -> float | None:
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _labels(m: dict) -> str:
    v = m.get('labelIds')
    return ' '.join(v) if isinstance(v, list) else str(v or '')


def _headers(m: dict) -> dict:
    return {str(h.get('name') or '').lower(): str(h.get('value') or '') for h in ((m.get('payload') or {}).get('headers') or [])
            if isinstance(h, dict)}


def words_of(m: dict) -> str:
    """What the writer of this message typed in it, quoted mail and signatures cut (writingstyle.his_words: it works
    on anybody's email, not only the owner's)."""
    W = __import__('writingstyle')
    text = str(m.get('messageText') or '')
    if not text.strip():
        pv = m.get('preview')
        text = str((pv.get('body') if isinstance(pv, dict) else pv) or '')
    import html as _h
    return W.his_words(_h.unescape(text))


def is_ack(text: str) -> bool:
    t = ' '.join(str(text or '').split())
    return not t or (len(t.split()) <= 8 and bool(ACK.match(t)))


def _echo(sentence: str, his: list) -> bool:
    """Is this sentence one of the owner's own questions repeated back (an answer that quotes each question as a heading)?
    ("Best contacts or the full people list?" as a heading is the owner's question, answered below it, not a new one.)"""
    w = set(re.findall(r'[a-z0-9]{3,}', _norm(sentence)))
    return bool(w) and any(len(w & set(re.findall(r'[a-z0-9]{3,}', h))) >= 0.7 * len(w) for h in his)


def asks(text: str, strict: bool = False, skip=()) -> str:
    """The first sentence that asks something, '' when none does. A question mark inside a link is not a question.
    strict (the owner's own messages): only a real question mark counts, since "let me know if you need anything" closes
    half the emails the owner sends and waits on nobody."""
    t = _URL.sub(' ', str(text or ''))
    for s in re.split(r'(?<=[.?!])\s+|\n+', t):
        s = s.strip()
        if len(s) >= 6 and ('?' in s or (not strict and REQUEST.search(s))) and not (skip and _echo(s, skip)):
            return ' '.join(s.split())[:200]
    return ''


def who(header_value: str) -> list:
    return [(n.strip().strip('"'), a.strip().lower()) for n, a in getaddresses([str(header_value or '')]) if '@' in a]


def first_name(name: str, address: str) -> str:
    n = re.sub(r'[^A-Za-z\'\- ]', ' ', str(name or '')).split()
    if n:
        return n[0].capitalize() if n[0].isupper() or n[0].islower() else n[0]
    return str(address or '').split('@')[0].split('.')[0].capitalize()


def _clip(text, n=200) -> str:
    return ' '.join(NOT_FOR_HIS_EYES.sub(' ', str(text or '')).split())[:n]


def _id(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:10]


# ---- reading one thread: the state machine (pure code) -----------------------------------------------------------------

def _message(mailbox: str, m: dict, team_boxes) -> dict:
    heads = _headers(m)
    sender_name, sender = parseaddr(str(m.get('sender') or heads.get('from') or ''))
    sender = sender.lower()
    labels = _labels(m)
    mine = 'SENT' in labels or _own(sender)
    subject = str(m.get('subject') or heads.get('subject') or '')
    text = words_of(m)
    noise = ('DRAFT' in labels or bool(CAL_SUBJECT.search(subject)) or 'text/calendar' in heads.get('content-type', '').lower()
             or (not mine and (bot(sender, sender_name, team_boxes) or bool(heads.get('list-unsubscribe') or heads.get('list-id'))
                               or heads.get('precedence', '').lower() in ('bulk', 'list', 'junk')
                               or heads.get('auto-submitted', 'no').lower() not in ('', 'no'))))
    return {'id': str(m.get('messageId') or ''), 'at': _ts(m.get('messageTimestamp')) or 0.0, 'mine': mine, 'noise': noise,
            'sender': sender, 'name': sender_name, 'to': str(m.get('to') or heads.get('to') or ''), 'cc': heads.get('cc', ''),
            'subject': subject, 'text': text, 'labels': labels}


def analyze(mailbox: str, msgs: list, hint: str = '', team_boxes=frozenset(), now: float | None = None) -> dict:
    """One thread -> {'owe': loop or None, 'waiting': loop or None, 'his': [the owner's substantive messages], 'why': str}.
    Pure code. `hint` is the inbox feed's reason line for an inbound row ("known sender"), '' for a sent thread."""
    S = _S()
    now = time.time() if now is None else now
    ms = sorted((_message(mailbox, m, team_boxes) for m in msgs if isinstance(m, dict)), key=lambda x: x['at'])
    out = {'owe': None, 'waiting': None, 'his': [], 'why': '', 'last_mine': None}
    real = [x for x in ms if not x['noise'] and (not x['mine'] or not is_ack(x['text']))]   # turns that count
    out['his'] = [x for x in real if x['mine']]
    out['ids'] = [x['id'] for x in real if x['id']][-60:]     # the conversation itself: no bounces, bots or notices
    # The reply draft job needs the thread, and its own tools may not open it. The thread is already in hand here (this
    # read is the one the caps and the re-read clock allow), so its last few real messages, their typed words only, go
    # with the loop.
    out['tail'] = [{'who': settings.OWNER_FIRST if x['mine'] else _clip(x['name'] or x['sender'].split('@')[0], 60), 'at': x['at'],
                    'text': _clip(x['text'], TAIL_CHARS)} for x in real[-TAIL_MSGS:]]
    if not real:
        out['why'] = 'nothing but noise'
        return out
    if any(lab in x['labels'] for x in ms for lab in BULK_LABELS) and not out['his']:
        out['why'] = 'bulk mail the owner never wrote in'
        return out
    last = real[-1]
    out['last_mine'] = last['mine']
    subject = re.sub(r'^\s*((re|fwd?|aw)\s*:\s*)+', '', last['subject'] or ms[0]['subject'], flags=re.I).strip()[:120]
    if not last['mine']:
        # their turn ended the thread: every message of theirs since the owner's last real one is unanswered
        since = [x for x in real if not x['mine'] and x['at'] > (out['his'][-1]['at'] if out['his'] else 0)]
        mine_box = S.MAILBOX_ADDRESS.get(mailbox, '')
        addressed = [x for x in since if S.addressed(mailbox, x['to'], x['cc'], x['text']) == 'to']
        if not addressed:
            out['why'] = 'the owner is only copied'
            return out
        his_qs = [_norm(s) for x in out['his'] for s in re.split(r'(?<=[.?!])\s+|\n+', x['text']) if '?' in s]
        # The newest message decides. An older question counts only behind a short nudge ("bumping this"): a long
        # newest message that asks nothing is an answer or an update, and it supersedes what came before it
        # (a vendor's full answer to the owner's two questions, after a colleague's "can you help me answer these?").
        newest = addressed[-1]
        q = asks(newest['text'], skip=his_qs)
        if not q and len(newest['text'].split()) <= NUDGE_WORDS:
            q = next((asks(x['text'], skip=his_qs) for x in reversed(addressed[:-1]) if asks(x['text'], skip=his_qs)), '')
        if not q:
            out['why'] = 'nothing asked of the owner (an update or FYI)'
            return out
        known = any(t in str(hint or '').lower() for t in KNOWN_TAGS) or last['sender'].rsplit('@', 1)[-1] in OWN_DOMAINS
        if not out['his'] and not known:
            out['why'] = 'a stranger the owner has never written to'
            return out
        out['owe'] = {'kind': 'owe_reply', 'mailbox': mailbox, 'thread_id': None, 'anchor': last['id'], 'person': last['name'] or last['sender'],
                      'address': last['sender'], 'subject': subject, 'what': q, 'due': None, 'opened': addressed[0]['at'],
                      'important': any(t in str(hint or '').lower() for t in IMPORTANT_TAGS), 'mailbox_address': mine_box,
                      'last_at': last['at']}
        return out
    # the owner's turn ended it: a question of the owner's that nobody answered
    if not any(not x['mine'] for x in real):
        out['why'] = 'they never wrote in this thread (a first email of the owner\'s, not a conversation)'
        return out
    q = asks(last['text'], strict=True)
    people = [(n, a) for n, a in who(last['to']) + who(last['cc']) if not _own(a) and not bot(a, n, team_boxes)]
    if q and people:
        earlier = [x for x in out['his'] if x['at'] > max([y['at'] for y in real if not y['mine']] or [0]) and asks(x['text'], strict=True)]
        n, a = people[0]
        out['waiting'] = {'kind': 'waiting', 'mailbox': mailbox, 'thread_id': None, 'anchor': last['id'], 'person': n or a, 'address': a,
                          'subject': subject, 'what': q, 'due': None, 'opened': (earlier[0] if earlier else last)['at'], 'important': False,
                          'last_at': last['at']}
    else:
        out['why'] = 'the owner\'s message asked nothing' if people else 'the owner\'s message went only to themself or the owner\'s agents'
    return out


# ---- promises in the owner's own words (one model pass per thread, code keeps only what checks out) ------------------------------

PROMISE_SYSTEM = """You read emails """ + settings.OWNER_NAME + """ wrote and list the promises they made in them: things they said
they would do for the person they wrote to ("I'll send the quote Friday", "Let me check with Taylor and get back to you").
Not promises: questions, wishes ("hope to"), things already done ("attached is"), things other people will do,
offers that depend on what the other person wants ("if you're building a room, I'll help you plan it"), and
pleasantries ("talk soon"). For each promise give: message (its number in the list), quote (the exact words from that
email, copied character for character, one sentence at most), what (the thing they owe, in at most 8 plain words, like
"send the quote"), due (the date it is due as YYYY-MM-DD, worked out from the email's own date, or null when they named
no day). Return an empty list when there are none."""
PROMISE_SCHEMA = {'type': 'object', 'required': ['promises'], 'properties': {'promises': {'type': 'array', 'items': {
    'type': 'object', 'required': ['message', 'quote', 'what', 'due'],
    'properties': {'message': {'type': 'integer'}, 'quote': {'type': 'string'}, 'what': {'type': 'string'},
                   'due': {'type': ['string', 'null']}}}}}}


def _norm(t: str) -> str:
    return ' '.join(re.sub(r'[‘’]', "'", re.sub(r'[“”]', '"', str(t or ''))).lower().split())


def check_promise(p: dict, his: list) -> dict | None:
    """A promise the model listed -> a kept promise, or None. Code checks: the message exists, the quote is in the owner's own
    words, it is in the first person with a promise word, `what` is short and plain, and the due date is a real date
    near the email that matches a weekday the owner named ("Friday" must be a Friday; "tomorrow" the next day)."""
    try:
        m = his[int(p.get('message'))]
    except (TypeError, ValueError, IndexError):
        return None
    quote, what = ' '.join(str(p.get('quote') or '').split()), ' '.join(str(p.get('what') or '').split())
    if len(quote) < 8 or _norm(quote) not in _norm(m['text']) or not PROMISE_CUE.search(quote) or not FIRST_PERSON.search(quote):
        return None
    if CONDITIONAL.search(quote):
        return None     # "if you're building a room, I'll help you plan it": a sales offer, nothing owed until they say yes
    if not what or len(what) > 60 or NOT_FOR_HIS_EYES.search(what):
        return None
    sent = datetime.fromtimestamp(m['at'], timezone.utc).astimezone(_pacific()).date()
    due = None
    if p.get('due'):
        try:
            due = date.fromisoformat(str(p['due'])[:10])
        except ValueError:
            return None
        if not -1 <= (due - sent).days <= 60:
            return None
        low = quote.lower()
        named = [i for i, d in enumerate(DAYS) if re.search(rf'\b{d}\b', low)]
        if named and due.weekday() not in named:
            return None
        if 'tomorrow' in low and (due - sent).days != 1:
            return None
        if re.search(r'\btoday\b|\btonight\b', low) and due != sent:
            return None
    return {'message': m, 'quote': quote, 'what': what, 'due': due.isoformat() if due else None}


def promises(his: list, subject: str, people: str, extract) -> list | None:
    """The owner's substantive messages in one thread -> kept promises, [] when none, None when the model gave no answer."""
    cands = [m for m in his if PROMISE_CUE.search(m['text'])]
    if not cands:
        return []
    shown = [{'message': i, 'sent': datetime.fromtimestamp(m['at'], timezone.utc).astimezone(_pacific()).strftime('%A %Y-%m-%d'),
              'to': people, 'subject': subject, 'his_words': m['text'][:2500]} for i, m in enumerate(cands)]
    r = extract(PROMISE_SYSTEM, json.dumps({'emails': shown}, ensure_ascii=False), PROMISE_SCHEMA)
    if r is None or not isinstance(r.get('out'), dict):
        return None
    kept = []
    for p in r['out'].get('promises') or []:
        got = check_promise(p if isinstance(p, dict) else {}, cands)
        if got and not any(_norm(k['quote']) == _norm(got['quote']) for k in kept):
            kept.append(got)
    return kept


def _pacific():
    from ownertime import PACIFIC
    return PACIFIC


def _default_extract(system, prompt, schema):
    return __import__('noticer').complete(system, prompt, schema, timeout=120)


# ---- the mail reads (one Composio call each; injected in tests and the replay) -----------------------------------------

def _composio(account: str, slug: str, args: dict):
    S = _S()
    R = S.R
    assert slug in ('GMAIL_FETCH_MESSAGE_BY_THREAD_ID', 'GMAIL_FETCH_EMAILS')   # read only, on purpose
    p = subprocess.run([R.COMPOSIO, 'execute', slug, '-d', json.dumps(args), '--account', account], capture_output=True, text=True,
                       timeout=50, env=R._env(), cwd=str(R.HOME), stdin=subprocess.DEVNULL)
    if p.returncode:
        raise R.ReadFailed(f'composio rc {p.returncode}')
    data = json.loads(p.stdout)
    if isinstance(data, dict) and data.get('storedInFile') is True:
        path = Path(str(data.get('outputFilePath') or '')).resolve()
        if not path.is_relative_to(R.COMPOSIO_TMP.resolve()) or not path.is_file():
            raise R.ReadFailed('unexpected stored output')
        try:
            data = json.loads(path.read_text())
        finally:
            path.unlink(missing_ok=True)   # never leave email text lying in /tmp
    if not isinstance(data, dict) or data.get('successful') is False:
        raise R.ReadFailed('refused')
    return data.get('data') or {}


def read_thread(mailbox: str, thread_id: str) -> list:
    """All messages of one thread, oldest first. Raises when it could not be read (never "nothing there")."""
    account = _S().R.ACCOUNTS.get(mailbox)
    if not account:
        raise ValueError('not one of the owner\'s mailboxes')
    msgs = _composio(account, 'GMAIL_FETCH_MESSAGE_BY_THREAD_ID', {'user_id': 'me', 'thread_id': thread_id}).get('messages')
    if not isinstance(msgs, list):
        raise _S().R.ReadFailed('no messages')
    return msgs


def list_sent(mailbox: str, days: int = WINDOW_DAYS, limit: int = 40) -> list:
    """The owner's sent mail in one mailbox (ids, thread, To, time, a short preview). The same in:sent list writingstyle and
    ideas use."""
    account = _S().R.ACCOUNTS.get(mailbox)
    if not account:
        raise ValueError('not one of the owner\'s mailboxes')
    msgs = _composio(account, 'GMAIL_FETCH_EMAILS', {'user_id': 'me', 'query': f'in:sent newer_than:{int(days)}d', 'max_results': limit,
                                                      'verbose': False}).get('messages')
    if not isinstance(msgs, list):
        raise _S().R.ReadFailed('no message list')
    return msgs


# ---- what to read ------------------------------------------------------------------------------------------------------

def _inbox_db(inbox_db=None):
    """The inbox feed file (see the module notes), or None when none is set up or it is not there."""
    path = inbox_db or settings.get('mail.inbox_db') or getattr(_S().R, 'INBOX_DB', None)
    if not path:
        return None
    path = Path(str(path)).expanduser()
    return path if path.is_file() else None


def inbound_rows(inbox_db=None, now: float | None = None, team_boxes=frozenset()) -> list:
    """Mail the inbox feed listed in the window that could hold a person waiting on the owner. Code only, no reads:
    its verdicts already sort out lists, promotions, calendar mail, agents and the owner's own mail. [] without a feed."""
    from urllib.parse import quote
    S = _S()
    now = time.time() if now is None else now
    db = _inbox_db(inbox_db)
    if db is None:
        return []
    import sqlite3
    with closing(sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=5)) as con:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        rows = con.execute('SELECT mailbox,message_id,thread_id,at,sender,subject,verdict,reason FROM seen WHERE at>=? ORDER BY at',
                           (now - WINDOW_DAYS * 86400,)).fetchall()
    out = []
    for r in rows:
        if r['mailbox'] not in S.R.ACCOUNTS or not r['thread_id']:
            continue
        name, address = parseaddr(str(r['sender'] or ''))
        if _own(address) or bot(address, name, team_boxes):
            continue
        reason = str(r['reason'] or '')
        reply = bool(re.match(r'\s*(re|aw)\s*:', str(r['subject'] or ''), re.I))
        colleague = address.rsplit('@', 1)[-1] in OWN_DOMAINS
        if r['verdict'] == 'surface' and reason != 'automated money notice' and (
                reply or colleague or any(t in reason.lower() for t in KNOWN_TAGS)):
            out.append(dict(r))      # someone the owner writes to, or a reply in a thread: the thread read decides
        elif r['verdict'] == 'filtered' and reason.startswith('unknown sender') and (reply or colleague):
            out.append(dict(r))      # a reply in a thread, or a colleague: the thread read decides
    return out


OWNED = 'owned by another agent'


def owned_threads(inbox_db=None) -> set:
    """Threads the inbox feed marked as owned by someone else (verdict 'owned', for example another agent's outreach
    sent from one of the owner's mailboxes). Without this, such a thread reads as a promise and a question of the owner's. set()
    without a feed."""
    from urllib.parse import quote
    import sqlite3
    db = _inbox_db(inbox_db)
    if db is None:
        return set()
    with closing(sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=5)) as con:
        rows = con.execute("SELECT mailbox, thread_id FROM seen WHERE verdict='owned' AND thread_id IS NOT NULL").fetchall()
    boxes = set(_S().R.ACCOUNTS)          # only the owner's own mailboxes: never keep another mailbox's thread ids
    return {(b, t) for b, t in rows if b in boxes}


def queue(con, life, now: float, inbox_db=None, team_boxes=frozenset()) -> int:
    n = 0
    for box, tid in owned_threads(inbox_db):
        con.execute('INSERT INTO loop_threads(mailbox,thread_id,hint,found,read) VALUES(?,?,?,?,?) ON CONFLICT(mailbox,thread_id) DO UPDATE SET hint=?, read=?',
                    (box, tid, OWNED, now, now, OWNED, now))
        for r in con.execute("SELECT id FROM loops WHERE mailbox=? AND thread_id=? AND state='open'", (box, tid)).fetchall():
            _close(con, r['id'], 'another agent handles this lead', now)
    for r in inbound_rows(inbox_db, now, team_boxes):
        cur = con.execute('INSERT OR IGNORE INTO loop_threads(mailbox,thread_id,hint,found) VALUES(?,?,?,?)',
                          (r['mailbox'], r['thread_id'], str(r['reason'] or ''), float(r['at'])))
        if not cur.rowcount:      # a known thread with new mail in it: read it again
            con.execute('UPDATE loop_threads SET read=NULL, fails=0, found=MAX(found,?), hint=COALESCE(NULLIF(hint,\'\'),?) WHERE mailbox=? AND thread_id=? AND read IS NOT NULL AND read<? AND hint IS NOT ?',
                        (float(r['at']), str(r['reason'] or ''), r['mailbox'], r['thread_id'], float(r['at']), OWNED))
        n += cur.rowcount
    con.commit()
    return n


def _template_key(subject) -> str:
    s = re.sub(r'^\s*((re|fwd?|aw)\s*:\s*)+', '', str(subject or ''), flags=re.I).lower()
    return ' '.join(re.findall(r"[a-z0-9']+", s)[:3])


def note_sent(con, mailbox: str, msgs: list, team_boxes, now: float) -> int:
    """The owner's sent list -> loop_sent rows, and every thread with a real person in it queued for reading.
    Outreach sequences are not queued: a sent folder can be mostly first-touch outreach ("Quick question for
    <company>"), each ending in a question, which would read as a hundred people "owing the owner an answer", and they are
    written one by one, so no text rule tells them apart. So only the owner's REPLIES are queued from the sent list (subject "Re:" / "Fwd:"):
    a waiting loop needs the other person to have written in the thread, and the owner's promises live in the owner's replies. A
    reply whose subject starts the same way as two or more others in the list is a sequence follow-up, not queued.
    When the other person answers a first email, that answer comes in through the inbox feed and is read from there.
    Missed on purpose: a brand-new email of the owner's asking something, or promising something, in its first message."""
    n = 0
    seen_keys = {}
    for m in msgs:
        k = _template_key(m.get('subject'))
        seen_keys[k] = seen_keys.get(k, 0) + 1
    for m in msgs:
        mid, tid = str(m.get('messageId') or ''), str(m.get('threadId') or '')
        if not mid:
            continue
        people = [a for _, a in who(m.get('to')) if not _own(a) and not bot(a, '', team_boxes)]
        at = _ts(m.get('messageTimestamp')) or now
        pv = m.get('preview')
        ack = is_ack(words_of({'messageText': (pv.get('body') if isinstance(pv, dict) else pv) or ''}))
        reply = bool(re.match(r'\s*(re|fwd?|aw)\s*:', str(m.get('subject') or ''), re.I))
        sequence = seen_keys.get(_template_key(m.get('subject')), 0) >= 3
        con.execute('INSERT OR IGNORE INTO loop_sent VALUES(?,?,?,?,?,?)', (mailbox, mid, tid, at, json.dumps([] if sequence else people), int(ack)))
        if sequence or not reply:
            continue
        if people and tid and at >= now - WINDOW_DAYS * 86400:
            cur = con.execute('INSERT OR IGNORE INTO loop_threads(mailbox,thread_id,hint,found) VALUES(?,?,?,?)', (mailbox, tid, '', at))
            if not cur.rowcount:
                con.execute('UPDATE loop_threads SET read=NULL, found=MAX(found,?) WHERE mailbox=? AND thread_id=? AND read IS NOT NULL AND read<? AND hint IS NOT ?',
                            (at, mailbox, tid, at, OWNED))
            n += cur.rowcount
    con.commit()
    return n


# ---- the ledger ----------------------------------------------------------------------------------------------------------

def _close(con, loop_id: str, why: str, now: float):
    con.execute("UPDATE loops SET state='closed', closed=?, why=? WHERE id=? AND state='open'", (now, why[:200], loop_id))


def _keep(con, lp: dict, now: float):
    row = con.execute('SELECT * FROM loops WHERE id=?', (lp['id'],)).fetchone()
    if row is None:
        con.execute("INSERT INTO loops(id,kind,mailbox,thread_id,anchor,person,address,subject,what,due,opened,state,checked,shown,payload) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open', ?, 0, ?)",
                    (lp['id'], lp['kind'], lp['mailbox'], lp['thread_id'], lp['anchor'], lp['person'], lp['address'], lp['subject'],
                     lp['what'], lp.get('due'), lp['opened'], now, json.dumps({'important': lp.get('important'), 'quote': lp.get('quote'),
                                                                               'last_at': lp.get('last_at'), 'mids': lp.get('mids'),
                                                                               'tail': lp.get('tail')})))
        return 'new'
    if row['state'] == 'open':
        p = json.loads(row['payload'] or '{}') | {k: lp[k] for k in ('last_at', 'mids', 'tail') if lp.get(k)}
        con.execute('UPDATE loops SET anchor=?, what=?, checked=?, payload=? WHERE id=?', (lp['anchor'], lp['what'], now, json.dumps(p), lp['id']))
    return 'same'


def apply_thread(con, mailbox: str, thread_id: str, msgs: list, hint: str, team_boxes, now: float, extract=None, extract_left=None,
                 life=None) -> dict:
    """Read result of one thread -> loops opened, kept or closed. Promises are pulled only when extract_left allows.
    A loop the owner already settled (`settled`) is not opened, and an open one is closed with the owner's reason."""
    got = analyze(mailbox, msgs, hint, team_boxes, now)
    rep = {'new': 0, 'closed': 0}
    live, gone = set(), {}
    mids = got.get('ids') or []
    for lp in (got['owe'], got['waiting']):
        if lp and now - lp['opened'] <= MAX_AGE_DAYS * 86400:
            lp |= {'thread_id': thread_id, 'id': f"{lp['kind']}:{mailbox}:{thread_id}", 'mids': mids, 'tail': got.get('tail')}
            why = settled(con, life, now, [lp]).get(lp['id'])
            if why:
                gone[lp['id']] = why
                rep['settled'] = rep.get('settled', 0) + 1
                continue
            rep['new'] += _keep(con, lp, now) == 'new'
            live.add(lp['id'])
    for r in con.execute("SELECT id,kind FROM loops WHERE kind!='promise' AND mailbox=? AND thread_id=? AND state='open'", (mailbox, thread_id)).fetchall():
        if r['id'] in live:
            continue
        if r['id'] in gone:
            why = gone[r['id']]
        elif r['kind'] == 'owe_reply' and got['last_mine']:
            why = 'the owner answered in the thread'
        elif r['kind'] == 'waiting' and got['last_mine'] is False:
            why = 'they answered the owner'
        else:
            why = got['why'] or 'the thread moved on'
        _close(con, r['id'], why, now)
        rep['closed'] += 1
    # promises: every later real message of the owner's in the thread settles an earlier promise in it
    his = got['his']
    for r in con.execute("SELECT id,opened FROM loops WHERE kind='promise' AND mailbox=? AND thread_id=? AND state='open'", (mailbox, thread_id)).fetchall():
        if any(m['at'] > r['opened'] + 60 for m in his):
            _close(con, r['id'], 'the owner wrote to them again in the thread', now)
            rep['closed'] += 1
    seen = set(json.loads((con.execute('SELECT last FROM loop_threads WHERE mailbox=? AND thread_id=?', (mailbox, thread_id)).fetchone() or ['[]'])[0] or '[]'))
    fresh = [m for m in his if m['id'] not in seen and m['at'] >= now - WINDOW_DAYS * 86400]
    if fresh and extract is not None and (extract_left is None or extract_left[0] > 0):
        people = [(n, a) for m in fresh for n, a in who(m['to']) + who(m['cc']) if not _own(a) and not bot(a, n, team_boxes)]
        if people:
            if extract_left is not None:
                extract_left[0] -= 1
            subject = re.sub(r'^\s*((re|fwd?)\s*:\s*)+', '', fresh[-1]['subject'], flags=re.I).strip()[:120]
            kept = promises(fresh, subject, ', '.join(n or a for n, a in people[:3]), extract)
            if kept is None:
                rep['extract_failed'] = True
                fresh = []          # not marked as read: tried again next time
            else:
                for k in kept:
                    m = k['message']
                    if any(x['at'] > m['at'] + 60 for x in his):
                        continue    # the owner already wrote again after it
                    own = [(n, a) for n, a in who(m['to']) + who(m['cc']) if not _own(a) and not bot(a, n, team_boxes)]
                    n, a = (own or people)[0]
                    lp = {'id': f"promise:{mailbox}:{thread_id}:{_id(_norm(k['quote']))}", 'kind': 'promise', 'mailbox': mailbox,
                          'thread_id': thread_id, 'anchor': m['id'], 'person': n or a, 'address': a, 'subject': subject, 'what': k['what'],
                          'due': k['due'], 'opened': m['at'], 'quote': k['quote'], 'last_at': m['at'], 'mids': mids}
                    if settled(con, life, now, [lp]).get(lp['id']):
                        rep['settled'] = rep.get('settled', 0) + 1
                        continue
                    rep['new'] += _keep(con, lp, now) == 'new'
        seen |= {m['id'] for m in fresh}     # read (a note only to themself or the owner's agents owes nobody anything)
    con.execute('UPDATE loop_threads SET read=?, last=? WHERE mailbox=? AND thread_id=?', (now, json.dumps(sorted(seen)), mailbox, thread_id))
    con.execute('UPDATE loops SET checked=? WHERE mailbox=? AND thread_id=? AND state=\'open\'', (now, mailbox, thread_id))
    con.commit()
    return rep


def close_by_code(con, life, now: float) -> int:
    """Closes that need no read: the owner emailed that person anywhere since they wrote, the item made for it was closed in
    chat, the owner already settled it (`settled`: an item the owner dropped, or the owner's own chat words), or the loop is too old to chase."""
    n = 0
    rows = con.execute("SELECT * FROM loops WHERE state='open'").fetchall()
    gone = settled(con, life, now, [_as_loop(r) for r in rows]) if rows else {}
    for r in rows:
        why = ''
        if now - (r['opened'] or now) > MAX_AGE_DAYS * 86400:
            why = 'too old to chase'
        elif r['kind'] == 'owe_reply':
            for s in con.execute('SELECT people, ack FROM loop_sent WHERE at>? AND ack=0', (r['opened'] or 0,)).fetchall():
                if _box(r['address']) in {_box(a) for a in json.loads(s['people'] or '[]')}:
                    why = 'the owner wrote to them since'
                    break
        if not why and r['item']:
            it = con.execute('SELECT state FROM opps WHERE key=?', (r['item'],)).fetchone()
            if it and it['state'] in ('done', 'moot', 'declined'):
                why = {'done': 'handled', 'moot': 'the owner answered the thread', 'declined': 'the owner passed on it'}[it['state']]
        if not why:
            why = gone.get(r['id'], '')
        if why:
            _close(con, r['id'], why, now)
            n += 1
    con.commit()
    return n


# ---- things the owner already settled -------------------------------------------------------------------------------------------

_DONE_VERB = (r"spoke|spoken|talked|chatted|called|phoned|rang|texted|messaged|met|caught up|replied|responded|answered|emailed|"
              r"wrote|written|handled|dealt|sorted|took care|taken care|got back|gotten back|followed up|sent|covered|settled")
# The owner says it is done, outside this email list: "I already spoke to Jordan", "I called them", "we talked yesterday".
HANDLED = re.compile(r"\b(?:i|we)(?:'ve| have|'d| had)?\s+(?:already\s+|just\s+|actually\s+)?(?:" + _DONE_VERB + r")\b"
                     r"|\balready\s+(?:" + _DONE_VERB + r"|did|done)\b"
                     r"|\b(?:handled|taken care of|dealt with|sorted out|all set with)\b", re.I)
# The owner says to drop it: "disregard that", "ignore Jordan", "no need", "don't remind me about Jordan".
DISMISS = re.compile(r"\b(?:disregard|ignore)\b|\bdrop (?:it|that|this|him|her|them|the)\b|\bforget (?:it|that|this|about)\b|\bno need\b"
                     r"|\bnot needed\b|\b(?:stop|quit) (?:raising|reminding|asking|bringing)\b|\bdon'?t (?:raise|remind|bring|need)\b"
                     r"|\bskip (?:it|that|this|him|her|them)\b", re.I)
# Not handled after all: "I haven't spoken to them", "I'll call Jordan", "need to reply to Jordan", "should I call them?".
NOT_DONE = re.compile(r"\b(?:not|never|don'?t|do not|haven'?t|hasn'?t|didn'?t|won'?t|can'?t|couldn'?t|need to|needs to|have to|has to|got to|gotta|"
                      r"going to|gonna|will|i'?ll|we'?ll|should|must|can you|could you|please|yet to|want to|meant to|supposed to|remind me)\s+(?:\w+\s+){0,2}$", re.I)
NOT_NAMES = frozenset(DAYS) | frozenset(('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october',
                                        'november', 'december', settings.AGENT_NAME.lower(), settings.OWNER_FIRST.lower(), 'reminder'))
PRONOUN = re.compile(r"\b(?:him|her|them|that|it|this|that one|this one)\b", re.I)
_CLOSE_WORDS = {'declined': 'the owner dropped it', 'moot': 'the owner settled it', 'done': 'the owner finished it in chat', 'skipped': 'the owner skipped it'}


def says_settled(text: str) -> bool:
    """Code reads the owner's words alone: does a sentence of the owner's (not a question) say it is handled or to drop it?"""
    for s in re.split(r'(?<=[.!?])\s+|\n+', str(text or '').replace('’', "'")):
        s = s.strip()
        if not s or s.endswith('?'):
            continue
        for rx in (HANDLED, DISMISS):
            for m in rx.finditer(s):
                if not NOT_DONE.search(s[:m.start()]):
                    return True
    return False


def _names(lp: dict) -> tuple:
    """(full name, first name) as they would appear in the owner's words, lowercased. Full name only when it has 2+ words."""
    full = ' '.join(re.sub(r'[^A-Za-z\'\- ]', ' ', str(lp.get('person') or '')).split()).lower()
    first = first_name(lp.get('person') or '', lp.get('address') or '').lower()
    return (full if len(full.split()) >= 2 else '', first if len(first) >= 3 else '')


def named(text: str, lps: list) -> list:
    """The loops whose person the owner's words name: the full name always counts; a first name counts only when just one
    person in these loops has it (two Alexes and a bare "Alex" is not told apart, so neither is closed)."""
    t = ' ' + re.sub(r"'s\b|[^a-z\- ]", ' ', str(text or '').lower().replace('’', "'")) + ' '
    firsts = {}
    for lp in lps:
        firsts.setdefault(_names(lp)[1], set()).add(_box(lp.get('address') or ''))
    out = []
    for lp in lps:
        full, first = _names(lp)
        if (full and f' {full} ' in t) or (first and f' {first} ' in t and len(firsts.get(first, ())) == 1):
            out.append(lp)
    return out


def only_names(text: str, lp: dict) -> bool:
    """Does this Zeus message name this person and nobody else? Any other capitalised word inside a sentence counts as
    maybe someone else ("Jordan and Alex are both waiting"), so it is not; an unclear "them" closes nothing."""
    full, first = _names(lp)
    if not first or lp not in named(text, [lp]):
        return False
    mine = set(full.split()) | {first}
    for sent in re.split(r'(?<=[.!?:;])\s+|\n+', str(text or '')):
        for w in re.findall(r"\b[A-Z][a-z]+\b", ' '.join(sent.split()[1:])):
            if w.lower() not in mine and w.lower() not in NOT_NAMES:
                return False
    return True


def _as_loop(r) -> dict:
    d = dict(r)
    p = json.loads(d.get('payload') or '{}')
    return d | {'last_at': p.get('last_at') or d.get('opened') or 0, 'mids': p.get('mids') or []}


def _dropped_items(con, life) -> list:
    """Every life item the owner settled themself: [(when, state, the owner's closing words, refs, threads)]. The loops' own line items
    are not (a line is code's own reminder; its loop closes by its own rules)."""
    import fresh as FR
    import prefs as F
    out = []
    for r in con.execute("SELECT key,state,payload,sources,updated FROM opps WHERE key NOT LIKE 'loops:%'").fetchall():
        try:
            p = json.loads(r['payload'] or '{}')
        except ValueError:
            continue
        muted = bool(life) and F.is_muted(life, r['key'])
        if not (FR.closed_by_him(r['state'], p) or muted):
            continue
        c = p.get('closed') or {}
        try:
            refs = {str(x) for x in json.loads(r['sources'] or '[]')}
        except ValueError:
            refs = set()
        # An item about one message (a bounce, a notice) settles only that message; an item about the whole thread
        # (a reply item) settles the thread: declining a bounce notice in a thread ("ignore for now") says nothing
        # about the question someone asked in it.
        th = p.get('thread') or {}
        threads = {(str(th.get('mailbox') or ''), str(th['thread_id']))} if th.get('thread_id') else set()
        for v in (p.get('latest_email') or {}, (p.get('fresh') or {}).get('seen') or {}):
            for k in ('message_id', 'anchor'):
                if v.get(k) and v.get('mailbox'):
                    refs.add(f"email:{v['mailbox']}:{v[k]}")
        out.append((float(c.get('at') or r['updated'] or 0), 'muted' if muted and r['state'] not in _CLOSE_WORDS else r['state'],
                    str(c.get('why') or ''), refs, threads))
    return out


def _chat(life, since: float):
    """The owner's own messages to Zeus and Zeus's replies since a time (chats.py, read-only). [] when there is no store to read:
    a sandbox never falls back to the owner's real one (onesend's rule)."""
    import chats as C
    store = os.environ.get('LIFE_CHAT_DB') or None
    try:
        if store is None and (life is None or __import__('onesend')._sandbox(life)):
            return []
        return C.messages(float(since), path=store)
    except Exception:
        return []


def settled(con, life, now: float, lps: list) -> dict:
    """-> {loop id: why} for the loops the owner already settled AFTER their newest message in it. Code only:
      1. an item about one of the real messages of this conversation (not a bounce or a notice in it), or about the
         whole thread, that the owner dropped, muted, finished in chat or answered themself;
      2. a dropped item whose closing words, the owner's, say the owner handled it with that person or to drop them;
      3. the owner's chat words to Zeus saying so, naming the person, or "I called them" / "disregard that" right after a Zeus
         message that named exactly one of these people.
    A newer message from them, after the owner settled it, is new: the time check is what keeps a fresh question alive."""
    lps = [lp for lp in lps if lp and lp.get('id')]
    if not lps:
        return {}
    out = {}
    ids = {lp['id'] for lp in lps}
    # a first name is told apart against every open loop, not only the ones asked about here
    crowd = lps + [_as_loop(r) for r in con.execute("SELECT * FROM loops WHERE state='open'").fetchall() if r['id'] not in ids]
    try:
        items = _dropped_items(con, life)
    except Exception:
        items = []
    for lp in lps:
        after = float(lp.get('last_at') or lp.get('opened') or 0)
        box, tid = str(lp.get('mailbox') or ''), str(lp.get('thread_id') or '')
        mine = {f'email:{box}:{m}' for m in set(lp.get('mids') or []) | {lp.get('anchor')} if m}
        for at, state, words, refs, threads in items:
            if at < after:
                continue
            word = _CLOSE_WORDS.get(state, 'the owner dropped it')
            if (tid and any(t == tid and (not b or b == box) for b, t in threads)) or (refs & mine):
                out[lp['id']] = (f'{word} earlier' + (f': "{words}"' if words else ''))[:200]
                break
            if words and says_settled(words) and lp in named(words, crowd):
                out[lp['id']] = f'{word} earlier: "{words}"'[:200]
                break
    left = [lp for lp in lps if lp['id'] not in out]
    if not left:
        return out
    since = max(min(float(lp.get('last_at') or lp.get('opened') or now) for lp in left), now - MAX_AGE_DAYS * 86400)
    msgs = _chat(life, since)
    for i, m in enumerate(msgs):
        if m['who'] != 'owner' or not says_settled(m['text']):
            continue
        pool = [lp for lp in left if lp['id'] not in out and float(m['at']) >= float(lp.get('last_at') or lp.get('opened') or 0)]
        hit = [lp for lp in named(m['text'], crowd) if lp in pool]
        if not hit and PRONOUN.search(m['text']) and not named(m['text'], crowd):
            # "I called them" / "disregard that": who the owner means is who Zeus had just named, if that was exactly one person
            prev = next((x for x in reversed(msgs[:i]) if x['who'] == 'zeus'), None)
            if prev and float(m['at']) - float(prev['at']) <= 20 * 60:
                said = named(prev['text'], crowd)
                if len({_box(lp.get('address') or '') for lp in said}) == 1:
                    hit = [lp for lp in said if lp in pool and only_names(prev['text'], lp)]
        for lp in hit:
            out[lp['id']] = f'the owner told Zeus: "{_clip(m["text"], 160)}"'
    return out


# ---- the lines ------------------------------------------------------------------------------------------------------------

def his_clock(life, now_dt: datetime) -> datetime:
    import ownertime as OT
    return now_dt.astimezone(OT.zone(at=now_dt.timestamp(), life=life))


def days_waiting(opened: float, today: date, z) -> int:
    return max(1, (today - datetime.fromtimestamp(opened, z).date()).days)


def due_words(due: str, today: date) -> str:
    d = date.fromisoformat(due)
    gap = (d - today).days
    if gap < 0:
        return f"was due {d.strftime('%a')} {d.strftime('%b')} {d.day}"
    if gap == 0:
        return 'due today'
    if gap == 1:
        return 'due tomorrow'
    return f"due {d.strftime('%a')}" if gap < 7 else f"due {d.strftime('%b')} {d.day}"


def candidates(con, now: float, today: date) -> list:
    """Open loops worth a line now, ranked: promises due today or late, then people waiting on the owner (money, buyers,
    customers first), then other promises, then what others owe the owner. Oldest first inside each."""
    out = []
    for r in con.execute("SELECT * FROM loops WHERE state='open' AND shown<?", (MAX_SHOWN,)).fetchall():
        age_h = (now - (r['opened'] or now)) / 3600
        imp = bool((json.loads(r['payload'] or '{}')).get('important'))
        if r['kind'] == 'promise':
            due = date.fromisoformat(r['due']) if r['due'] else None
            if due is not None and (due - today).days <= 0:
                tier = 0
            elif due is not None and (due - today).days == 1 or (due is None and age_h >= PROMISE_SHOW_H):
                tier = 3
            else:
                continue
        elif r['kind'] == 'owe_reply':
            if age_h < OWE_SHOW_H:
                continue
            tier = 1 if imp else 2
        else:
            if age_h < WAIT_SHOW_H:
                continue
            tier = 4
        out.append((tier, r['opened'] or now, dict(r)))
    out.sort(key=lambda x: (x[0], x[1]))
    return [r for _, _, r in out]


def line_fact(r: dict, today: date, z) -> dict:
    """One line as code states it. The writer words it; nothing here is a sentence for the owner."""
    n = days_waiting(r['opened'], today, z)
    who_ = first_name(r['person'], r['address'])
    base = {'kind': {'owe_reply': 'the owner owes them a reply', 'waiting': 'they owe the owner an answer', 'promise': 'the owner promised this'}[r['kind']],
            'person': who_, 'full_name': _clip(r['person'], 60), 'about': _clip(r['subject'], 90), 'day': n}
    if r['kind'] == 'owe_reply':
        base['short'] = f'{who_}, day {n}'
        base['their_question'] = _clip(r['what'])
    elif r['kind'] == 'waiting':
        base['short'] = f'{who_} owes you an answer, day {n}'
        base['his_question'] = _clip(r['what'])
    else:
        thing = _clip(r['what'], 60)
        base['thing'] = thing
        base['short'] = thing + ('' if who_.lower() in thing.lower() else f' for {who_}') + ', ' + (due_words(r['due'], today) if r['due'] else f'day {n}')
        base['his_words'] = _clip((json.loads(r['payload'] or '{}')).get('quote') or '', 240)
        if r['due']:
            base['due'] = r['due']
    return base


def open_item_for_thread(con, thread_id: str):
    for r in con.execute(f"SELECT key,payload FROM opps WHERE state IN ({','.join('?' * len(OPEN_ITEM))})", OPEN_ITEM).fetchall():
        p = json.loads(r['payload'])
        if (p.get('thread') or {}).get('thread_id') == thread_id or (p.get('latest_email') or {}).get('thread_id') == thread_id:
            return r['key']
    return None


def thread_words(con, r: dict) -> str:
    """The thread's last few messages as the loops code last read them, for the draft job,
    so a short honest draft never depends on the job's own tools. It goes in the item's evidence, never in a step: the
    steps are read by policy.guard_class, and their question ("can you ...?") must not turn the draft into an outside ask.
    The loop row is read again here because plan_lines may have just re-read the thread. '' when no read kept any."""
    try:
        row = con.execute('SELECT payload FROM loops WHERE id=?', (r['id'],)).fetchone()
        tail = (json.loads((row[0] if row else None) or r.get('payload') or '{}') or {}).get('tail') or []
    except (TypeError, ValueError, KeyError):
        return ''
    said = []
    for x in tail:
        if isinstance(x, dict) and x.get('text') and x.get('at'):
            t = time.localtime(float(x['at']))
            said.append(f"{x.get('who') or 'them'}, {time.strftime('%b', t)} {t.tm_mday}: \"{x['text']}\"")
    if not said:
        return ''
    return (' The thread so far, oldest first, as read from the owner\'s mail: ' + ' | '.join(said) + ' Draft from these even if you '
            'cannot open the thread yourself: a short honest answer to their question is better than no draft. Never '
            'promise anything these messages and the owner\'s own words do not support.')


def reply_item(con, life, r: dict, line: dict, surface_at: datetime, now_dt: datetime, promise: bool = False) -> str | None:
    """An ordinary life item: the background job drafts the reply in the owner's voice, one-send freezes it, the owner's yes sends it.
    Code fills in which mailbox, which thread and which message it answers; the job never chooses them."""
    import policy as P
    key = f"loopreply:{r['mailbox']}:{r['thread_id']}"
    if con.execute('SELECT 1 FROM opps WHERE key=?', (key,)).fetchone():
        return None
    who_ = line['person']
    ask_ = line.get('their_question') or ''
    steps = [{'do': f'Draft the reply {settings.OWNER_FIRST} would send {who_} in this email thread, answering: "{ask_}". Look up anything the answer '
                    'needs in the owner\'s mail and CRM first; if the answer needs a fact you cannot find, say what is missing instead of guessing.',
              'action_class': 'prepare_or_research'},
             {'do': f'Send that reply to {who_} in the same email thread, only after the owner\'s yes.', 'action_class': 'message_outside_person'}]
    if promise:          # what the owner promised someone, prepared for the owner's yes; one item per thread, however many promises it holds
        thing = str(line.get('thing') or line.get('short') or '')
        steps[0]['do'] = (f'Prepare what {settings.OWNER_FIRST} promised {who_} in this email thread ("{thing}"): draft the message the owner would send, with what they said they '
                          'would include. Read the whole thread for everything the owner promised there. Look up anything it needs in the owner\'s mail and CRM first; '
                          'if it needs something only the owner has, say what is missing instead of guessing.')
        ask_ = str(line.get('his_words') or thing)
    opp = {'title': (f"What you promised {who_}: {line['about']}" if promise else f"Reply to {who_}: {line['about']}")[:120],
           'why': f'The owner promised {who_} this {line["day"]} day(s) ago.' if promise else f'{who_} has been waiting {line["day"]} day(s) for the owner\'s answer.',
           'proposed_action': f'Draft the owner\'s reply to {who_} in the owner\'s voice and hold it for the owner\'s yes.', 'action_class': 'message_outside_person',
           'urgency': 'today', 'surface_at': surface_at.isoformat(), 'evidence': ask_ + thread_words(con, r), 'dedupe_key': key, 'confidence': 0.9,
           'owner_involved': 'yes', 'steps': steps}
    decision = P.decide(opp, None, now_dt)
    decision['surface_at'] = surface_at.isoformat()
    now = time.time()
    payload = {**opp, 'decision': decision, '_source': 'loops', 'loop': r['id'],
               'thread': {'mailbox': r['mailbox'], 'thread_id': r['thread_id']},
               'latest_email': {'mailbox': r['mailbox'], 'message_id': r['anchor'], 'thread_id': r['thread_id'],
                                'when': datetime.fromtimestamp(r['opened'], timezone.utc).isoformat()}}
    con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key, 'pending', decision['autonomy'], surface_at.isoformat(), 'today', 'message_outside_person',
                 json.dumps(payload, default=str), json.dumps([f"email:{r['mailbox']}:{r['anchor']}"]), now, now))
    con.execute('UPDATE loops SET item=? WHERE id=?', (key, r['id']))
    return key


def _sentence(i: int, x: dict, today: date) -> str:
    who_ = x['person'] + (f" ({x['full_name']})" if x['full_name'] and x['full_name'] != x['person'] else '')
    about = f' about "{x["about"]}"' if x['about'] else ''
    if x['kind'] == 'the owner owes them a reply':
        s = f'{i}. {who_} wrote to the owner{about} and has waited {x["day"]} day(s) for the owner\'s reply. They asked: "{x.get("their_question", "")}".'
    elif x['kind'] == 'they owe the owner an answer':
        s = f'{i}. The owner asked {who_}{about} and has waited {x["day"]} day(s) for an answer. The owner asked: "{x.get("his_question", "")}".'
    else:
        when = f'it is {due_words(x["due"], today)}' if x.get('due') else f'the owner said it {x["day"]} day(s) ago and named no day'
        s = f'{i}. The owner promised {who_}{about}: {x.get("thing", "")}; {when}. The owner\'s words: "{x.get("his_words", "")}".'
    if x.get('draft_coming'):
        s += ' A reply in the owner\'s voice is being drafted and comes with this note for the owner\'s yes; nothing goes without it.'
    elif x.get('already_on_his_list'):
        s += ' This one is already on the owner\'s list as its own item.'
    return s


def plan_lines(con, life, now_dt: datetime, read=None, feed=None, reads_left=None, team_boxes=frozenset(), report=None) -> list:
    """9am to 8pm the owner's clock: each loop worth a line now becomes its own item (or, for a person waiting on the owner, a
    "reply to X" item with the draft). At most MAX_LINES a day, a loop at most once a day. -> the item keys made."""
    import prefs as F
    his = his_clock(life, now_dt)
    if not (PLAN_FROM <= (his.hour, his.minute) < PLAN_UNTIL):
        return []
    now, today, z = now_dt.timestamp(), his.date(), his.tzinfo
    day = today.isoformat()
    if F.is_muted(life, f'loops:{day}'):
        return []

    def last_day(r):
        try:
            return (json.loads(r['payload'] or '{}') or {}).get('last_day')
        except (TypeError, ValueError):
            return None
    used = sum(1 for r in con.execute('SELECT payload FROM loops').fetchall() if last_day(r) == day)
    day0 = his.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    drafts = con.execute("SELECT COUNT(*) FROM opps WHERE key LIKE 'loopreply:%' AND first_seen>=?", (day0,)).fetchone()[0]
    made = []
    for r in candidates(con, now, today):
        if used >= MAX_LINES:
            break
        if last_day(r) == day or F.is_muted(life, f'loops:{day}:{r["id"]}'):
            continue
        if r['kind'] != 'promise' and now - (r['checked'] or 0) > FRESH_FOR_NOTE_H * 3600:
            if read is None or (reads_left is not None and reads_left[0] <= 0):
                continue     # cannot be re-read now: not shown stale
            try:
                if reads_left is not None:
                    reads_left[0] -= 1
                hint = (con.execute('SELECT hint FROM loop_threads WHERE mailbox=? AND thread_id=?', (r['mailbox'], r['thread_id'])).fetchone() or [''])[0]
                apply_thread(con, r['mailbox'], r['thread_id'], read(r['mailbox'], r['thread_id']), hint, team_boxes, now, life=life)
            except Exception as e:
                if report is not None:
                    report.setdefault('errors', []).append(f're-read before the line: {type(e).__name__}')
                continue
            if (con.execute('SELECT state FROM loops WHERE id=?', (r['id'],)).fetchone() or ['closed'])[0] != 'open':
                continue     # the owner answered: it vanishes before the owner sees it
        ln = line_fact(r, today, z)
        key = None
        if r['kind'] == 'owe_reply':
            held = r['item'] or open_item_for_thread(con, r['thread_id'])
            if held:
                key = held                                    # already on the owner's list as its own item: nothing new
            elif drafts < MAX_DRAFTS:
                key = reply_item(con, life, r, ln, now_dt, now_dt)
                drafts += bool(key)
        if key is None and r['kind'] == 'promise' and drafts < MAX_DRAFTS and not (r['item'] or open_item_for_thread(con, r['thread_id'])):
            key = reply_item(con, life, r, ln, now_dt, now_dt, promise=True)
            drafts += bool(key)
        # A bare reminder is not a text: "You still owe Alex ..." with nothing prepared is noise. A loop reaches the owner
        # only as an item with the reply or the promised thing drafted for the owner's yes (above). Otherwise it stays on the owner's
        # list, is looked at once today, and comes up in chat when the owner asks what is open.
        lp = json.loads((con.execute('SELECT payload FROM loops WHERE id=?', (r['id'],)).fetchone() or ['{}'])[0] or '{}') or {}   # read again: the re-read above may have changed it
        con.execute('UPDATE loops SET shown=shown+?, payload=? WHERE id=?', (1 if key else 0, json.dumps(lp | {'last_day': day}), r['id']))
        if key:
            used += 1
            made.append(key)
    con.commit()
    return made


# ---- every tick ----------------------------------------------------------------------------------------------------------

def tend(con, life, now_dt: datetime, *, read=None, sent=None, extract=None, feed=None, inbox_db=None, max_reads=MAX_READS,
         max_extract=MAX_EXTRACT, discover=True) -> dict:
    """tick.run_phases calls this with the switch file present. Queue, read a few threads, close what code can, and
    turn the loops worth a line now into their own items. -> a small report; {} when the switch is off. Never sends."""
    if not on(life):
        return {}
    tables(con)
    if not discover:
        # Existing promises retain their settlement history. New threads and drafts belong to hourly research.
        closed = close_by_code(con, life, now_dt.timestamp())
        return {'closed': closed} if closed else {}
    read = read or read_thread
    sent = sent or list_sent
    extract = extract or _default_extract
    now = now_dt.timestamp()
    team_boxes = team(life)
    rep = {'queued': 0, 'read': 0, 'new': 0, 'closed': 0, 'errors': []}
    reads_left, extract_left = [max_reads], [max_extract]
    rep['queued'] = queue(con, life, now, inbox_db, team_boxes)
    # the owner's sent mail: ONE mailbox a tick (the one listed longest ago), each at most every SENT_EVERY_H hours, so the
    # lists never crowd out the thread reads
    state_file = Path(life) / 'loops-sent.json'
    try:
        listed = json.loads(state_file.read_text())
    except (OSError, ValueError):
        listed = {}
    due_boxes = sorted((float(listed.get(b) or 0), b) for b in _S().R.ACCOUNTS if now - float(listed.get(b) or 0) >= SENT_EVERY_H * 3600)
    for _, mailbox in due_boxes[:1]:
        if reads_left[0] <= 0:
            break
        reads_left[0] -= 1
        try:
            rep['queued'] += note_sent(con, mailbox, sent(mailbox), team_boxes, now)
            listed[mailbox] = now
        except Exception as e:
            rep['errors'].append(f'sent list {mailbox}: {type(e).__name__}')
    state_file.write_text(json.dumps(listed))
    rep['closed'] += close_by_code(con, life, now)
    # the lines go before the new threads, so re-reading them never waits behind a backlog
    items = plan_lines(con, life, now_dt, read, feed, reads_left, team_boxes, rep)
    if items:
        rep['line_items'] = items
    # threads: new ones first (people who wrote to the owner before the owner's own replies, newest first), then open loops due a re-read
    todo = con.execute("SELECT mailbox,thread_id,hint FROM loop_threads WHERE read IS NULL AND found>=? AND COALESCE(fails,0)<? "
                       "ORDER BY COALESCE(hint,'')='', found DESC", (now - WINDOW_DAYS * 86400, MAX_READ_FAILS)).fetchall()
    todo += con.execute("SELECT DISTINCT t.mailbox,t.thread_id,t.hint FROM loop_threads t JOIN loops l ON l.mailbox=t.mailbox AND "
                        "l.thread_id=t.thread_id WHERE l.state='open' AND l.kind!='promise' AND t.read<? AND t.hint IS NOT ? ORDER BY t.read", (now - RECHECK_H * 3600, OWNED)).fetchall()
    for t in todo:
        if reads_left[0] <= 0:
            break
        if not t['mailbox'] in _S().R.ACCOUNTS:
            continue
        reads_left[0] -= 1
        try:
            msgs = read(t['mailbox'], t['thread_id'])
        except Exception as e:
            rep['errors'].append(f'thread read: {type(e).__name__}')
            con.execute('UPDATE loop_threads SET fails=COALESCE(fails,0)+1 WHERE mailbox=? AND thread_id=?', (t['mailbox'], t['thread_id']))
            con.commit()
            continue
        got = apply_thread(con, t['mailbox'], t['thread_id'], msgs, t['hint'] or '', team_boxes, now, extract, extract_left, life=life)
        rep['read'] += 1
        rep['new'] += got['new']
        rep['closed'] += got['closed']
        rep['settled'] = rep.get('settled', 0) + got.get('settled', 0)
        if got.get('extract_failed'):
            rep['errors'].append('promise reading: no answer from the model')
    rep['closed'] += close_by_code(con, life, now)
    rep['open'] = con.execute("SELECT COUNT(*) FROM loops WHERE state='open'").fetchone()[0]
    return {k: v for k, v in rep.items() if v not in (0, [], None)} or {'open': 0}


# ---- `life loops` --------------------------------------------------------------------------------------------------------

def cli(args: list, life, db) -> int:
    """life loops                     the open loops, oldest first
    life loops close <n> "<why>"   one is settled (the owner handled it, or it is not the owner's)"""
    with closing(db()) as con:
        tables(con)
        rows = con.execute("SELECT * FROM loops WHERE state='open' ORDER BY opened").fetchall()
        today = his_clock(life, datetime.now(_pacific())).date()
        z = his_clock(life, datetime.now(_pacific())).tzinfo
        if args[:1] == ['close']:
            try:
                r = rows[int(args[1]) - 1]
            except (IndexError, ValueError):
                print('Which one? Use the number from: life loops')
                return 2
            _close(con, r['id'], 'closed in chat: ' + (' '.join(args[2:]) or 'the owner handled it'), time.time())
            con.commit()
            print(f"Closed: {line_fact(dict(r), today, z)['short']}")
            return 0
        if not rows:
            print('Nobody is waiting on the owner and the owner is not waiting on anyone (that Zeus has seen).')
            return 0
        for i, r in enumerate(rows, 1):
            f = line_fact(dict(r), today, z)
            print(f"{i}. {f['short']} ({f['about']})")
        return 0

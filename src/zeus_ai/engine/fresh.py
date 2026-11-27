"""Fresh: nothing stale reaches the owner, and the owner hears about one matter once.

Without this module an ask is never looked at again once it is written: a draft about a Thursday meeting stays as it
was when the meeting moves to Friday (tick.KEEP_ON_MERGE keeps the old draft and decision), a later "yes" approves
the old draft, an ask never ends, a note held at 9:40pm goes out at 9:00am as it stood, and any new email about a
thing the owner had dropped raises it again. The rules: when a thing changes after the assistant asked, redo the
draft and send a fresh ask that REPLACES the old one; an ask the owner never answers ends when the thing's own
deadline passes, with one line of its own.

What this module does, all of it in code and from stored facts:

  version    every item carries the version of the source record it came from (a mail thread: the noticed message
             and the messages after it; a calendar event: its time, status, place and guests). It is stored when
             the item is noticed, read again when a round of work begins, and kept on the result packet as
             "built_from".
  re-check   right before a note is written every item in it is read again and compared (gate):
               same      it goes
               handled   the owner answered the thread themselves: closes quietly, as before
               changed   the old result is put aside, the item goes back for fresh work, and if the owner had already been
                         asked the old ask is retired and the next one says it replaces it
               noise     the version moved but nothing real did (the same content delivered again, a new title):
                         the held note stays exactly as it is
               unknown   the source could not be read. Never "unchanged": the item waits (up to 30 minutes), then
                         goes with one plain sentence saying so. An urgent item is never held, only said.
  asked      asks already put to the owner are compared the same way on every run (tend): the calendar each run, a
             mail thread at most once an hour in the day. An ask whose own deadline has passed ends, with one line.
  arrival    another record about a thing already on the list (tick.upsert): the same content again is never
             news. A second note about a matter the owner was told needs a changed version AND a real change. A thing the owner
             dropped or handled reopens only when the version changed and the plan for it really changed: never
             for a later deadline, a copy, or new wording.
  links      (switch links-by-code) after the note is written, every link that is not, character for character,
             one of the links code itself found in the items' source mail (written out in it, or where its HTML
             links point) is taken out. A text line shows a plain address or nothing: no markdown.

Where a model's word is still used, and how code bounds it, is written at each place (plan_change, talked).
Switches (files in the life folder): `fresh-on` for everything except links, `links-by-code` for the links.
With neither file every function here returns what it was given and nothing is stored.

Three readers look at the real world. Tests replace them; nothing else in this file reads outside the ledger:
  read_thread    sources.thread_tail (the same read-only thread read the engine already uses)
  read_calendar  the calendar snapshot (the file sources.calendar_observations reads, sources.CAL_SNAPSHOT)
  read_chat      chats.messages (the engine's one read-only reader of the owner's chat)
One thing is handed in instead of read: raw_links gets each mail as it arrived from sources.email_observation.

A bug in the bookkeeping (stamp, arrival, work_began, packet_ready) never loses mail or a result: the engine carries
on as before and the trouble is written on the item. A bug in gate, composing, final_text or tend is not swallowed:
that phase of the run fails loudly (exit 1) and no unchecked note goes out.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ownertime import PACIFIC   # glue.py: the owner's zone right now (settings.OWNER_TZ unless `life zone` said otherwise), so time words are on the owner's clock
import settings
SWITCH = 'fresh-on'
LINKS_SWITCH = 'links-by-code'
OPEN_STATES = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')
HOLD_UNKNOWN = 30 * 60        # an item whose source cannot be read waits this long for a good read, then goes and says so
MAX_REDOS = 2                 # times one item is sent back because its source changed; after that it goes and says so
FACTS_FRESH_FOR = 6 * 3600    # a result older than this when its note would go gets one fresh look first. Six hours: nothing
                              # worked and told the same day is touched, anything that slept overnight is
REFRESH_LEAD = 40 * 60        # that fresh look starts this long before the note (worker.PREP_LEAD)
MAIL_SWEEP_GAP = 60 * 60      # an ask already put to the owner: its mail thread is read again at most this often, 9am to 9pm only
MAIL_READS_PER_SWEEP = 4      # and at most this many threads a run (each is one outside read of up to 50 seconds)
CHAT_GAP = 10 * 60            # with the chat unreadable, a result younger than this passes: its own job read the chat that recently
TAIL_KEPT = 30
KEPT = 12                     # contents remembered per item
_COMMON = {'your', 'this', 'that', 'with', 'from', 'have', 'will', 'send', 'reply', 'about', 'should', 'would', 'ready',
           'draft', 'check', 'plan', 'prepare', 'confirm', 'answer', 'today', 'tomorrow', 'before', 'after', 'whether', 'them', 'these'}
_COMMON |= {w for w in str(settings.OWNER_NAME).lower().split() if len(w) >= 4}   # the owner's own name is never a distinctive word

# Every sentence below can reach the owner word for word. Plain words, no names of parts.
# No sentence quotes an item's title. A title is the engine's own
# label for an item ("Today's schedule (from the owner's calendar)"), and in quotes it reads like a system. A sentence names the
# thing by what the owner really saw (the question the owner was asked, quoted) or where it came from (the email thread from a
# person, the owner's calendar), or says "that" when the note carries only that one thing. And every time in these sentences
# is worded when the text goes out ({when} etc. are filled by `speak` at that moment), never when the line was made:
# "Its deadline passed at 5pm", made at 5:01pm and sent at 9am the next day, would be wrong.
# These sentences are the LAST resort. Each fact they
# carry is handed to the note writer with its item first ("tell_him", see owed_facts), the writer says it in its own
# words, code checks that it did, and only a fact still missing after one targeted fix gets its sentence here. No
# sentence quotes the question the owner was asked: the stored question is not the text the owner was sent.
EXPIRED = 'What I asked you {asked} about {thing} is off now: its deadline passed {when}, so I did not go ahead.'
EXPIRED_NO_ASK = 'What I asked you {asked} is off now: its deadline passed {when}, so I did not go ahead.'
WITHDRAWN = 'Ignore what I asked you {asked}: {what}, so nothing is needed from you there now.'
REPLACES = 'This replaces what I asked you {asked}: {what}.'
UNREAD = 'That is as it stood {when}.'                        # the time the facts are from, never the assistant's own access
UNREAD_MANY = 'Some of this is as it stood earlier.'
MOVING = '{Subject} keeps changing, so {part} is as it stood {when}.'
SINCE = 'One change since I first looked{at}: {what}.'
PASSED = 'The deadline for {that} passed {when}, while it was waiting to reach you.'
STOOD = 'That is as it stood {when}.'
ASK_QUOTED_MAX = 200          # a question longer than this is not shown to the writer as "what it was about"


def on(life) -> bool:
    """Switch file `fresh-on` in the life folder. Without it nothing here stamps, compares, holds, retires or ends
    anything, and the tick, the worker and the note are exactly what they were."""
    return (Path(life) / SWITCH).exists()


def links_on(life) -> bool:
    """Switch file `links-by-code` in the life folder: links in a note are checked against the stored source links."""
    return life is not None   # always on: the link check is part of the normal path


def life_of(con) -> Path:
    """The life folder a ledger connection belongs to (worker._ready is not handed the folder)."""
    return Path(con.execute('PRAGMA database_list').fetchone()[2]).parent


# ------------------------------------------------------------------------------------------------ the three readers
def read_thread(mailbox, thread_id, message_id):
    """The messages in the thread after `message_id`, as sources.thread_tail gives them:
    [{'from', 'at', 'from_owner', 'preview'}]. None = could not be read (never "nothing new")."""
    try:
        import sources as S
        account = S.R.ACCOUNTS.get(mailbox)
        if not account or not message_id:
            return None
        if not thread_id:
            m = S.R.read_message(account, message_id)
            thread_id = m.get('threadId') if isinstance(m, dict) else None
        return S.thread_tail(account, thread_id, message_id) if thread_id else None
    except Exception:
        return None


def read_calendar():
    """The calendar snapshot (sources.CAL_SNAPSHOT): {'events', 'complete', 'checked_at', 'errors'}. None = the copy could
    not be opened, or no calendar is configured (sources.CALENDAR_CONFIGURED)."""
    try:
        import sources as S
        if not getattr(S, 'CALENDAR_CONFIGURED', True):
            return None
        snap = json.loads(S.CAL_SNAPSHOT.read_text())
    except Exception:
        return None
    if not isinstance(snap, dict) or not isinstance(snap.get('events'), list):
        return None
    return {'events': snap['events'], 'complete': bool(snap.get('complete')), 'checked_at': snap.get('checked_at'),
            'errors': snap.get('errors') or []}


def read_chat(since: float):
    """What the owner personally said to the assistant since a time: [(when, text)]. None = the chat could not be read."""
    try:
        import chats as C
        return [(float(m['at']), str(m['text'])) for m in C.messages(float(since), path=os.environ.get('LIFE_CHAT_DB') or None)
                if m['who'] == 'owner']
    except Exception:
        return None


OWN = None    # the owner's own addresses; a test may set this instead of loading sources.py


def _mailbox(address: str) -> str:
    """One spelling per mailbox (the rule of sources.same_mailbox): lower case, no "+tag", no dots for Gmail."""
    local, _, domain = str(address or '').strip().lower().partition('@')
    local = local.split('+', 1)[0]
    if domain in ('gmail.com', 'googlemail.com'):
        local, domain = local.replace('.', ''), 'gmail.com'
    return f'{local}@{domain}' if local and domain else ''


def _from_owner(sender) -> bool:
    """Did the owner send this personally? When that cannot be told the answer is no: a message is then treated as someone
    else's, which can cost one fresh look but never hides a change."""
    global OWN
    found = re.findall(r"[a-z0-9][a-z0-9._%+'\-]*@[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", str(sender or '').lower())
    if not found:
        return False
    if OWN is None:
        try:
            import sources as S
            OWN = {_mailbox(a) for a in S.OWN_ADDRESSES}
        except Exception:
            return False
    return _mailbox(found[-1]) in OWN


# ------------------------------------------------------------------------------------------------ versions
def _sha(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:20]


def _flat(text) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower()).strip()


def _address(sender) -> str:
    found = re.findall(r"[a-z0-9][a-z0-9._%+'\-]*@[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", str(sender or '').lower())
    return found[-1] if found else _flat(sender)


_REPLY_MARK = re.compile(r'^\s*((re|fw|fwd)\s*:\s*)+', re.I)
_DATE_WORDS = re.compile(r'\b(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\b|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|'
                         r'july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b|\b(?:today|tomorrow|tonight|am|pm)\b')


def content_keys(obs: dict) -> tuple:
    """-> (content, shape) of one email as it was read. `content` is the same for the same words from the same sender,
    whatever id or mailbox the copy arrived under. `shape` is the same when only the dates, times and numbers differ
    (a reminder sent again with a new date)."""
    words = _flat(_REPLY_MARK.sub('', str(obs.get('subject') or '')) + ' ' + str(obs.get('text') or ''))[:3000]
    who = _address(obs.get('from'))
    shape = re.sub(r'(?:# ?)+', '# ', re.sub(r'\d+', '#', _DATE_WORDS.sub('#', words)))
    return _sha('content', who, words), _sha('shape', who, shape)


def preview_key(sender, text) -> str:
    """One message inside a thread, by who sent it and how it begins (the thread read gives a short preview only)."""
    return _sha('preview', _address(sender), _flat(text)[:80])


def _ts(value):
    """A message time as seconds, from whatever the mail read gave (epoch seconds or milliseconds, ISO, or a mail
    date line). None = cannot be read."""
    if value in (None, ''):
        return None
    try:
        n = float(value)
        return n / 1000 if n > 1e11 else n
    except (TypeError, ValueError):
        pass
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            t = parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def _moment(value):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00')) if value else None
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=PACIFIC)).astimezone(PACIFIC) if t else None


def _tail_rows(tail) -> list:
    return [{'at': t.get('at'), 'from': str(t.get('from') or '')[:80], 'mine': bool(t.get('from_owner')),
             'c': preview_key(t.get('from'), t.get('preview'))} | ({'id': str(t['id'])} if t.get('id') else {}) for t in tail if isinstance(t, dict)]


def _seen_before(rows) -> object:
    """-> a test: was this message already among `rows` (the messages of an earlier reading of the same thread)?
    By its own id when both readings have one. A reading stored before ids were kept (or by a read that gave none)
    is matched by time and sender, as before, so an old stamp never makes every message look new."""
    rows = [t for t in rows or [] if isinstance(t, dict)]
    ids = {str(t['id']) for t in rows if t.get('id')}
    pairs = {(str(t.get('at')), t.get('from')) for t in rows}
    old_pairs = {(str(t.get('at')), t.get('from')) for t in rows if not t.get('id')}

    def seen(r) -> bool:
        pair = (str(r.get('at')), r.get('from'))
        return (str(r['id']) in ids or pair in old_pairs) if r.get('id') else pair in pairs
    return seen


def mail_stamp(mailbox, thread_id, anchor, tail, partial=False, at=None) -> dict:
    """The version of a mail thread as seen from one message: that message and everything after it.
    tail = what the thread read returned; None (could not be read) gives a stamp whose version is None."""
    st = {'kind': 'mail', 'mailbox': mailbox, 'thread_id': thread_id, 'anchor': str(anchor or ''), 'at': at or time.time()}
    if tail is None:
        return st | {'v': None, 'tail': None}
    rows = _tail_rows(tail)[-TAIL_KEPT:]
    return st | {'tail': rows, 'partial': bool(partial), 'v': _sha('mail', st['anchor'], [(r['at'], r['from'], r['mine']) for r in rows])}


def _person(a) -> str:
    if isinstance(a, dict):
        return str(a.get('email') or a.get('name') or a.get('displayName') or '').strip().lower()
    return str(a or '').strip().lower()


def calendar_facts(e: dict) -> dict:
    """What can really change about a meeting: when, whether it is on, where and with whom. Its title is wording."""
    return {'start': e.get('start'), 'end': e.get('end'), 'all_day': bool(e.get('all_day')), 'status': str(e.get('status') or 'confirmed'),
            'place': _flat(e.get('location')), 'people': sorted({p for p in (_person(a) for a in e.get('attendees') or []) if p})}


def calendar_stamp(e: dict, ref, day, inst=None, at=None) -> dict:
    facts = calendar_facts(e)
    return {'kind': 'calendar', 'ref': ref, 'day': day, 'inst': list(inst if inst is not None else (e.get('instances') or [])),
            'facts': facts, 'title': str(e.get('title') or '')[:120], 'at': at or time.time(), 'v': _sha('calendar', facts, str(e.get('title') or ''))}


def find_event(events: list, ref, day, inst):
    """One occurrence in the calendar copy. First by its own id (the copy keeps one per occurrence, stable when it
    moves), then by series and day, then, for a one-off whose id was never stored, by a series with a single event."""
    inst = set(inst or [])
    if inst:
        for e in events:
            if inst & set(e.get('instances') or []):
                return e
    for e in events:
        if ref in (e.get('series_key'), e.get('key')) and str(e.get('start') or '')[:10] == str(day or ''):
            return e
    if not inst:
        same = [e for e in events if e.get('series_key') == ref]
        if len(same) == 1:
            return same[0]
    return None


def same_occurrence(a: dict, b: dict) -> bool:
    """Are two calendar stamps the same single meeting? By the occurrence's own id when both have one (it stays the same
    when the meeting moves); otherwise the same series on the same day. Next week's meeting of a weekly series is not
    this week's meeting moved."""
    if a.get('inst') and b.get('inst'):
        return bool(set(a['inst']) & set(b['inst']))
    return bool(a.get('ref')) and a.get('ref') == b.get('ref') and a.get('day') == b.get('day')


def stamp_of(obs: dict) -> dict:
    """The version of what was just observed, from the observation itself (no outside read)."""
    if obs.get('source') == 'email':
        tail = obs.get('later_in_thread') if 'later_in_thread' in obs else None
        return mail_stamp(obs.get('mailbox'), obs.get('thread_id'), str(obs.get('id', '')).split(':', 2)[-1], tail,
                          partial=tail is not None and len(tail) >= 6)
    if obs.get('source') == 'calendar':
        _, ref, day = (str(obs.get('id') or '').split(':', 2) + ['', ''])[:3]
        inst = []
        cal = read_calendar()       # the observation does not carry the occurrence's own id; the copy it came from does
        if cal:
            inst = next((list(e.get('instances') or []) for e in cal['events']
                         if ref in (e.get('series_key'), e.get('key')) and e.get('start') == obs.get('start')), [])
        return calendar_stamp(obs, ref, day, inst)
    return {'kind': 'none', 'v': '-', 'at': time.time()}


# ------------------------------------------------------------------------------------------------ plain words
def _clock(t: datetime) -> str:
    return t.strftime('%I:%M%p').lstrip('0').lower().replace(':00', '')


def _day(t: datetime, now_dt: datetime) -> str:
    days = (t.date() - now_dt.date()).days
    if days in (0, 1, -1):
        return ('yesterday', 'today', 'tomorrow')[days + 1]
    if 1 < days <= 6:
        return t.strftime('%A')
    return t.strftime('%A %b ') + str(t.day)


def when_words(value, now_dt: datetime, all_day=False) -> str:
    """'Thursday at 3pm', 'tomorrow at 9:30am', 'Friday Nov 20 at 2pm'. Empty when the time cannot be read."""
    t = _moment(value) if not isinstance(value, datetime) else value.astimezone(PACIFIC)
    if t is None:
        return ''
    return _day(t, now_dt) if all_day else f'{_day(t, now_dt)} at {_clock(t)}'


def stood_words(ts, now_dt: datetime) -> str:
    """'at 8:20am', 'yesterday at 9:40pm', 'on Tuesday at 4pm': when a result was worked out, the way the owner would say it."""
    t = datetime.fromtimestamp(float(ts), PACIFIC)
    days = (now_dt.date() - t.date()).days
    return f'at {_clock(t)}' if days == 0 else f'yesterday at {_clock(t)}' if days == 1 else f"on {t.strftime('%A')} at {_clock(t)}"


def _name(sender) -> str:
    name = re.sub(r'<[^>]*>|"', '', str(sender or '')).strip()
    return name if name and '@' not in name else (_address(sender) or 'someone')


# ---- time words worded when the text goes out -------------------------------------------------------------------------
# A sentence that is made now and may be read later carries its times as marks, not words. `speak` turns the marks
# into words for the moment the text goes out: "yesterday at 5pm", "at 5pm" (the same day), "on Tuesday at 5pm".
# The marks never leave this file: every place that hands such a sentence to the owner, to the note writer, to the chat
# Zeus or to a job calls `speak` first, and final_text speaks the whole note once more as the last step.
# Kinds: 'w' a deadline-like moment ("yesterday at 5pm", "at 5pm", "on Friday Nov 20 at 5pm"); 'm' a meeting's time
# ("Thursday at 3pm"); 's' when something was worked out or asked ("at 8:20am", "yesterday at 9:40pm").
_MARK = '\x1f'
_MARKS = re.compile(_MARK + r'([wms])([dt])([^' + _MARK + r']*)' + _MARK)


def at_mark(value, kind='w', all_day=False) -> str:
    """A time that `speak` words later. value: an ISO time, a datetime, or (kind 's') seconds."""
    if isinstance(value, datetime):
        value = value.isoformat()
    return f"{_MARK}{kind}{'d' if all_day else 't'}{value}{_MARK}"


def _said_when(kind, all_day, value, now_dt):
    if kind == 's':
        try:
            ts = float(value)
        except (TypeError, ValueError):
            return 'earlier'
        if now_dt is None:
            t = datetime.fromtimestamp(ts, PACIFIC)
            return f"on {t.strftime('%A %b')} {t.day} at {_clock(t)}"
        return stood_words(ts, now_dt)
    t = _moment(value)
    if t is None:
        return 'earlier' if kind == 'w' else 'another time'
    if now_dt is None:             # no moment of sending known: a date that is true whenever it is read
        day = f"{t.strftime('%A %b')} {t.day}"
        return (day if all_day else f'{day} at {_clock(t)}') if kind == 'm' else ('on ' + day if all_day else f'on {day} at {_clock(t)}')
    words = when_words(t, now_dt, all_day)
    if kind == 'm':
        return words
    if -6 <= (t.date() - now_dt.date()).days < -1:     # earlier this week: "on Tuesday at 5pm", the day is enough
        words = t.strftime('%A') if all_day else f"{t.strftime('%A')} at {_clock(t)}"
    return (words if words.startswith(('yesterday', 'today', 'tomorrow')) else 'on ' + words).replace('today at', 'at')


def speak(text, now_dt=None):
    """Word every time mark in `text` for `now_dt` (the moment it goes out). now_dt None: a full date, true whenever
    it is read. Text with no marks comes back exactly as it was."""
    if not isinstance(text, str) or _MARK not in text:
        return text
    text = _MARKS.sub(lambda m: _said_when(m.group(1), m.group(2) == 'd', m.group(3), now_dt), text)
    return text.replace(_MARK, '')


def _change_marked(a: dict, b: dict) -> str:
    """calendar_change, with the meeting's times as marks (worded by `speak` when it is told)."""
    a, b = a or {}, b or {}
    if b.get('status') == 'cancelled' and a.get('status') != 'cancelled':
        return 'it was cancelled'
    if _moment(a.get('start')) != _moment(b.get('start')) or bool(a.get('all_day')) != bool(b.get('all_day')):
        if _moment(a.get('start')) and _moment(b.get('start')):
            return f"it moved from {at_mark(a.get('start'), 'm', a.get('all_day'))} to {at_mark(b.get('start'), 'm', b.get('all_day'))}"
        return 'its time changed'
    if _moment(a.get('end')) != _moment(b.get('end')):
        end = _moment(b.get('end'))
        return f'it now ends at {_clock(end)}' if end else 'its end time changed'
    if (a.get('place') or '') != (b.get('place') or ''):
        return 'the place changed'
    if list(a.get('people') or []) != list(b.get('people') or []):
        return 'the guest list changed'
    return ''


def calendar_change(a: dict, b: dict, now_dt: datetime) -> str:
    """What really changed between two readings of one meeting, in plain words. '' = nothing real (a new title is
    wording). Code decides this alone: time, cancelled, place, people."""
    return speak(_change_marked(a, b), now_dt)


def plan_change(old: dict, opp: dict) -> str:
    """Did the plan for a thing the owner dropped or handled really change? '' = no.
    This is the one place a model's word decides a reopen: the noticer's plan for the new arrival (its deadline, its
    event time, which kinds of step it holds). Code bounds it three ways. The arriving record must be one this item
    has never seen, by id and by content (arrival checks that first). Only these structured fields are compared, never
    the title, the reason or any other wording. And each step is read again by policy.guard_class, so a step counts as
    money, a message, a calendar change or a delete by its own words, not by the label the model gave it.
    A deadline that moved later is never a change."""
    import policy as P
    was, now = _moment(old.get('deadline')), _moment(opp.get('deadline'))
    if was and now and now < was - timedelta(minutes=1):
        return 'its deadline moved earlier'
    was, now = _moment(old.get('event_start')), _moment(opp.get('event_start'))
    if was and now and was != now:
        return 'its time changed'
    had = {s.get('action_class') for s in ((old.get('decision') or {}).get('steps') or []) if isinstance(s, dict)} & P.STRICT
    has = {P.guard_class(s) for s in (opp.get('steps') or []) if isinstance(s, dict)} & P.STRICT
    return 'it now needs something it did not need before' if has - had else ''


# ------------------------------------------------------------------------------------------------ on the item
def _f(opp: dict) -> dict:
    f = opp.get('fresh')
    if not isinstance(f, dict):
        f = opp['fresh'] = {}
    return f


def _list(sources) -> list:
    if isinstance(sources, (list, tuple)):
        return list(sources)
    try:
        got = json.loads(sources or '[]')
    except (TypeError, ValueError):
        return []
    return got if isinstance(got, list) else []


def _remember(f: dict, name: str, value) -> None:
    if value and value not in (f.get(name) or []):
        f[name] = ((f.get(name) or []) + [value])[-KEPT:]


LINK = re.compile(r"""(?<![\w@./-])(?:https?://|mailto:|www\.|(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|gov|edu|us|app|ai|me|ly)/)[^\s<>"'\]]*""", re.I)
_MD_LINK = re.compile(r'\[([^\]\n]{1,200})\]\(\s*<?([^)\s>]+)>?\s*\)')
_ANGLE = re.compile(r'<((?:https?://|mailto:|www\.)[^>\s]+)>', re.I)
_TRAIL = '.,;:!?)]}\'"'
GONE = '\x00'     # stands where a link was taken out, until the words around it are tidied


def _core(link: str) -> str:
    core = link.rstrip(_TRAIL)
    return core + ')' if link[len(core):].startswith(')') and core.count('(') > core.count(')') else core


_RAW = {}         # observation id -> where the links in that mail's own HTML point, for this run only (raw_links)
LINKS_KEPT = 40   # per item
LINKS_ROOM = 8000  # characters of links per item: tracking links are long


def raw_links(obs_id, raw_body) -> None:
    """sources.email_observation hands over the mail as it arrived. The engine reads mail as plain text, which keeps
    the words of a link ("Check in") and drops where it points, so the links of an HTML mail are picked up here. They
    are held beside the run and stored on the item by stamp / arrival; they never enter an observation or a prompt.
    Does nothing without the switch file links-by-code. Never raises."""
    try:
        life = getattr(sys.modules.get('tick'), 'LIFE', None) or getattr(sys.modules.get('__main__'), 'LIFE', None)   # tick.py run as a script is __main__
        if life is None or not links_on(life):
            return
        import html
        found = []
        for m in re.finditer(r"""href\s*=\s*(?:"([^"]+)"|'([^']+)')""", str(raw_body or ''), re.I):
            link = html.unescape(m.group(1) or m.group(2) or '').strip()
            if link.lower().startswith('https://') and 12 <= len(link) <= 600 and link not in found:
                found.append(link)
        if len(_RAW) > 300:
            _RAW.clear()
        _RAW[str(obs_id)] = found[:LINKS_KEPT]
    except Exception:
        pass


def source_links(obs: dict) -> list:
    """The links that are really in a source email, exactly as they are there, found by code when it is noticed: the
    ones written out in its text, and where its HTML links point (raw_links). Only https links. None from an email
    the screen flagged as written to steer an assistant (screen.py)."""
    if obs.get('source') != 'email' or obs.get('screen_warning'):
        return []
    out = []
    for m in LINK.finditer(str(obs.get('text') or '')):
        core = _core(m.group(0))
        if core.lower().startswith('https://') and 12 <= len(core) <= 600 and core not in out:
            out.append(core)
    out += [link for link in _RAW.get(str(obs.get('id')), []) if link not in out]
    return out[:LINKS_KEPT]


def _keep_links(f: dict, links: list) -> None:
    have = list(f.get('links') or [])
    for link in links:
        if link not in have and len(have) < LINKS_KEPT and sum(len(x) for x in have) + len(link) <= LINKS_ROOM:
            have.append(link)
    if have:
        f['links'] = have


def _note_arrival(life, f: dict, obs: dict) -> None:
    if on(life):
        watch = __import__('glue').watch_stamp(obs)   # glue.py: a watched page's change is stamped beside the item's own source, never over it
        if watch:
            f['watch'] = watch
        else:
            f['seen'] = stamp_of(obs)
        if obs.get('source') == 'email':
            if not f.get('who') and obs.get('from') and not _from_owner(obs.get('from')):
                f['who'] = _name(obs.get('from'))[:60]    # who the thread is with, so a sentence to the owner can name it instead of a title
            if obs.get('from') and not _from_owner(obs.get('from')):
                f['last_from'] = _name(obs.get('from'))[:60]   # who sent the newest mail merged into it (so a sentence can say "X sent a newer email")
            content, shape = content_keys(obs)
            _remember(f, 'contents', content)
            _remember(f, 'shapes', shape)
            _remember(f, 'previews', preview_key(obs.get('from'), obs.get('text')))
    if links_on(life):
        _keep_links(f, source_links(obs))


def stamp(con, payload: dict, obs: dict) -> None:
    """tick.upsert, a new item: store the version of what it was noticed from (and, with links-by-code, the links
    that are really in its source mail). A stamp that cannot be worked out is left off: unknown, never invented."""
    try:
        life = life_of(con)
        if not (on(life) or links_on(life)):
            return
    except Exception:
        return
    try:
        _note_arrival(life, _f(payload), obs)
    except Exception as e:
        _f(payload)['stamp_error'] = f'{type(e).__name__}: {str(e)[:120]}'


def closed_by_him(state: str, payload: dict) -> bool:
    """The owner dropped it, muted it, finished it in chat, or answered the thread personally (as against: the assistant told
    the owner and it simply ended). The name is kept for its callers (loops.py)."""
    c = payload.get('closed') or {}
    return state == 'declined' or str(c.get('by') or '').startswith('zeus chat') or 'answered the thread' in str(c.get('why') or '')


def arrival(con, row, old: dict, srcs: list, obs: dict, opp: dict):
    """tick.upsert, another record about a thing already on the list. -> 'duplicate' (already settled here: not
    news), 'reopen' (a closed thing really changed: one update item may be made), or None (carry on as before).

    Open item: the same content delivered again (another id, another mailbox, a resend) is not merged at all, so it
    cannot move the "newest email" mark, restart work or cancel a held note. Anything else is merged as before and
    its version and content are remembered.
    Closed item, lock one (code): the record is new to this item by id and by content; for a calendar event the
    stored facts differ. Lock two (code where it can be):
      a meeting   time, cancelled, place or people changed (calendar_change). A new title alone never.
      a mail      the same message with only dates and numbers different reopens only for a strictly earlier
                  deadline, never a later one.
                  A thing the OWNER dropped or handled reopens only when the plan itself changed (plan_change): this is
                  where the noticer's word is used, bounded as written there. New wording never reopens it.
                  A thing the owner was only told about gets a second note for a new message from someone other than the owner.
    A reopening content is remembered on the closed item, so it reopens once.
    Trouble in here never loses mail: the answer is then None (the engine carries on as it always did) and the
    trouble is written on the item."""
    try:
        return _arrival(con, row, old, srcs, obs, opp)
    except Exception as e:
        if isinstance(old, dict):
            _f(old)['arrival_error'] = f'{type(e).__name__}: {str(e)[:120]}'
        return None


def _arrival(con, row, old: dict, srcs: list, obs: dict, opp: dict):
    life = life_of(con)
    if not on(life):
        if links_on(life) and row['state'] in OPEN_STATES and source_links(obs):
            _keep_links(_f(old), source_links(obs))
        return None
    f = _f(old)
    kind = obs.get('source')

    def settle(verdict='duplicate'):
        con.execute('UPDATE opps SET payload=?, sources=? WHERE key=?',
                    (json.dumps(old, default=str), json.dumps(sorted(set(srcs) | ({obs['id']} if verdict == 'duplicate' else set()))), row['key']))
        return verdict

    content, shape = content_keys(obs) if kind == 'email' else ('', '')
    if row['state'] in OPEN_STATES:
        if kind == 'email' and content in (f.get('contents') or []):
            f['again'] = int(f.get('again') or 0) + 1
            return settle()
        _note_arrival(life, f, obs)
        f.pop('swept_at', None)             # something real arrived: an ask already put to the owner is compared on this very run
        return None
    if kind == 'calendar':
        base = f.get('seen') if isinstance(f.get('seen'), dict) and f['seen'].get('kind') == 'calendar' else None
        if base is None:
            return None                     # nothing stored to compare with: a calendar copy of a closed item is not news, as before
        new = stamp_of(obs)
        if not same_occurrence(base, new):
            return None                     # another meeting (next week's, or another calendar's copy): as before
        what = calendar_change(base.get('facts'), new['facts'], datetime.now(PACIFIC))
        if not what:
            return None
        f['seen'] = new
        f['reopened'] = {'at': time.time(), 'why': what}
        return settle('reopen')
    if kind != 'email' or obs['id'] in srcs:
        return None
    if content in (f.get('contents') or []):
        return settle()
    earlier = bool(_moment(old.get('deadline')) and _moment(opp.get('deadline'))
                   and _moment(opp['deadline']) < _moment(old['deadline']) - timedelta(minutes=1))
    why = ''
    if shape in (f.get('shapes') or []):
        why = 'its deadline moved earlier' if earlier else ''
    elif closed_by_him(row['state'], old):
        why = plan_change(old, opp)
    elif not _from_owner(obs.get('from')):
        why = 'a new message came in'
    _remember(f, 'contents', content)       # settled either way: this content is never weighed for this item again
    _remember(f, 'shapes', shape)
    if not why:
        return settle()
    f['reopened'] = {'at': time.time(), 'why': why}
    return settle('reopen')


def feedback(con, key: str, what: str, his_words: str = '') -> bool:
    """The owner said a note was useful or not useful. That is recorded on the item and is NOT a dismissal: the item keeps
    its state, nothing is closed or muted, and nothing about reopening changes. (Dropping is `life drop`, stopping a
    kind of thing is `life mute`.) -> False when there is no such item."""
    row = con.execute('SELECT payload FROM opps WHERE key=?', (key,)).fetchone()
    if row is None or what not in ('useful', 'not_useful'):
        return False
    opp = json.loads(row['payload'])
    f = _f(opp)
    f['feedback'] = ((f.get('feedback') or []) + [{'what': what, 'his_words': str(his_words)[:300], 'at': time.time()}])[-6:]
    con.execute('UPDATE opps SET payload=? WHERE key=?', (json.dumps(opp, default=str), key))
    con.commit()
    return True


# ------------------------------------------------------------------------------------------------ the comparison
def source_kind(key: str, opp: dict) -> str:
    """'mail', 'calendar' or 'none'. A later check hanging off an item, and a thing the owner mentioned in chat, have no
    record of their own to read again."""
    if '#fu' in key or opp.get('followup'):
        return 'none'
    return {'email': 'mail', 'calendar': 'calendar'}.get(opp.get('_source'), 'none')


def _mail_ref(opp: dict, sources):
    mail = __import__('integrity').own_mail(opp, opp.get('latest_email') or {})   # integrity.py (switch commit-integrity-on): a newest-email mark in another thread than the item's own is not used; without the switch the same dict comes back
    mailbox, mid, tid = mail.get('mailbox'), mail.get('message_id'), mail.get('thread_id')
    if not mid:
        ids = [s for s in _list(sources) if str(s).startswith('email:')]
        if len(ids) != 1:
            return None          # several mails merged and the newest is not known: cannot tell
        _, mailbox, mid = ids[0].split(':', 2)
        tid = (opp.get('thread') or {}).get('thread_id')
    return (mailbox, tid, str(mid)) if mailbox and mid else None


def _calendar_ref(opp: dict, sources):
    for st in ((opp.get('packet') or {}).get('built_from'), (opp.get('fresh') or {}).get('seen') if isinstance(opp.get('fresh'), dict) else None):
        if isinstance(st, dict) and st.get('kind') == 'calendar' and st.get('ref'):
            return st['ref'], st.get('day'), st.get('inst') or []
    ids = [s for s in _list(sources) if str(s).startswith('calendar:')]
    if not ids:
        return None
    _, ref, day = (str(ids[-1]).split(':', 2) + ['', ''])[:3]
    return (ref, day, []) if ref else None


def _base(opp: dict):
    """What the current result was built from: the stamp on its packet, else the one from when it was noticed."""
    f = opp.get('fresh') if isinstance(opp.get('fresh'), dict) else {}
    st = (opp.get('packet') or {}).get('built_from') or f.get('began') or f.get('seen')
    return st if isinstance(st, dict) else None


def built_at(opp: dict):
    """When the current result was worked out (the start of that work), as seconds. None = not known."""
    p, ex = opp.get('packet') or {}, opp.get('exec') or {}
    for v in (p.get('built_at'), ex.get('run_at_ts'), opp.get('ready_at')):
        try:
            if v:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _v(is_, what='', stamp=None, where='', marked=None) -> dict:
    """'what': plain words worded now. 'marked': the same words with their times as marks (speak), for a sentence
    that is told later."""
    return {'is': is_, 'what': what, 'stamp': stamp, 'where': where, 'marked': what if marked is None else marked}


def _mail_verdict(opp: dict, sources, at=None) -> dict:
    where = 'the email thread'
    ref = _mail_ref(opp, sources)
    if ref is None:
        return _v('unknown', where=where)
    mailbox, tid, mid = ref
    tail = read_thread(mailbox, tid, mid)
    if tail is None:
        return _v('unknown', where=where)
    rows = _tail_rows(tail)
    now = mail_stamp(mailbox, tid, mid, tail, at=at)
    if any(r['mine'] for r in rows):
        return _v('handled', 'The owner already answered the thread personally', now)
    base = _base(opp)
    base = base if base and base.get('kind') == 'mail' else None
    if base and base.get('anchor') and base['anchor'] != mid:
        who = ((opp.get('fresh') or {}).get('last_from') if isinstance(opp.get('fresh'), dict) else None)
        return _v('changed', f'{who} sent a newer email about it' if who else 'a newer email about it came in', now)   # merged after the work began (tick.upsert)
    began = built_at(opp)
    if base and base.get('tail') is not None:
        had = _seen_before(base['tail'])
        new = [r for r in rows if not had(r)]
        if base.get('partial'):             # only the last few were stored when it was noticed: older ones are not new
            new = [r for r in new if _ts(r['at']) is None or _ts(r['at']) > float(base.get('at') or 0) - 60]
    else:                                   # no reading from then: a message is new when its own time is after the work began
        new = [r for r in rows if began is None or _ts(r['at']) is None or _ts(r['at']) > began - 60]
    if not new:
        return _v('same', stamp=now)
    f = opp.get('fresh') if isinstance(opp.get('fresh'), dict) else {}
    known = {t.get('c') for t in ((base or {}).get('tail') or [])} | set(f.get('previews') or [])
    # With an id, the id decides alone: a message this item itself was noticed from (its id is in the item's own
    # list of sources) is not somebody writing again, and any other id is a message nobody has weighed yet, however
    # it begins (two different mails from one sender often begin alike; real previews do not begin like the text).
    # Only a row with no id falls back to comparing how it begins, as before.
    own = {str(s).split(':', 2)[2] for s in _list(sources) if str(s).startswith(f'email:{mailbox}:')} | {mid}
    real = [r for r in new if not (str(r['id']) in own if r.get('id') else r['c'] in known)]
    if not real:
        return _v('noise', 'the same message was delivered again', now)
    return _v('changed', f"{_name(real[-1]['from'])} wrote again in that thread", now)


def _once(read):
    """A reader that reads at most once: one look at the calendar copy per sweep, however many items ask."""
    got = []

    def cached():
        if not got:
            got.append(read())
        return got[0]
    return cached


def _calendar_verdict(opp: dict, sources, now_dt: datetime, calendar=None) -> dict:
    where = 'your calendar'
    ref = _calendar_ref(opp, sources)
    cal = (calendar or read_calendar)() if ref else None
    if cal is None:
        return _v('unknown', where=where)
    try:
        import lifehealth as H
        if H.calendar_stale(cal.get('checked_at'), now_dt):
            return _v('unknown', where=where)   # the copy is older than its own schedule allows: it cannot say "unchanged"
    except Exception:
        return _v('unknown', where=where)
    base = _base(opp)
    facts = base.get('facts') if base and base.get('kind') == 'calendar' else None
    e = find_event(cal['events'], *ref)
    if e is None:
        if not cal['complete'] or cal['errors']:
            return _v('unknown', where=where)   # a calendar was not read: it may simply be on that one
        start = _moment((facts or {}).get('start') or opp.get('event_start'))
        if start is None or start <= now_dt:
            return _v('same')                   # it has begun or is over; the copy no longer lists it
        return _v('changed', 'it is no longer on your calendar')
    now = calendar_stamp(e, ref[0], str(e.get('start') or '')[:10], at=now_dt.timestamp())
    if facts is None:                           # an item from before the stamps: only the time it carries can be compared
        was = _moment(opp.get('event_start'))
        if was and not e.get('all_day') and was != _moment(e.get('start')):
            marked = _change_marked({'start': opp.get('event_start'), 'end': e.get('end')}, now['facts'])
            return _v('changed', speak(marked, now_dt), now, marked=marked)
        return _v('same', stamp=now)
    marked = _change_marked(facts, now['facts'])
    if marked:
        return _v('changed', speak(marked, now_dt), now, marked=marked)
    return _v('noise', 'only its title changed', now) if base.get('v') != now['v'] else _v('same', stamp=now)


def verdict(key: str, opp: dict, sources, now_dt: datetime, calendar=None) -> dict:
    """Read the item's source again and compare it with what its result was built from.
    -> {'is': 'same' | 'handled' | 'changed' | 'noise' | 'unknown', 'what': plain words, 'stamp': the source now,
        'where': what could not be read}. A failed read is always 'unknown'."""
    kind = source_kind(key, opp)
    if kind == 'mail':
        return _mail_verdict(opp, sources, now_dt.timestamp())
    if kind == 'calendar':
        return _calendar_verdict(opp, sources, now_dt, calendar)
    return _v('same')


def talked(opp: dict, lines: list) -> bool:
    """Did the owner say something in chat that looks like it is about this item (two of its distinctive words in one
    message of the owner's)? Code cannot tell "I already called her" from "call her for me", so it does not try: a hit only
    sends the item back for one fresh look, where Zeus reads the chat. That model's word is bounded by code: once
    per item, and only the usual result comes back (redone, or no longer needed)."""
    ask = ((opp.get('packet') or {}).get('decision') or {}).get('ask') or ''
    words = {w for w in re.findall(r'[a-z0-9]{4,}', f"{opp.get('title') or ''} {ask}".lower())} - _COMMON
    if len(words) < 2:
        return False
    return any(sum(1 for w in words if w in str(text).lower()) >= 2 for text in lines)


# ------------------------------------------------------------------------------------------------ what is done about it
def _update(con, key: str, opp: dict, state_was: str, state=None, surface_at=None) -> bool:
    sets, vals = ['payload=?', 'updated=?'], [json.dumps(opp, default=str), time.time()]
    if state:
        sets.append('state=?')
        vals.append(state)
    if surface_at:
        sets.append('surface_at=?')
        vals.append(surface_at)
    cur = con.execute(f"UPDATE opps SET {', '.join(sets)} WHERE key=? AND state=?", (*vals, key, state_was))
    return cur.rowcount == 1


def _close_children(con, parent_key: str, why: str) -> None:
    for c in con.execute("SELECT key,payload FROM opps WHERE key LIKE ? AND state IN ('pending','executing','ready')", (parent_key + '#fu%',)).fetchall():
        p = json.loads(c['payload'])
        p['closed'] = {'state': 'moot', 'at': time.time(), 'by': 'engine', 'why': why}
        con.execute("UPDATE opps SET state='moot', payload=?, updated=? WHERE key=?", (json.dumps(p, default=str), time.time(), c['key']))


def _retire(opp: dict, why: str, marked=None) -> dict | None:
    """Put the ask the owner was given aside, kept for the record. -> the retired ask, or None when the owner was never asked."""
    p, told = opp.get('packet') or {}, opp.get('told') or {}
    d = p.get('decision') or {}
    if not (told and not told.get('silent') and (d.get('ask') or d.get('ready'))):
        return None
    gone = {'ask': d.get('ask'), 'ready': d.get('ready'), 'default': d.get('default'), 'asked_at': told.get('at'), 'note': told.get('note'),
            'retired_at': time.time(), 'why': why, 'version': (p.get('built_from') or {}).get('v')}
    if marked and marked != why:
        gone['why_marked'] = marked          # the same words with their times as marks: worded again when told (speak)
    f = _f(opp)
    f['retired'] = ((f.get('retired') or []) + [gone])[-5:]
    return gone


def send_back(con, key: str, state_was: str, opp: dict, kind: str, what: str, now_dt: datetime, now_stamp=None, marked=None) -> bool:
    """The result is put aside and the item goes back for fresh work. kind: 'changed' (its source changed), 'chat'
    (the owner may have spoken about it), 'age' (it slept; one fresh look before its note).
    If the owner had already been asked, that ask is retired here: kept for the record, never offered to the owner again.
    Its time becomes now when that time had passed: the fresh work starts on this run, and "an hour overdue" (the
    watchdog's repair ticket) is counted from the redo, not from the day it was first noticed."""
    f = _f(opp)
    gone = _retire(opp, what, marked) if kind == 'changed' else None
    old = opp.pop('packet', None)
    if kind in ('age', 'chat') and old:
        f['earlier_packet'] = old            # if the fresh look cannot run, this result still stands (and says how old it is)
    f['redo'] = {'kind': kind, 'what': what, 'at': time.time(), 'asked': (gone or {}).get('ask') or (gone or {}).get('ready') or '',
                 'stamp': now_stamp, 'earlier': ' '.join(str(x) for x in ((old or {}).get('headline'), (old or {}).get('details')) if x)[:600],
                 'was_built_at': built_at({**opp, 'packet': old or {}})}
    if marked and marked != what:
        f['redo']['what_marked'] = marked
    if gone and gone.get('asked_at'):
        f['redo']['asked_at'] = gone['asked_at']     # when the owner was asked: the sentence that replaces or withdraws it names that time
    if kind == 'changed':
        f['redos'] = int(f.get('redos') or 0) + 1
    elif kind == 'age':
        f['refreshed'] = int(f.get('refreshed') or 0) + 1
    else:
        f['chat_redo'] = True
    for k in ('told', 'ready_at', 'asked_later', 'held_overnight'):
        opp.pop(k, None)
    if kind == 'changed' and isinstance(now_stamp, dict) and now_stamp.get('kind') == 'calendar' and opp.get('event_start'):
        start = (now_stamp.get('facts') or {}).get('start')
        if start and not (now_stamp.get('facts') or {}).get('all_day'):
            opp['event_start'] = start        # the item's own time follows the calendar, so the night rules read the real one
    row = con.execute('SELECT surface_at FROM opps WHERE key=?', (key,)).fetchone()
    due = _moment(row['surface_at']) if row else None
    if not _update(con, key, opp, state_was, state='pending', surface_at=now_dt.isoformat() if due is None or due < now_dt else None):
        return False
    if kind == 'changed':
        _close_children(con, key, 'the item is being redone: its source changed')
    return True


def ended_packet(line: str, kind: str, base=None, at=None) -> dict:
    """A result that is one plain sentence written by code: an ask that ended. It is never shown to the note writer;
    the note carries the sentence word for word (composing, final_text).
    `line` may carry time marks: the note words them as it goes out ("say"). The headline, which other readers see
    (the chat Zeus's list), has full dates instead, so it is true whenever it is read."""
    packet = {'status': 'done', 'headline': speak(line), 'details': '', 'decision': None, 'followups': [], 'learned': [], 'ended': kind,
              'built_from': base or {'kind': 'none', 'v': '-'}, 'built_at': at or time.time()}
    if _MARK in line:
        packet['line_marked'] = line
    return packet


def ended_line(packet: dict, now_dt=None) -> str:
    """The sentence of an ended ask, worded for the moment it goes out."""
    return speak(str(packet.get('line_marked') or packet.get('headline') or ''), now_dt)


def _quotable(ask) -> str:
    """The question the owner was asked, ready to quote back, or '' when it is too long to quote."""
    ask = re.sub(r'\s+', ' ', str(ask or '')).strip().strip('"')
    if not ask or len(ask) > ASK_QUOTED_MAX:
        return ''
    return ask if ask[-1] in '.?!' else ask + '.'


def _asked_mark(at) -> str:
    try:
        return at_mark(float(at), 's') if at else 'earlier'
    except (TypeError, ValueError):
        return 'earlier'


def end_ask(con, key: str, state_was: str, opp: dict, now_dt: datetime) -> bool:
    """An ask the owner never answered, whose own deadline has passed (the owner's "3a"): the ask is retired and the item becomes one
    plain line for the next 9:00am note. After that line it is done."""
    deadline = _moment(opp.get('deadline'))
    gone = _retire(opp, 'its deadline passed') or {}
    when = at_mark(deadline, 'w') if deadline else 'earlier'
    thing = _thing(opp)
    line = (EXPIRED.format(asked=_asked_mark(gone.get('asked_at')), thing=thing, when=when) if thing
            else EXPIRED_NO_ASK.format(asked=_asked_mark(gone.get('asked_at')), when=when))
    opp['packet'] = ended_packet(line, 'expired', None, now_dt.timestamp())
    _f(opp)['ended'] = {'kind': 'expired', 'at': time.time(), 'line': speak(line), 'line_marked': line,
                        'ask': gone.get('ask') or gone.get('ready'), 'asked_at': gone.get('asked_at'), 'deadline': deadline.isoformat() if deadline else None}
    for k in ('told', 'asked_later', 'held_overnight'):
        opp.pop(k, None)
    opp['ready_at'] = time.time()
    nine = __import__('notifier').next_daytime(now_dt)   # lane R: its own line at the next daytime moment (was: the next 9am note)
    if isinstance(opp.get('decision'), dict):
        opp['decision']['surface_at'] = nine.isoformat()
    if not _update(con, key, opp, state_was, state='ready', surface_at=nine.isoformat()):
        return False
    con.execute("UPDATE opps SET urgency='later' WHERE key=?", (key,))
    _close_children(con, key, 'the ask ended: its deadline passed')
    return True


def _close_quietly(con, key: str, state_was: str, opp: dict, why: str) -> bool:
    opp['closed'] = {'state': 'moot', 'at': time.time(), 'by': 'notifier', 'why': why}
    if not _update(con, key, opp, state_was, state='moot'):
        return False
    _close_children(con, key, 'parent closed: the owner already answered')
    return True


def _offer(opp: dict) -> bool:
    return isinstance(opp.get('offer'), dict) and bool(opp['offer'].get('rule_key'))


def tend(con, life, now_dt: datetime) -> dict:
    """Every run, before the work (tick.run_phases). Never sends anything.
    1. An ask already put to the owner whose own deadline has passed ends (end_ask).
    2. Every other ask already put to the owner is compared with its source: the calendar on every run, a mail thread at
       most once an hour between 9am and 9pm (and at once when new mail was merged into it). The owner answered the thread personally: it
       closes quietly. Really changed: the ask is retired and the item goes back for fresh work. Unknown: nothing is
       done now; the ask is checked again at the moment of the owner's yes (before_yes).
       What the owner says in chat is NOT looked at here: the owner's chat is where the owner answers an ask.
    3. A worked result that slept gets one fresh look shortly before its note would go."""
    out, reads, now = {}, 0, now_dt.timestamp()
    calendar = _once(lambda: read_calendar())

    def count(name):
        out[name] = out.get(name, 0) + 1

    rows = con.execute("SELECT key,state,surface_at,urgency,sources,payload FROM opps WHERE state IN ('surfaced','ready') ORDER BY updated").fetchall()
    for r in rows:
        opp = json.loads(r['payload'])
        p = opp.get('packet') or {}
        if _offer(opp) or p.get('ended') or opp.get('approved') or opp.get('closed'):
            continue
        f = _f(opp)
        if r['state'] == 'ready':
            made, quiet = built_at(opp), now_dt.hour >= 21 or now_dt.hour < 9
            if not made or not (opp.get('exec') or {}).get('job_id') or p.get('prep') or f.get('refreshed'):
                continue
            goes = _moment(r['surface_at']) or now_dt
            if quiet and (r['urgency'] != 'now' or now_dt.hour < 9):
                nine = now_dt.replace(hour=9, minute=0, second=0, microsecond=0)
                goes = max(goes, nine if now_dt < nine else nine + timedelta(days=1))
            goes = max(goes, now_dt)
            if (goes - now_dt).total_seconds() <= REFRESH_LEAD and goes.timestamp() - made > FACTS_FRESH_FOR:
                if send_back(con, r['key'], 'ready', opp, 'age', '', now_dt):
                    count('refreshed')
            continue
        if not (p.get('decision') or {}).get('ask') and not (p.get('decision') or {}).get('ready'):
            continue
        deadline = _moment(opp.get('deadline'))
        if deadline and deadline <= now_dt:
            try:
                late = float((opp.get('told') or {}).get('at') or 0) > deadline.timestamp()
            except (TypeError, ValueError):
                late = False
            if late:
                continue                     # the owner first heard it after the deadline, and the note said so (gate): no second line
            if end_ask(con, r['key'], 'surfaced', opp, now_dt):
                count('expired')
            continue
        kind = source_kind(r['key'], opp)
        if kind == 'none' or int(f.get('redos') or 0) >= MAX_REDOS:
            continue
        if kind == 'mail':
            # Outside reads are kept few: once an hour per ask, in the day. At night nothing can reach the owner anyway, the 9:00am
            # run looks first, and the owner's yes is always checked against a fresh read at that moment (before_yes).
            if not 9 <= now_dt.hour < 21 or now - float(f.get('swept_at') or 0) < MAIL_SWEEP_GAP or reads >= MAIL_READS_PER_SWEEP:
                continue
            reads += 1
        v = verdict(r['key'], opp, r['sources'], now_dt, calendar)
        if v['is'] == 'handled':
            count('answered' if _close_quietly(con, r['key'], 'surfaced', opp, v['what']) else 'raced')
            continue
        if v['is'] == 'changed':
            count('retired' if send_back(con, r['key'], 'surfaced', opp, 'changed', v['what'], now_dt, v['stamp'], v.get('marked')) else 'raced')
            continue
        store = kind == 'mail'                     # a mail thread: remember when it was last read
        if store:
            f['swept_at'] = now
        if v['is'] == 'unknown':
            store = store or 'unknown_since' not in f
            f.setdefault('unknown_since', now)
            count('unknown')
        else:
            store = store or 'unknown_since' in f
            f.pop('unknown_since', None)
            if v['is'] == 'noise' and v['stamp']:
                p['built_from'] = v['stamp']      # the version moved, nothing real did: the ask stands and is not weighed again
                store = True
                count('noise')
        if store:                                  # written with the row's own "updated" left alone: nothing happened to the matter
            con.execute("UPDATE opps SET payload=? WHERE key=? AND state='surfaced'", (json.dumps(opp, default=str), r['key']))
    con.commit()
    return out


def gate(con, life, now_dt: datetime, keep: list, out: dict) -> list:
    """notifier.deliver, right before the note is written: every item in it is read again (the module text has the
    table). -> the items that go in this note. An item that waits stays as it is and is counted as held; an item sent
    back for fresh work comes in a later note.
    One text per matter: a changed item is only ever sent back when the change is real; "noise" keeps the held note
    exactly as it is and stores the new version, so the same copy is not weighed twice."""
    if not on(life):
        return keep
    kept, waiting, now = [], [], now_dt.timestamp()
    since = min([built_at(o) or now for _, o in keep] or [now])
    chat = read_chat(since) if keep else []
    calendar = _once(lambda: read_calendar())

    def count(name):
        out[name] = out.get(name, 0) + 1

    def store(r, opp):
        con.execute("UPDATE opps SET payload=? WHERE key=? AND state='ready'", (json.dumps(opp, default=str), r['key']))

    for r, opp in keep:
        f, p = _f(opp), opp.get('packet') or {}
        was = json.dumps(opp, sort_keys=True, default=str)
        f.pop('say', None)
        f.pop('unread', None)
        if p.get('ended'):
            kept.append((r, opp))
            continue
        says, made, wait = [], built_at(opp), False
        stood = at_mark(made, 's') if made else 'when I first saw it'     # worded by final_text as the note goes (speak)
        v = verdict(r['key'], opp, r['sources'], now_dt, calendar)
        if v['is'] == 'handled':
            if _close_quietly(con, r['key'], 'ready', opp, v['what']):
                out['moot'] = out.get('moot', 0) + 1
            continue
        if v['is'] == 'changed':
            if int(f.get('redos') or 0) < MAX_REDOS:
                count('fresh_redo' if send_back(con, r['key'], 'ready', opp, 'changed', v['what'], now_dt, v['stamp'], v.get('marked')) else 'fresh_raced')
                continue
            says.append({'k': 'moving', 'when': stood})
        elif v['is'] == 'unknown':
            first = float(f.setdefault('unknown_since', now))
            f['unread'] = {'where': v['where'] or 'the source', 'when': stood}      # final_text says it, once for the whole note
            wait = r['urgency'] != 'now' and now - first < HOLD_UNKNOWN          # decided after the loop: it waits only when nothing else goes now
        else:
            f.pop('unknown_since', None)
            if v['is'] == 'noise' and v['stamp']:
                p['built_from'] = v['stamp']
                count('fresh_noise')
        routine = r['urgency'] != 'now'      # an urgent item is never delayed for a look at the owner's chat or for its age
        if routine and not f.get('chat_redo') and made and (opp.get('exec') or {}).get('job_id') and not p.get('prep'):
            mine = None if chat is None else [text for at, text in chat if at > made]
            if (mine is None and now - made > CHAT_GAP) or (mine and talked(opp, mine)):
                count('fresh_redo' if send_back(con, r['key'], 'ready', opp, 'chat', 'you said something about it in our chat', now_dt) else 'fresh_raced')
                continue
        if made and now - made > FACTS_FRESH_FOR and (opp.get('exec') or {}).get('job_id') and not p.get('prep'):
            if routine and not f.get('refreshed'):
                count('fresh_redo' if send_back(con, r['key'], 'ready', opp, 'age', '', now_dt) else 'fresh_raced')
                continue
            says.append({'k': 'stood', 'when': stood})
        deadline = _moment(opp.get('deadline'))
        if (p.get('decision') or {}).get('ask') and deadline and deadline <= now_dt:
            # Never dropped: an ask whose deadline fell while it was held (quiet hours) is still told, and says so.
            says.append({'k': 'passed', 'when': at_mark(deadline, 'w')})
        if p.get('changed_since'):
            says.append({'k': 'since', 'what': p.get('changed_since_marked') or p['changed_since']})
        if says:
            f['say'] = says
        if wait:
            waiting.append((r, opp))
            continue
        if f.get('unread'):
            count('fresh_unread_said')
        if json.dumps(opp, sort_keys=True, default=str) != was:
            store(r, opp)
        kept.append((r, opp))
    # An item whose source could not be read waits (up to HOLD_UNKNOWN) for a good read only when nothing else goes
    # now. When a note goes anyway it rides along and says so: held back, it would come alone half an hour later as a
    # second text about the same burst of news.
    for r, opp in waiting:
        if kept:
            count('fresh_unread_said')
            count('fresh_unread_joined')
            store(r, opp)
            kept.append((r, opp))
            continue
        opp['fresh'].pop('say', None)
        opp['fresh'].pop('unread', None)
        store(r, opp)
        out['held'] = out.get('held', 0) + 1
        count('fresh_unread_held')
    con.commit()
    return kept


# ------------------------------------------------------------------------------------------------ the worker's side
def work_began(life, opp: dict, sources, now_dt: datetime) -> None:
    """worker.run, a job was just booked: read the source now and keep its version as what this round of work is
    built from. Read at the START of the work on purpose: a change that lands while the job runs cannot be proven
    seen by the job, so it counts as a change. A failed read is stored as unknown."""
    if not on(life):
        return
    at = now_dt.timestamp()
    try:
        kind = source_kind(str(opp.get('dedupe_key') or ''), opp)
        st = {'kind': 'none', 'v': '-', 'at': at}
        if kind == 'mail':
            ref = _mail_ref(opp, sources)
            st = mail_stamp(ref[0], ref[1], ref[2], read_thread(*ref), at=at) if ref else {'kind': 'mail', 'v': None, 'tail': None, 'at': at}
        elif kind == 'calendar':
            ref, cal = _calendar_ref(opp, sources), read_calendar()
            e = find_event(cal['events'], *ref) if ref and cal else None
            st = calendar_stamp(e, ref[0], str(e.get('start') or '')[:10], at=at) if e else {'kind': 'calendar', 'v': None, 'at': at,
                                                                                           **({'ref': ref[0], 'day': ref[1], 'inst': ref[2]} if ref else {})}
        _f(opp)['began'] = st
        __import__('glue').work_starts(life, opp)   # glue.py: the watch version this work starts from; and which email its reply answers
    except Exception as e:
        _f(opp)['began'] = {'kind': 'none', 'v': None, 'at': at, 'why': f'{type(e).__name__}: {str(e)[:120]}'}


def prompt_extra(opp: dict, now_dt=None) -> str:
    """worker.build_prompt: what a job is told when the item was sent back for fresh work. '' for every other item,
    so their prompt is exactly what it was. Times in it are worded for now_dt, the moment the job is booked."""
    redo = opp['fresh'].get('redo') if isinstance(opp.get('fresh'), dict) else None
    if not isinstance(redo, dict):
        return ''
    earlier = str(redo.get('earlier') or '(none)')
    if redo.get('kind') == 'age':
        return ('You already worked on this earlier and the owner has NOT been told yet. Before the owner hears it, check that every fact in your earlier '
                'result is still true right now, and answer again with the whole result, changed where something changed. '
                f'Your earlier result: {earlier}\n')
    if redo.get('kind') == 'chat':
        return ('Since you worked on this, the owner said something in your chat with the owner that may be about it. Read your recent conversation with '
                'the owner first. If the owner has it in hand or said to leave it, report status "moot". Otherwise answer again with the whole result, '
                f'using what the owner said. Your earlier result: {earlier}\n')
    what = speak(redo.get('what_marked') or redo.get('what') or 'something about it changed', now_dt or datetime.now(PACIFIC))
    text = (f"Its source changed after you last worked on it: {what}. Your earlier result was built "
            f'on how things stood before, so do the work again from the source as it is now. Your earlier result: {earlier}\n')
    if redo.get('asked'):
        text += (f"The owner has ALREADY been asked about it: \"{str(redo['asked'])[:400]}\". That ask is withdrawn, and the owner will be told that your new "
                 'result replaces it. If a step still needs the owner\'s yes, prepare it again in full (status "needs_decision"). If nothing is needed '
                 'from the owner any more, report status "moot", and in "moot_reason" say what changed in a few plain words the owner can read '
                 '(for example "the client cancelled the visit"); code puts them in the one line that tells the owner the earlier ask is off.\n')
    else:
        text += 'The owner has not been told anything about it yet.\n'
    return text


def packet_ready(con, key: str, opp: dict, packet: dict, now_dt: datetime | None = None) -> dict:
    """worker._ready, a result is about to be saved: put the version it was built from on it, and settle a round of
    fresh work.
      The owner had been asked, and now nothing is needed -> one plain line that the earlier ask is off (never a silent
        disappearance of something sitting on the owner's phone).
      The owner had been asked, and there is a new result  -> marked as replacing the earlier ask.
      A fresh look that could not run (no job)      -> the earlier result stands; the note says how old it is.
    -> the packet to save. Trouble in here never loses a result: the packet is then saved as it came, with no stamp,
    and an unstamped result is compared by time, never taken as unchanged (_mail_verdict, _calendar_verdict)."""
    try:
        if not isinstance(packet, dict) or not on(life_of(con)):
            return packet
        return _packet_ready(opp, dict(packet), now_dt)
    except Exception as e:
        if isinstance(opp, dict):
            _f(opp)['packet_error'] = f'{type(e).__name__}: {str(e)[:120]}'
        return packet


def _packet_ready(opp: dict, packet: dict, now_dt) -> dict:
    f = _f(opp)
    now = now_dt.timestamp() if now_dt else time.time()
    redo, began = f.pop('redo', None), f.pop('began', None)
    redo = redo if isinstance(redo, dict) else {}
    worked = not packet.get('prep')                      # a packet with "prep" was built from the item alone: no job ran
    if redo.get('kind') in ('age', 'chat'):
        earlier = f.pop('earlier_packet', None)
        if not worked and isinstance(earlier, dict):
            return earlier
        if isinstance(earlier, dict) and isinstance(earlier.get('replaces'), dict) and packet.get('decision') and not packet.get('replaces'):
            packet['replaces'] = earlier['replaces']     # a fresh look at a new version the owner has not seen yet: it still replaces the owner's earlier ask
    packet['built_from'] = (began if worked else None) or redo.get('stamp') or f.get('seen') or {'kind': 'none', 'v': '-'}
    packet['built_at'] = float((began or {}).get('at') or now) if worked else now
    if redo.get('kind') == 'changed':
        what = redo.get('what_marked') or redo.get('what') or 'it changed'
        if redo.get('asked') and packet.get('status') in ('moot', 'nothing_useful'):
            # What changed, in the job's own words when it gave them (its moot_reason: "Alex cancelled the lunch"). Code
            # only knows "a newer email about it came in", which tells the owner nothing. Code still decides that the ask
            # is off and that the owner hears it once; the job's words only say why.
            # A calendar change is worded by code from the calendar itself ("it is no longer on your calendar"); that stands.
            why = (_reason(packet.get('moot_reason')) if source_kind(str(opp.get('dedupe_key') or ''), opp) != 'calendar' else '') or what
            line = WITHDRAWN.format(asked=_asked_mark(redo.get('asked_at')), what=why)
            f['ended'] = {'kind': 'withdrawn', 'at': time.time(), 'line': speak(line), 'line_marked': line,
                          'why': packet.get('moot_reason') or packet.get('headline'), 'why_marked': why,
                          'ask': redo.get('asked'), 'asked_at': redo.get('asked_at')}
            return ended_packet(line, 'withdrawn', packet['built_from'], now)
        if redo.get('asked'):
            packet['replaces'] = {'ask': redo['asked'], 'what': speak(what), 'what_marked': what, 'asked_at': redo.get('asked_at'),
                                  'by': (redo.get('stamp') or {}).get('kind') if isinstance(redo.get('stamp'), dict) else None}
        if not worked:
            packet['changed_since'] = speak(what)
            packet['changed_since_marked'] = what
    return packet


def _reason(text) -> str:
    """A job's moot_reason as the middle of one sentence to the owner: its first sentence, no closing stop, a small first
    word in lower case ("The lunch was cancelled" -> "the lunch was cancelled"). '' when there is nothing usable."""
    text = re.sub(r'\s+', ' ', str(text or '')).strip().strip('"')
    text = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)[0].rstrip('.!? ').strip()
    text = re.split(r'[,;]\s+(?:so|which means|meaning|and so)\b', text)[0].strip()    # "..., so nothing is needed" is the line's own ending
    if not text or len(text) > 240 or re.match(r'(?:nothing|no |none|not needed|moot|n/?a\b)', text, re.I):
        return ''                    # it says that nothing is needed, not what changed: the line's own ending says that already
    first = text.split(' ', 1)[0]
    if first in ('The', 'It', 'This', 'That', 'There', 'His', 'Her', 'Their', 'Its', 'He', 'She', 'They', 'Both', 'All'):
        text = text[0].lower() + text[1:]
    return text


# ------------------------------------------------------------------------------------------------ the note's side
def composing(con, life, now_dt: datetime, compose, keep: list, out: dict):
    """notifier.deliver: the writer to use for this note. With both switches off: the writer it was given, untouched.
    With fresh-on, a wrapper that hands the writer, inside each item, the facts code owes the owner
    about it ("tell_him": an ask this result replaces, an ask that ended, a source that could not be read again), and
    one short rule to say each once in its own words. A writer that can take them is also handed the check
    (owed_missing) for its own one targeted fix. What is still missing after that, final_text adds as code's sentence.
    When the writer chooses silence while such a fact is owed, the facts (and any real question, as plain facts) go
    instead of letting the silence swallow them.
    The check also catches the assistant naming itself ("<agent name> has ...") for the same one targeted fix; an
    item carrying a fact is never silenced as "nothing changed"; and with links-by-code an
    item's own action link is handed over as "link", with one short rule."""
    if not (on(life) or links_on(life)):
        return compose

    def wrapped(items, when, recent):
        pairs = list(zip(keep, items)) if len(items) == len(keep) else []
        if not pairs:
            return compose(items, when, recent)
        owed = owed_facts(con, keep, now_dt) if on(life) else {}
        shown, linked = [], False
        for (r, o), it in pairs:
            facts = owed.get(r['key']) or []
            if (o.get('packet') or {}).get('ended') and isinstance(it, dict):
                it = {'item': it.get('item'), 'result': 'ended'}    # its headline is code's own sentence: the facts say it instead
            if facts and isinstance(it, dict):
                it = {**it, 'tell_him': [x['tell'] for x in facts]}
            link = action_link(o) if links_on(life) and isinstance(it, dict) else ''
            if link:
                it, linked = {**it, 'link': link}, True
            shown.append(it)
        kw = {}
        rules = (SAY_RULE if any(owed.values()) else '') + (LINK_RULE if linked else '')
        if rules and _takes(compose, 'rules'):
            kw['rules'] = rules
        if on(life) and _takes(compose, 'must_say'):
            kw['must_say'] = lambda text: ([{'quote': str(x['name'])[:120], 'why': MISSING_WHY.format(tell=x['tell'])}
                                            for x in owed_missing(text, owed)]
                                           + [{'quote': q, 'why': THIRD_WHY} for q in dict.fromkeys(third_person(text))])
        result = compose(shown, when, recent, **kw)
        lines = [x['line'] for (r, o) in keep for x in owed.get(r['key']) or [] if x['line']]
        if isinstance(result, dict) and result.get('silent') and lines:
            # (a fact with no code sentence, a job's own failed look, leaves the writer's own silence alone: the vote
            # can no longer silence it, and code has nothing of its own to say about it)
            import composer as C
            asks = [it for it in shown if isinstance(it, dict) and (it.get('decision_for_him') or it.get('urgency') == 'now'
                                                                    or str(it.get('headline') or '').startswith('Reminder:'))]
            single = len(keep) == 1
            out['fallback'] = 'writer chose silence while a fact was owed'
            return {'text': '\n\n'.join(x for x in ([C.fallback_text(asks)] if asks else []) + _join_unread(lines, single) if x), 'silent': False,
                    'violations': [], 'tries': result.get('tries', 1)}
        if isinstance(result, dict) and 'must_say_left' in result and any(owed.values()):
            out['said_by_writer'] = {'owed': sum(len(v) for v in owed.values()), 'fixed': bool(result.get('must_say_fixed')),
                                     'left': result['must_say_left']}
        return result
    return wrapped


def _takes(fn, name: str) -> bool:
    """Can this writer take the keyword `name`? A test's own writer often cannot: then it simply gets the items."""
    import inspect
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in ps or any(p.kind == p.VAR_KEYWORD for p in ps.values())


def scrub_links(text: str, allowed) -> tuple:
    """Take out every link that is not, character for character, one of `allowed`. -> (text, removed, kept).
    A link is anything a phone would make tappable: http(s)://, www., mailto:, or a site name with a path after it.
    A bare site name ("southwest.com") is a name, not a link, and is left. Markdown links and <angle> links become
    plain text first: a text line shows a bare address or nothing.
    When a removed link shares its site with exactly one allowed link, the allowed one takes its place: the writer
    judged a link was needed there, and code supplies the real one."""
    allowed = [a for a in (allowed or []) if a]
    removed, kept = [], []

    def host(u):
        m = re.match(r'(?:https?://)?(?:www\.)?([^/:?#\s]+)', str(u).lower())
        return m.group(1) if m else ''

    def settle(link):
        core = _core(link)
        rest = link[len(core):]
        if core in allowed:
            kept.append(core)
            return core + rest
        removed.append(core)
        same = [a for a in allowed if host(a) and host(a) == host(core)]
        if len(same) == 1:
            kept.append(same[0])
            return same[0] + rest
        return GONE + rest

    def markdown(m):
        label, link = m.group(1).strip(), m.group(2)
        if LINK.fullmatch(label) or label == link:
            return link
        return f'{label} {link}'

    text = _ANGLE.sub(lambda m: m.group(1), _MD_LINK.sub(markdown, str(text or '')))
    text = LINK.sub(lambda m: settle(m.group(0)), text)
    if GONE in text:
        # Tidy only where a link was taken out: the few words that led up to it ("... here: <link>", "... at <link>").
        lead = r'(?:[ \t]*[:,]|[ \t]+(?:at|via|see|visit|link|on))?[ \t]*'
        text = re.sub(r'[ \t]*\(\s*(?:or|and|see|also|at|via|link)?[\s:]*' + GONE + r'\s*\)', '', text)
        text = re.sub(lead + GONE + r'(?=[.,;!?])', '', text)
        text = re.sub(lead + GONE + r'[ \t]*(?=\n|$)', '.', text)
        text = re.sub(r'(?:[ \t]+(?:at|via|see|visit|link|on))?[ \t]*' + GONE, '', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        text = re.sub(r'[ \t]+([.,;!?])', r'\1', text)
        text = re.sub(r'(?m)^[ \t]*\.[ \t]*$\n?', '', text)
        text = re.sub(r'[ \t]+\n', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text, removed, kept


def _where(key: str, opp: dict) -> str:
    """Where an item came from, in the owner's words: 'your calendar', 'the email thread with Example Roofing'. '' for an
    item with no record of its own. Never the item's title."""
    kind = source_kind(key, opp)
    if kind == 'calendar':
        return 'your calendar'
    if kind == 'mail':
        who = (opp.get('fresh') or {}).get('who') if isinstance(opp.get('fresh'), dict) else None
        return f'the email thread with {who}' if who else 'that email thread'
    return ''


def _subject(key: str, opp: dict) -> str:
    return 'that meeting on your calendar' if source_kind(key, opp) == 'calendar' else _where(key, opp)


def _join(words: list) -> str:
    return words[0] if len(words) == 1 else ', '.join(words[:-1]) + ' and ' + words[-1]


def _said(key: str, opp: dict, entry, single: bool) -> str:
    """One of gate's sentences for this item, naming it by where it came from (or "that", alone in the note)."""
    if not isinstance(entry, dict):
        return str(entry or '')
    subject, part = _subject(key, opp), 'that' if single else 'that part'
    at = f' at {subject}' if subject else ''
    if entry.get('k') == 'moving':
        subject = subject or 'that one'
        return MOVING.format(Subject=subject[0].upper() + subject[1:], part=part, when=entry.get('when'))
    if entry.get('k') == 'stood':
        return STOOD.format(at=at, part=part, when=entry.get('when'))
    if entry.get('k') == 'passed':
        return PASSED.format(that='that' if single else f"the question about {subject or 'one of these'}", when=entry.get('when'))
    if entry.get('k') == 'since':
        return SINCE.format(at=at, what=entry.get('what'))
    return ''


# ---- the facts code owes the owner, said by the writer -------------------------------------------------------------------------
# Code's own sentences added after the writer read as robotic, can say a thing twice ("Scratch the Thursday question"
# and then "This replaces what I asked ..."), and miss facts the writer never saw. So each fact is handed to the writer with its item, in plain words ("tell_him"), the writer says it, code
# checks that it did (tolerant word tests, below), the writer gets one targeted fix for what is missing, and only then
# does code add its own sentence. Never both. Code still decides every fact; the writer only words it.
SAY_RULE = ('\n- An item may carry "tell_him": notes, in your own first person, of facts the owner must hear about it. Say each once, plainly, '
            'with that item, in your own sentences: never copy a note as it is. When one says your result replaces an earlier ask, '
            'make that clear. Never quote an earlier ask word for word.')
MISSING_WHY = ('the text never tells the owner this, and the owner needs it: {tell}. Say it once, in a few plain words of your own, with that item. '
               'Never quote an earlier ask word for word.')
LINK_RULE = ('\n- An item may carry "link": the page where the owner does what that item needs from the owner. When your text asks the owner to do it, '
             'put that link on its own line right after that item, exactly as given. Otherwise leave it out.')
THIRD_WHY = ('the text talks about you, the assistant, by your own name. You are writing as yourself: say "I", "me" and "my" '
             '("my Chrome", "I can no longer get in"), never your own name.')
SOURCE_WORDS = {'mail': 'by email', 'calendar': 'on your calendar', 'watch': 'on the page I watch for it'}
_REPLACED = re.compile(r"\b(?:replac\w*|instead|scratch\w*|updat\w*|chang\w*|moved?|moving|new (?:time|date|day|plan|version|price|quote|ask)|"
                       r"no longer|cancel\w*|supersed\w*|scrap\w*|correct\w*|typo|ignore (?:my|the|that|what)|forget (?:my|the|that|what))\b", re.I)
# "not the $5,950 I asked you about yesterday" says it is replaced, with none of the words above.
_EARLIER = re.compile(r"\b(?:i asked(?: you)?|asked you|you asked|i (?:sent|texted) you|my (?:earlier |last |first )?(?:question|ask|text)|"
                      r"(?:the|that) (?:earlier|previous|last|old|first|other) (?:question|ask|version|plan|time|price|quote|one)|"
                      r"\w+'s (?:\w+ )?question|question (?:i|you) (?:asked|got|sent)|question from \w+)\b", re.I)
_CONTRAST = re.compile(r"\b(?:not|no longer|instead|rather than|typo|correct\w*|wrong|mistake|was|were|used to be|from|replac\w*|old|earlier)\b", re.I)
_OFF = re.compile(r"\b(?:off|no longer|cancel\w*|called off|scratch\w*|ignore|moot|not needed|nothing (?:is )?needed|need nothing|never ?mind|"
                  r"drop\w*|withdraw\w*|instead|chang\w*|replac\w*)\b", re.I)
_ENDED_SURE = re.compile(r"\b(?:deadline|passed|expired|ended|closed|too late|missed|lapsed|ran out|run out)\b", re.I)
_ENDED = re.compile(r"\b(?:deadline|passed|expired|ended|closed|too late|missed|lapsed|over|off|ran out|run out|no longer|unanswered|"
                    r"not (?:done|ordered|booked|bought|sent|placed)|(?:did|was|were)(?:n'?t| not) (?:\w+ )?(?:done|ordered|booked|bought|sent|placed|go ahead)|"
                    r"nothing (?:is )?(?:needed|open))\b", re.I)
_CAVEAT = re.compile(r"\b(?:as (?:it|they|that) stood|as of|could(?:n'?t| not)|can(?:'?t|not)|wasn'?t able|was not able|unable|not able|"
                     r"(?:would|did)(?:n'?t| not) (?:open|load|come up)|(?:was|is) down|timed out|(?:did(?:n'?t| not)|not) get through|"
                     r"(?:not|n't) (?:been )?(?:re-?)?check\w*|not re-?read|haven'?t (?:been able|re-?checked|checked)|last I (?:saw|checked|looked)|"
                     r"may have changed|might have changed|(?:keeps?|still|kept) chang\w*|in flux|no fresh look|older)\b", re.I)
_SIGNIN = re.compile(r"\b(?:sign(?:ed)?[- ]?(?:in|out)|log(?:ged)?[- ]?(?:in|out)|sign-in|login)\b", re.I)
_SIGNIN_LOOSE = re.compile(r"\b(?:sign\w*|log(?:ged)?[- ]?(?:in|out)|login|chrome|browser|get (?:back )?in(?:to)?)\b", re.I)
_CLOCK = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s?([ap])\.?m\b', re.I)
_DAYS = re.compile(r'\b(?:yesterday|today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|noon|midnight)\b', re.I)
_ANY = re.compile(r'.', re.S)
_SELF = re.compile(r"\b" + re.escape(str(settings.AGENT_NAME or 'Zeus')) + r"\b")     # the assistant naming itself: "<name> has", "<name> is", "<name>'s"
# A job's own result saying its fresh look did not get through ("the city's permit page could not be opened this morning").
_LOOKS = r"(?:page|site|website|portal|tracker|account|app|status|feed|system|calendar|inbox|listing)"
_FAILED = r"(?:could(?:n'?t| not)|can(?:'?t|not)|would(?:n'?t| not)|did(?:n'?t| not)|was(?:n'?t| not) able to|unable to|failed to)"
_OPENED = r"(?:be\s+)?(?:re-?)?(?:open(?:ed)?|load(?:ed)?|reach(?:ed)?|read|access(?:ed)?|come up|check(?:ed)?)"
_JOB_UNREAD = re.compile(rf"\b{_LOOKS}\b[^.;!?]{{0,80}}?\b{_FAILED}\s+{_OPENED}\b|\b{_FAILED}\s+{_OPENED}\b[^.;!?]{{0,60}}?\b{_LOOKS}\b|"
                         rf"\b{_LOOKS}\b[^.;!?]{{0,40}}?\b(?:was|is) down\b", re.I)
# A link that helps the owner act on the thing itself, and words in the note that ask the owner to do it (links-by-code).
_ACT = re.compile(r"\b(?:sign|submit|pay|book|confirm|register|rsvp|renew|fill (?:it |this |that )?(?:in|out)|complete|check in|reserve|accept|"
                  r"approve|enroll|apply)\b", re.I)
_NOT_ACTION = re.compile(r'unsubscribe|opt-?out|preferences|privacy|/track|pixel|view.?in.?(?:your )?browser|manage.?(?:subscription|email)', re.I)
_TOKEN = re.compile(r'\$\s?\d[\d,]*(?:\.\d+)?|\b\d{1,2}(?::\d{2})?\s?[ap]\.?m\b|\b\d[\d,]*(?:\.\d+)?\b', re.I)


def _thing(opp: dict) -> str:
    """The thing an item is about, in plain words, for code's last-resort sentence: its title without the engine's
    notes in brackets, said to the owner ("your", not "the owner's"). '' when there is nothing usable. Never quoted."""
    t = re.sub(r'\s*\([^)]*\)', '', str(opp.get('title') or '')).strip().rstrip('.:;')
    owner = re.escape(str(settings.OWNER_FIRST or ''))
    if owner and settings.OWNER_FIRST != 'the owner':
        t = re.sub(rf"\b{owner}'s\b", 'your', t)
    t = re.sub(r"\b[Tt]he owner's\b", 'your', re.sub(r'\bhis\b', 'your', re.sub(r'\bhim\b', 'you', t)))
    if not t or len(t) > 70 or len(t.split()) > 9 or re.search(r'[;:?!"]|\b(?:is|are|was|were|has|have|needs?)\b', t):
        return ''                      # a sentence, not the name of a thing: the line then names the time the owner was asked instead
    first = t.split(' ', 1)[0]
    return t[0].lower() + t[1:] if first in ('The', 'A', 'An', 'Your', 'This', 'That') else t


def _item_words(opp: dict, *more) -> set:
    """Distinctive words of an item (its title, its question): one of them in the text says the text is about it."""
    p = opp.get('packet') or {}
    text = ' '.join([str(opp.get('title') or ''), str(((p.get('decision') or {}).get('ask')) or '')] + [str(m or '') for m in more])
    return {w for w in re.findall(r'[a-z0-9]{4,}', re.sub(r"'s\b", '', text.lower()))} - _COMMON - {'earlier', 'asked', 'question'}


def _time_tests(words: str) -> list:
    """The day and clock words in a time as the owner reads it ("yesterday at 5pm") -> regexes that find them in the text,
    tolerant of "5 pm", "5:00pm", "5 p.m."."""
    tests = [re.compile(r'\b' + re.escape(d.lower()) + r'\b', re.I) for d in _DAYS.findall(str(words or ''))]
    for h, m, ap in _CLOCK.findall(str(words or '')):
        mins = r'(?::00)?' if not m or m == '00' else ':' + m
        tests.append(re.compile(r'\b' + h + mins + r'\s?' + ap + r'\.?m\b', re.I))
    return tests


def _tokens(text) -> set:
    """Amounts, clock times and numbers of two digits or more in a text, the same however they are written
    ("$5,950.00" -> "5950", "4:15 pm" -> "4:15pm", "5:00 p.m." -> "5pm")."""
    out = set()
    for m in _TOKEN.findall(str(text or '')):
        t = m.lower().replace('$', '').replace(',', '').replace(' ', '').replace('.m', 'm')
        t = t.replace(':00', '') if t.endswith(('am', 'pm')) else re.sub(r'\.0+$', '', t)
        if t.endswith(('am', 'pm')) or len(re.sub(r'\D', '', t)) >= 2:
            out.add(t)
    return out


def _has(*rxs, when=()):
    """A test of one paragraph: one of the regexes matches and, when times are given, one of them too."""
    return lambda body: any(r.search(body) for r in rxs) and (not when or any(t.search(body) for t in when))


def _fact(key, name, k, tell, line, sure, loose=None, about=(), title='') -> dict:
    """One fact owed to the owner. sure / loose: tests of one paragraph: `sure` says the writer certainly said it
    (no targeted fix needed); `loose` says it may well have (code then never adds its own sentence: a fact said twice
    is worse than one left to the targeted fix). about: words, one of which names the item; title: the item's own
    label, taken out of the paragraph before the tests (an echoed "(changed by her text)" is not the writer saying it)."""
    return {'key': key, 'name': name, 'k': k, 'tell': tell, 'line': line, 'sure': sure, 'loose': loose or sure, 'about': set(about),
            'title': str(title or '')}


def said(fact: dict, text: str, loose: bool = False) -> bool:
    """Did the text say this fact? Judged by content, per paragraph: a word that names the item (when the note carries
    more than one), then the fact's own test. loose=True: might it have (see _fact)."""
    test = fact['loose'] if loose else fact['sure']
    for para in [p for p in re.split(r'\n\s*\n', str(text or '')) if p.strip()] or ['']:
        low = para.lower()
        if fact['about'] and not any(w in low for w in fact['about']):
            continue
        body = re.sub(re.escape(fact['title']), ' ', para, flags=re.I) if len(fact['title']) >= 8 else para
        if test(body):
            return True
    return False


def _replaced_tests(rp: dict, p: dict, what: str, new_time: str) -> tuple:
    """For a replaced ask (the content rule): sure = the paragraph points at the change (a replacement word,
    or the earlier ask itself: "not the $5,950 I asked you about"), carries the new detail (the new time; an amount or
    time in the new result that the old ask did not have; "cancelled" for a cancellation) and, wherever it names a
    detail of the old ask, names it as the old one ("not", "instead", "was"). loose = it points at the change at all."""
    d = p.get('decision') or {}
    old = _tokens(rp.get('ask'))
    now_words = ' '.join(str(x or '') for x in (p.get('headline'), d.get('ask')))
    new_tokens = _tokens(now_words) - old
    old_only = old - _tokens(now_words)
    whens = _time_tests(new_time)
    cancelled = bool(re.search(r'\bcancel\w*|called off\b', ' '.join([what, str(p.get('headline') or '')]), re.I))

    def sure(body):
        if not (_REPLACED.search(body) or _EARLIER.search(body)):
            return False
        mine = _tokens(body)
        if whens:
            if not any(t.search(body) for t in whens):
                return False
        elif cancelled:
            if not _OFF.search(body):
                return False
        elif new_tokens and not (new_tokens & mine):
            return False
        for sentence in re.split(r'(?<=[.!?])\s+', body) if not cancelled else []:
            if old_only & _tokens(sentence) and not (_CONTRAST.search(sentence) or _EARLIER.search(sentence)):
                return False             # the old detail, still put to the owner as if it were open
        return True
    return sure, _has(_REPLACED, _EARLIER)


def owed_facts(con, keep: list, now_dt: datetime) -> dict:
    """-> {item key: [fact]} for the items of one note: everything code knows the owner must hear and the writer would not
    otherwise know. Code decides every one of them from stored facts; the time words are worded for now_dt."""
    single = len(keep) == 1
    out = {}
    for r, o in keep:
        try:
            out[r['key']] = _facts_of(r['key'], o, now_dt, single, len(keep))
        except Exception as e:                    # never a lost note: the old sentences still come from final_text's fallback
            out[r['key']] = []
            _f(o)['facts_error'] = f'{type(e).__name__}: {str(e)[:120]}'
    return out


def _facts_of(key: str, o: dict, now_dt: datetime, single: bool, n: int) -> list:
    """Each `tell` is a note of facts in the assistant's own first person, talking to
    the owner ("my question to you yesterday at 10:15am"), in short pieces split by semicolons: facts to say, never a
    sentence to paste. The `line` is code's own last-resort sentence and is said only when the writer did not."""
    p, f = o.get('packet') or {}, o.get('fresh') if isinstance(o.get('fresh'), dict) else {}
    name, thing, facts = str(o.get('title') or key)[:120], _thing(o), []
    title = str(o.get('title') or '')
    words = _item_words(o) if n > 1 else set()
    part = 'that' if single else 'that part'
    if p.get('ended'):
        e = f.get('ended') if isinstance(f.get('ended'), dict) else {}
        asked = speak(_asked_mark(e.get('asked_at')), now_dt)
        about = f" (it was about: {_quotable(e.get('ask')).rstrip('.')})" if _quotable(e.get('ask')) else ''
        ab = _item_words(o, e.get('ask')) if n > 1 else set()
        if p['ended'] == 'expired':
            when = speak(at_mark(e['deadline'], 'w'), now_dt) if e.get('deadline') else 'earlier'
            facts.append(_fact(key, name, 'expired', f'ended: my question to you {asked}{about}; its deadline passed {when}; I did not do the '
                               'step that needed your yes; nothing is needed from you now',
                               ended_line(p, now_dt), _has(_ENDED_SURE), _has(_ENDED), ab, title))
        else:
            why = speak(e.get('why_marked'), now_dt) if e.get('why_marked') else 'it changed'
            facts.append(_fact(key, name, 'withdrawn', f'off: my question to you {asked}{about}; why: {why}; nothing is needed from you on it now',
                               ended_line(p, now_dt), _has(_OFF), _has(_OFF, _REPLACED, _EARLIER), ab, title))
        return facts
    rp = p.get('replaces') if isinstance(p.get('replaces'), dict) else None
    if rp:
        asked = speak(_asked_mark(rp.get('asked_at')), now_dt)
        what = speak(rp.get('what_marked') or rp.get('what'), now_dt) or 'it changed'
        by = SOURCE_WORDS.get(rp.get('by'))
        about = f" (it was about: {_quotable(rp.get('ask')).rstrip('.')})" if _quotable(rp.get('ask')) else ''
        new_time = what.split(' to ', 1)[1] if rp.get('by') == 'calendar' and ' to ' in what else ''
        sure, loose = _replaced_tests(rp, p, what, new_time)
        facts.append(_fact(key, name, 'replaced', f"replaced: my question to you {asked}{about}; what changed"
                           f"{' (' + by + ')' if by else ''}: {what}; this result is the new version",
                           REPLACES.format(asked=_asked_mark(rp.get('asked_at')), what=rp.get('what_marked') or rp.get('what')),
                           sure, loose, words, title))
    for x in f.get('say') or []:
        if not isinstance(x, dict):
            continue
        when = speak(x.get('when'), now_dt) if x.get('when') else ''
        line = _said(key, o, x, single)
        if x.get('k') == 'moving':
            facts.append(_fact(key, name, 'moving', f"still changing: {_subject(key, o) or 'its source'}; this is as of {_as_of(when)}",
                               line, _has(_CAVEAT), _has(_CAVEAT, *_time_tests(when)), words, title))
        elif x.get('k') == 'stood':
            facts.append(_fact(key, name, 'stood', f'this is as of {_as_of(when)}',
                               line, _has(_CAVEAT), _has(_CAVEAT, *_time_tests(when)), words, title))
        elif x.get('k') == 'passed':
            facts.append(_fact(key, name, 'passed', f'its deadline passed {when}, while this waited to reach you', line,
                               _has(_ENDED, when=_time_tests(when)), _has(_ENDED), words, title))
        elif x.get('k') == 'since':
            what = speak(x.get('what'), now_dt)
            facts.append(_fact(key, name, 'since', f'one change since my first look: {what}', line, _has(_REPLACED), None, words, title))
    if isinstance(f.get('unread'), dict):
        where = _where(key, o) or str(f['unread'].get('where') or 'the source')
        when = speak(f['unread'].get('when'), now_dt)
        facts.append(_fact(key, name, 'unread', f'this is as of {_as_of(when)}',
                           speak(UNREAD.format(where=where, part=part, when=f['unread'].get('when')), now_dt),
                           _has(_CAVEAT), _has(_CAVEAT, *_time_tests(when)), words, title))
        facts[-1]['where'] = where
    if not any(x['k'] in ('moving', 'stood', 'unread') for x in facts) and _job_unread(p):
        # The job's own fresh look did not get through ("the city's page wouldn't open this morning" must not be read
        # as "nothing changed" and silence the note). Unknown is not "nothing": it is
        # owed like the others. No code sentence: the result itself says it, so a fallback would only say it twice.
        facts.append(_fact(key, name, 'job_unread', 'my fresh look at it did not get through this time; it is as of my last good read, '
                           'as the details say', '', _has(_CAVEAT), None, words, title))
    # "No fresh look this time", "could not re-read just now" and "my fresh look did not
    # get through; last good read" are about the assistant's own access, not about the thing. They are not owed to
    # the owner as such: an unreadable source is held and retried (gate, HOLD_UNKNOWN) and logged, not narrated. What the owner is still
    # owed is when the facts are from, said about the thing: "as of 2pm".
    facts = [x for x in facts if x['k'] != 'job_unread']      # it has no time to give; 'stood' and 'unread' stay as a plain "as of" time
    facts += _signin_facts(key, o, p, now_dt)
    return facts


def _as_of(when: str) -> str:
    """'at 7:30am' -> '7:30am', for "as of ...": the rest ("yesterday at 5pm", "Monday at 9am") stays as it is."""
    return when[3:] if str(when).startswith('at ') else (when or 'earlier')


def _job_unread(p: dict) -> bool:
    """The job's own result says a page or account it needed could not be opened this time."""
    if p.get('ended') or p.get('status') not in ('done', 'needs_decision', 'ready'):
        return False
    return bool(_JOB_UNREAD.search(' '.join(str(p.get(k) or '') for k in ('headline', 'details'))))


def _signin_facts(key: str, o: dict, p: dict, now_dt: datetime) -> list:
    """signedin.py's ask: the store and the one step are said; and each held errand's own deadline, and whether the owner
    already okayed it, reach the writer (so it can say, for example, "before 8pm tonight")."""
    if not isinstance(o.get('signin'), dict) or not isinstance(p.get('held'), list):
        return []
    name = str(p.get('store') or (o.get('signin') or {}).get('site') or '')
    if not name:
        return []
    out, held = [], [h for h in p['held'] if isinstance(h, dict) and h.get('title')]
    parts = []
    for h in held:
        bits = []
        if isinstance(h.get('okayed'), dict) and h['okayed'].get('at'):
            bits.append('you okayed it ' + speak(at_mark(h['okayed']['at'], 's'), now_dt))
        elif h.get('okayed') == 'standing':
            bits.append('you gave a standing okay for this kind of thing')
        if h.get('by'):
            bits.append('it must be done before ' + _before(h['by'], now_dt))
        parts.append(h['title'] + (' (' + '; '.join(bits) + ')' if bits else ''))
    tell = (f"I can no longer get into {name} in my own browser; your one step: sign in to {name} once in my Chrome"
            + ('; waiting on it: ' + '; '.join(parts) if parts else ''))
    first = _thing({'title': held[0]['title']}) if held else ''
    out.append(_fact(key, name, 'signin', tell, f"My {name} sign-in has lapsed, so {first or 'what I do there'} is on hold. Open my Chrome, "
                     f"sign in to {name} once, and I'll carry on.", _has(_SIGNIN), _has(_SIGNIN_LOOSE), _name_words(name)))
    for h in held:
        if h.get('by'):
            when = _before(h['by'], now_dt)
            out.append(_fact(key, name, 'signin_by', tell, f"{h['title'][0].upper() + h['title'][1:]} has to be done before {when}.",
                             _has(_ANY, when=_time_tests(when)), None, set()))
    return out


def _name_words(name: str) -> set:
    """The words of a store's name that name it in a text ("Hollyhock" for "Hollyhock Pet Supply")."""
    words = {w for w in re.findall(r'[a-z0-9]{4,}', name.lower())} - {'supply', 'store', 'shop', 'market', 'company', 'online', 'club'}
    return words or {name.lower()}


def _before(value, now_dt: datetime) -> str:
    """'8pm today', 'tomorrow at 8pm', 'Friday at 3pm': the time something must be done before, for now_dt."""
    t = _moment(value)
    if t is None:
        return 'its deadline'
    words = when_words(t, now_dt)
    return f'{_clock(t)} today' if words.startswith('today at ') else words


def owed_missing(text: str, owed: dict, loose: bool = False) -> list:
    """The owed facts the text does not say. A code sentence already in the text counts as said.
    loose=False (the writer's targeted fix): what the text does not certainly say. loose=True (code's own sentence,
    final_text): only what it plainly does not say at all. In doubt, the fix runs and code adds nothing."""
    return [x for facts in owed.values() for x in facts if (not x['line'] or x['line'] not in str(text or '')) and not said(x, text, loose)]


def third_person(text: str) -> list:
    """Where the note names the assistant itself ("<name> has", "<name>'s own Chrome"): it should say "I" and "my"."""
    return [m.group(0) for m in _SELF.finditer(str(text or ''))]


def action_link(opp: dict) -> str:
    """links-by-code: the one stored source link that helps the owner act on this very item, or ''. It must be
    a link really in its source (fresh.links, stored by code), named by the item's own result (the job chose it as
    the place to do it), not an unsubscribe or tracking link, and the result must be about something the owner does there
    (sign, pay, book, confirm, submit ...)."""
    f = opp.get('fresh') if isinstance(opp.get('fresh'), dict) else {}
    stored = [x for x in f.get('links') or [] if isinstance(x, str)]
    if not stored:
        return ''
    p = opp.get('packet') or {}
    d = p.get('decision') or {}
    words = ' '.join(str(x or '') for x in (p.get('headline'), p.get('details'), d.get('ask'), d.get('ready')))
    named = {_core(m.group(0)) for m in LINK.finditer(words)}
    if not _ACT.search(LINK.sub(' ', words)):
        return ''
    return next((x for x in stored if x in named and not _NOT_ACTION.search(x)), '')


def _with_link(text: str, link: str, opp: dict, single: bool) -> str:
    """The note asks the owner to act on this item and leaves its link out: the stored link goes on its own line right after
    that item's paragraph. A note that does not ask the owner to act on it is left as it is."""
    paras = re.split(r'(\n\s*\n)', text)
    words = _item_words(opp)
    for i in range(0, len(paras), 2):
        para = paras[i]
        if (single or any(w in para.lower() for w in words)) and _ACT.search(para):
            paras[i] = para.rstrip() + '\n' + link
            return ''.join(paras)
    return text


def _join_unread(lines: list, single: bool) -> list:
    return [x for x in dict.fromkeys(lines) if x]


def final_text(life, text, keep: list, carried: list, out: dict | None = None, now_dt=None):
    """notifier.deliver, the last step before the note is claimed and sent, on every path (written, rewritten, or
    plain facts): every fact code owes the owner that the text still does not say gets code's own sentence, word for word,
    then the links are checked. -> the text that goes. now_dt: the moment it goes out (every time is worded for it).
    A fact the writer already said is never said again by code; only what is missing is added.
    "Missing" is judged loosely here (owed_missing(loose=True)): code adds its sentence only when nothing in
    the text points at the fact at all. And with links-by-code, an item's own action link (action_link) goes on its own
    line after that item when the text asks the owner to act on it and left the link out."""
    if not text or not (on(life) or links_on(life)):
        return text
    going = [(r, o) for r, o in keep if r['key'] in set(carried)]
    if on(life):
        owed = owed_facts(None, going, now_dt or datetime.now(PACIFIC))
        missing = [x for x in owed_missing(text, owed, loose=True) if x['line']]
        unread = [x for x in missing if x['k'] == 'unread']
        lines = [x['line'] for x in missing if x['k'] != 'unread']
        if len(unread) == 1:
            lines.append(unread[0]['line'])
        elif unread:                                   # one sentence for the note, however many could not be read
            named = list(dict.fromkeys(x['where'] for x in unread if x['where'] != 'that email thread'))
            plain = sum(1 for x in unread if x['where'] == 'that email thread')
            if plain:
                other = ' other' if any('email thread' in w for w in named) else ''
                named.append(f'one{other} email thread' if plain == 1 else f'{plain}{other} email threads')
            lines.append(UNREAD_MANY.format(wheres=_join(named)))
        if out is not None and any(owed.values()):
            out['facts_by_code'] = len(lines)
        for line in lines:
            line = speak(line, now_dt)
            if line and line not in text:
                text = text.rstrip() + '\n\n' + line
        text = speak(text, now_dt)                     # the last word on time: no mark ever reaches the owner
    if links_on(life):
        added = 0
        for _, o in going:
            link = action_link(o)
            if link and link not in text:
                was, text = text, _with_link(text, link, o, len(going) == 1)
                added += text != was
        allowed = [link for _, o in going for link in ((o.get('fresh') or {}).get('links') or [] if isinstance(o.get('fresh'), dict) else [])]
        text, removed, kept = scrub_links(text, allowed)
        if out is not None and (removed or kept):
            out['links'] = {'removed': len(removed), 'kept': len(kept)}
        if out is not None and added:
            out['links_added'] = added
    return text


# ------------------------------------------------------------------------------------------------ the owner's yes (for `life approve`)
def before_yes(con, life, key: str, now_dt: datetime | None = None, said_at: float | None = None) -> dict:
    """Call this when the owner says yes to an item, BEFORE anything is recorded or done (the hook for `life approve`).
    -> {'go': bool, 'status': ..., 'tell_him': plain words for the owner, 'ask': ..., 'ready': ...}
      current    go: the ask the owner is answering is the one on the item, and its source was just read again and is the same
      replaced   stop: the ask the owner is answering was retired after it changed. 'ask' and 'ready' are the NEW version;
                 show the owner that and take the owner's yes to it. The owner's old yes is never applied to the new version
      redoing    stop: it changed and the new version is still being worked; the owner will get it
      ended      stop: that ask ended (its deadline passed, or the thing changed and nothing is needed)
      handled    stop: the owner already answered the thread themselves; the item was closed
      unknown    stop: the source could not be read again just now. Nothing is done blind
      missing    stop: no such item
    said_at: when the owner said it (seconds). A yes said before the new ask reached the owner is a yes to the old one.
    With the switch off this always answers current/go, so approve behaves exactly as before."""
    row = con.execute('SELECT key,state,sources,payload FROM opps WHERE key=?', (key,)).fetchone()
    if row is None:
        return {'go': False, 'status': 'missing', 'tell_him': ''}
    if not on(life):
        return {'go': True, 'status': 'current', 'tell_him': ''}
    now_dt = now_dt or datetime.now(PACIFIC)
    opp = json.loads(row['payload'])
    f, p = _f(opp), opp.get('packet') or {}
    d = p.get('decision') or {}
    last = (f.get('retired') or [None])[-1]
    if isinstance(last, dict) and last.get('why_marked'):
        last = {**last, 'why': speak(last['why_marked'], now_dt)}     # worded now: the chat Zeus may pass it on to the owner as it is

    def new_version(status, lead):
        return {'go': False, 'status': status, 'ask': d.get('ask'), 'ready': d.get('ready'), 'retired': last,
                'tell_him': lead + (f" Here is the new version: {d.get('ask')}" if d.get('ask') else '')}

    if p.get('ended') or f.get('ended'):
        return {'go': False, 'status': 'ended', 'retired': last,
                'tell_him': speak(str((f.get('ended') or {}).get('line_marked') or (f.get('ended') or {}).get('line') or ended_line(p, now_dt)
                                      or 'I am no longer asking about that one.'), now_dt)}
    if row['state'] in ('pending', 'executing') and (f.get('redo') or last):
        redo = f.get('redo') or {}
        what = speak(redo.get('what_marked') or redo.get('what'), now_dt) or (last or {}).get('why') or 'it changed'
        return {'go': False, 'status': 'redoing', 'retired': last,
                'tell_him': f'That one changed after I asked: {what}. I am redoing it and will bring you the new version.'}
    if row['state'] not in ('ready', 'surfaced') or not (d.get('ask') or d.get('ready')):
        return {'go': True, 'status': 'current', 'tell_him': ''}        # nothing of this module's to say: approve answers as before
    told_at = (opp.get('told') or {}).get('at')
    if last and (row['state'] == 'ready' or (said_at is not None and told_at and float(said_at) < float(told_at))):
        return new_version('replaced', f"That one changed after I asked: {last.get('why') or 'it changed'}.")
    v = verdict(key, opp, row['sources'], now_dt)
    if v['is'] == 'handled':
        _close_quietly(con, key, row['state'], opp, v['what'])
        con.commit()
        return {'go': False, 'status': 'handled', 'tell_him': 'You already answered that thread yourself, so I have left it alone.'}
    if v['is'] == 'changed':
        send_back(con, key, row['state'], opp, 'changed', v['what'], now_dt, v['stamp'], v.get('marked'))
        con.commit()
        gone = (f.get('retired') or [None])[-1]
        gone = {**gone, 'why': speak(gone.get('why_marked') or gone.get('why'), now_dt)} if isinstance(gone, dict) else gone
        return {'go': False, 'status': 'redoing', 'retired': gone,
                'tell_him': f"That one just changed: {v['what']}. I am redoing it and will bring you the new version."}
    if v['is'] == 'unknown':
        return {'go': False, 'status': 'unknown',
                'tell_him': f"I could not re-read {v['where'] or 'the source'} just now, so I have not done it yet. I will try again in a few minutes."}
    return {'go': True, 'status': 'current', 'tell_him': '', 'ask': d.get('ask'), 'ready': d.get('ready')}

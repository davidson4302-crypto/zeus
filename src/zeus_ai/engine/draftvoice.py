"""draftvoice: a draft in the owner's name adds nothing the owner did not give.

Why: a draft can grow a "by Friday" the owner never said, or an "I'm so sorry" they never felt. This file is ONE code
check on a draft that a background job wrote in the owner's name:

  check_draft  no date or time, price or amount, promise or commitment, feeling or personal detail that is not in
               the owner's instruction (the item), the thread, or the job's own source facts; stock assistant phrasing is
               flagged the same way. No model is asked: code decides.
  fix_draft    a flagged draft gets ONE remove-only fix and is checked again. What still fails is cut, or, when
               cutting would gut the draft, the draft is left as written and every unsupported part is named
               plainly beside it. It never passes as clean.
  on_packet    the hook at the top of worker._ready, before the step is frozen, so the owner's yes is bound to the checked
               words. The thread is read again (read only) for the check.

Scope: the agent's own writing voice counts as approved, so this module does NOT show a job the owner's earlier
messages, does not sort their mail into "typed by the owner" and "written by an agent", and does not learn from their
edits. The style a job writes in is unchanged. Only the no-new-facts check is here.

Never sends. Never changes what needs the owner's yes.

For a grader or a test, without a live job:
  check_draft(draft, given) -> list of flags (empty = clean)
  fix_draft(draft, given)   -> the remove-only fix and what is left
  Fixed(messages)           -> a thread reader over messages already in hand (nothing is fetched)
"""
from __future__ import annotations

import html
import json
import math
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import writingstyle as WS
import settings

SWITCH = 'draft-voice-on'
MIN_LEFT = 20               # a fixed draft shorter than this (characters) is not a draft any more
READ_SECONDS = 30           # the one thread read for a check (it runs inside the 5-minute tick)
CALL_SECONDS = 25
READ = None                 # tests and replays put a thread reader here; None = the live read-only reader

# The owner's own addresses (settings.OWN_ADDRESSES, from [owner.emails]): which thread messages are theirs.
OWN = frozenset(settings.OWN_ADDRESSES)
READ_ONLY = ('GMAIL_FETCH_MESSAGE_BY_THREAD_ID',)


def on(life) -> bool:
    try:
        return life is not None   # always on when there is a life folder
    except (OSError, TypeError):
        return False


def _ts(value) -> float:
    """Epoch seconds from a number, a datetime or an ISO string. Unreadable: 0."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        t = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def same_mailbox(address) -> str:
    """One spelling per mailbox (the rule of sources.same_mailbox): lower case, no "+tag", Gmail without dots."""
    local, _, domain = str(address or '').strip().lower().partition('@')
    local = local.split('+', 1)[0]
    if domain in ('gmail.com', 'googlemail.com'):
        local, domain = local.replace('.', ''), 'gmail.com'
    return f'{local}@{domain}' if local and domain else ''


_OWN_BOXES = frozenset(same_mailbox(a) for a in OWN)


def _his(address) -> bool:
    return same_mailbox(address) in _OWN_BOXES


# ---- the thread, read only ---------------------------------------------------------------------------------------------

def as_message(m, mailbox: str = '') -> dict:
    """One thread message as {'id', 'thread', 'mailbox', 'at', 'from', 'to', 'subject', 'body', 'mine'}, from the mail
    tool's own shape (messageId, messageText, labelIds ...) or from a dict already in this shape."""
    if not isinstance(m, dict):
        return {}
    if 'body' in m and 'at' in m and 'messageId' not in m:
        out = dict(m)
        out.setdefault('mailbox', mailbox)
    else:
        labels = m.get('labelIds') or []
        if isinstance(labels, str):
            labels = re.findall(r'[A-Z_a-z0-9]+', labels)
        out = {'id': str(m.get('messageId') or m.get('id') or ''), 'thread': str(m.get('threadId') or m.get('thread') or ''),
               'mailbox': m.get('mailbox') or mailbox, 'at': _ts(m.get('messageTimestamp')), 'from': str(m.get('sender') or m.get('from') or ''),
               'to': str(m.get('to') or ''), 'subject': str(m.get('subject') or ''), 'body': str(m.get('messageText') or ''), 'labels': list(labels)}
    out['at'] = _ts(out.get('at'))
    if 'mine' not in out:
        sender = WS.addresses(out.get('from'))
        out['mine'] = 'SENT' in (out.get('labels') or []) or bool(sender and _his(sender[0]))
    return out


class Fixed:
    """A thread reader over messages already in hand (tests, the dev check, a grader). Nothing is fetched."""

    def __init__(self, messages):
        self.msgs = [m for m in (as_message(x) for x in messages or []) if m]

    def thread(self, mailbox, thread_id) -> list:
        return sorted((m for m in self.msgs if m.get('thread') == str(thread_id) and (not mailbox or not m.get('mailbox') or m['mailbox'] == mailbox)),
                      key=lambda m: m['at'])


class LiveMail:
    """One thread of the owner's watched mailboxes through the read-only route the engine already uses (the Composio CLI
    via the intake helpers, the one read action in READ_ONLY), inside one time budget."""

    def __init__(self, seconds: float = READ_SECONDS):
        import sources as S
        self.R = S.R
        self.deadline = time.time() + seconds

    def _call(self, slug: str, args: dict, mailbox: str) -> dict:
        if slug not in READ_ONLY:
            raise ValueError('read only')
        left = self.deadline - time.time()
        if left < 3:
            raise TimeoutError('out of time for mail reads')
        R = self.R
        p = subprocess.run([R.COMPOSIO, 'execute', slug, '-d', json.dumps(args), '--account', R.ACCOUNTS[mailbox]], capture_output=True, text=True,
                           timeout=min(CALL_SECONDS, left), env=R._env(), cwd=str(R.HOME), stdin=subprocess.DEVNULL)
        if p.returncode:
            raise RuntimeError(f'mail read rc {p.returncode}')
        data = json.loads(p.stdout)
        if isinstance(data, dict) and data.get('storedInFile') is True:
            path = Path(str(data.get('outputFilePath') or '')).resolve()
            if not path.is_relative_to(R.COMPOSIO_TMP.resolve()) or not path.is_file():
                raise RuntimeError('unexpected stored output')
            try:
                data = json.loads(path.read_text())
            finally:
                path.unlink(missing_ok=True)          # never leave email text lying in /tmp
        if not isinstance(data, dict) or data.get('successful') is False:
            raise RuntimeError('mail read refused')
        return data.get('data') or {}

    def thread(self, mailbox, thread_id) -> list:
        d = self._call('GMAIL_FETCH_MESSAGE_BY_THREAD_ID', {'user_id': 'me', 'thread_id': str(thread_id)}, mailbox)
        return sorted((as_message(m, mailbox) for m in d.get('messages') or []), key=lambda m: m['at'])


def _reader(read=None):
    return read or READ or LiveMail()


def WS_ZONE():
    from zoneinfo import ZoneInfo
    return ZoneInfo(settings.OWNER_TZ)


# ---- the check: nothing the owner did not give ----------------------------------------------------------------------------

_QUOTES = str.maketrans({'\u2019': "'", '\u2018': "'", '\u201c': '"', '\u201d': '"', '\xa0': ' ', '\u2013': '-'})     # one character for one: positions stay true
_URL = re.compile(r"https?://\S+|www\.\S+|[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_NUM_DATE = re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/]|th\b|st\b|nd\b|rd\b)")
_MONTH_DAY = re.compile(r"\b" + _MONTH + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?::|am\b|pm\b|%))(?:,?\s*(20\d{2}))?", re.I)
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r"\b(?:,?\s*(20\d{2}))?", re.I)
_MONTH_ALONE = re.compile(r"\b(?:in|by|until|till|since|this|next|last|early|mid|late|end of|beginning of|start of|before|after|through)\s+"
                          r"(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.I)
_TIME_RANGE = re.compile(r"(?<![\d:.$])(\d{1,2})(?::(\d{2}))?\s?(?:-|\u2013|to)\s?(\d{1,2})(?::(\d{2}))?\s?(a\.m\.|p\.m\.|am|pm)(?![a-z])", re.I)
_TIME = re.compile(r"(?<![\d:.$])(\d{1,2})(?::(\d{2}))?\s?(a\.m\.|p\.m\.|am|pm)(?![a-z])|(?<![\d:.$])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])", re.I)
_NOON = re.compile(r"\b(noon|midday|midnight)\b", re.I)
_WEEKDAY = re.compile(r"\b(?:(?:this|next|last|coming)\s+)?(mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)days?\b", re.I)
_WEEKDAY_SHORT = re.compile(r"\b(Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun)\b\.?(?=,?\s+(?:\d|" + _MONTH + r"|at\b|morning|afternoon|evening|night))")
_WD = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
_REL = [
    ('rel:0', re.compile(r"\b(?:later |earlier )?(?:today|tonight|tonite)\b|\bthis (?:morning|afternoon|evening)\b|\b(?:by |before )?(?:the )?(?:end|close) of (?:the |business )?(?:day|business)\b|\b(?:eod|cob)\b", re.I)),
    ('rel:1', re.compile(r"\b(?:tomorrow|tmrw|tmw|tomorow)(?: (?:morning|afternoon|evening|night))?\b", re.I)),
    ('rel:-1', re.compile(r"\byesterday\b|\blast night\b", re.I)),
    ('rel:week', re.compile(r"\b(?:later |early |earlier )?this week\b|\b(?:the )?end of (?:the |this )?week\b|\beow\b|\bthis weekend\b|\bover the weekend\b|\bthe weekend\b", re.I)),
    ('rel:nextweek', re.compile(r"\b(?:early |later |sometime )?next week\b|\bthe following week\b|\bnext weekend\b", re.I)),
    ('rel:lastweek', re.compile(r"\blast week(?:end)?\b", re.I)),
    ('rel:month', re.compile(r"\bthis month\b|\b(?:the )?end of (?:the |this )?month\b", re.I)),
    ('rel:nextmonth', re.compile(r"\bnext month\b", re.I)),
    ('rel:year', re.compile(r"\b(?:this|next|last) (?:year|quarter)\b|\b(?:the )?end of (?:the |this )?(?:year|quarter)\b", re.I)),
]
_NUMWORD = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'couple': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
            'eleven': 11, 'twelve': 12, 'dozen': 12, 'fifteen': 15, 'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'ninety': 90,
            'hundred': 100, 'thousand': 1000, 'half': 0.5}
_NW = r"(?:a couple(?: of)?|couple(?: of)?|a few|few|several|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|sixty|ninety|\d+(?:\.\d+)?)"
_SPAN = re.compile(r"\b(" + _NW + r")\s*(?:-|to|or)?\s*(?:(?:\d+|[a-z]+teen|twenty|thirty|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+)?(?:more |full |business |working |extra |additional )?"
                   r"(minutes?|mins?|hours?|hrs?|days?|nights?|weeks?|months?|years?|quarters?)\b", re.I)
_VAGUE = re.compile(r"\b(asap|as soon as (?:possible|i can)|right away|straight away|immediately|shortly|soon|momentarily|promptly|first thing|in a bit|"
                    r"in a moment|in a sec(?:ond)?|in no time|right now|at once)\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])([$\u20ac\xa3])?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(k\b|m\b|%|percent\b|x\b|(?:st|nd|rd|th)\b|grand\b|(?:dollars?|bucks|usd)\b)?", re.I)
_NUM_WORDS = re.compile(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen|fifteen|twenty|thirty|forty|fifty|sixty|ninety|hundred|thousand|half)\b"
                        r"(?:\s+(?:dollars?|bucks|grand|percent)\b|(?=\s+(?:\w+\s+)?(?:\w+s\b|off|price|hundred|thousand|dozen)))", re.I)
_MONEY_WORDS = re.compile(r"\b(?:free of charge|no charge|at no (?:extra |additional )?cost|on the house|half (?:off|price)|discount(?:ed|s)?|refund(?:ed|s)?|"
                          r"waiv(?:e|ed|ing)|reimburs(?:e|ed|ing|ement)|my treat|on me\b|comp(?:ed|ing)? (?:it|you|that)|no cost)", re.I)
_LIST_MARK = re.compile(r"(?m)^\s*(\d{1,2})[.)]\s")


def _plain(text) -> str:
    """Any text (mail HTML included) as plain words, quoted parts kept: what the given side is read from."""
    t = str(text or '')
    if re.search(r'(?i)<(?:div|p|br|blockquote|html|body|span|table)\b', t):
        t = re.sub(r'(?is)<!--.*?-->|<(style|script|head)\b.*?</\1>', ' ', t)
        t = re.sub(r'(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|blockquote)>', '\n', t)
        t = re.sub(r'(?s)<[^>]+>', ' ', t)
    return html.unescape(t).translate(_QUOTES)


def _minutes(hour, minute, half) -> list:
    h, m = int(hour), int(minute or 0)
    half = (half or '').lower().replace('.', '')
    if half == 'am':
        return [(0 if h == 12 else h) * 60 + m]
    if half == 'pm':
        return [(12 if h == 12 else h + 12) * 60 + m]
    return [h * 60 + m] + ([h * 60 + m + 720] if h < 12 else [])       # "10:30" with no am or pm could be either


def _md(month, day):
    month, day = int(month), int(day)
    return f'md:{month}-{day}' if 1 <= month <= 12 and 1 <= day <= 31 else None


def _n(value) -> str:
    v = float(value)
    return 'n:' + (str(int(v)) if v == int(v) else ('%f' % v).rstrip('0'))


def _num(word: str):
    word = word.lower().strip()
    if re.fullmatch(r'\d+(?:\.\d+)?', word):
        return float(word)
    if word.startswith(('a couple', 'couple')):
        return 2
    if word in ('a few', 'few', 'several'):
        return 'few'
    return _NUMWORD.get(word)


def facts(text: str) -> list:
    """Every date, time and amount in a text: [(keys, start, end, kind, the words)]. `keys` holds every reading of the
    same thing ("10:30" may be morning or evening); two texts agree on a fact when they share one key. kind is
    'date', 'time', 'vague' (soon, right away) or 'amount'. Pure, no clock."""
    t = str(text or '').translate(_QUOTES)
    buf = list(t)
    out = []

    def mask(s, e):
        buf[s:e] = ' ' * (e - s)

    def sweep(rx, fn, kind):
        for m in rx.finditer(''.join(buf)):
            keys = [k for k in (fn(m) or []) if k]
            if keys:
                out.append((tuple(keys), m.start(), m.end(), kind, t[m.start():m.end()]))
                mask(m.start(), m.end())

    for m in _URL.finditer(t):
        mask(m.start(), m.end())
    for m in _LIST_MARK.finditer(''.join(buf)):
        mask(m.start(1), m.end(1))
    sweep(_ISO_DATE, lambda m: [_md(m.group(2), m.group(3))], 'date')
    sweep(_NUM_DATE, lambda m: [_md(m.group(1), m.group(2))], 'date')
    sweep(_MONTH_DAY, lambda m: [_md(_MONTHS[m.group(1)[:3].lower()], m.group(2))], 'date')
    sweep(_DAY_MONTH, lambda m: [_md(_MONTHS[m.group(2)[:3].lower()], m.group(1))], 'date')
    sweep(_TIME_RANGE, lambda m: ['t:%d' % x for x in _minutes(m.group(1), m.group(2), m.group(5)) + _minutes(m.group(3), m.group(4), m.group(5))], 'time')
    sweep(_TIME, lambda m: ['t:%d' % x for x in (_minutes(m.group(1), m.group(2), m.group(3)) if m.group(1) else _minutes(m.group(4), m.group(5), ''))], 'time')
    sweep(_NOON, lambda m: ['t:%d' % (0 if m.group(1).lower() == 'midnight' else 720)], 'time')
    sweep(_WEEKDAY, lambda m: ['wd:%d' % _WD[m.group(1)[:3].lower()]], 'date')
    sweep(_WEEKDAY_SHORT, lambda m: ['wd:%d' % _WD[m.group(1)[:3].lower()]], 'date')
    for key, rx in _REL:
        sweep(rx, lambda m, key=key: [key], 'date')
    sweep(_MONTH_ALONE, lambda m: ['mon:%d' % _MONTHS[m.group(1)[:3].lower()]], 'date')

    def span(m):
        n = _num(m.group(1))
        unit = re.sub(r's$', '', m.group(2).lower())
        unit = {'min': 'minute', 'hr': 'hour', 'night': 'day'}.get(unit, unit)
        return None if n is None else ['span:%s:%s' % (n if n == 'few' else _n(n)[2:], unit)]
    sweep(_SPAN, span, 'time')
    sweep(_VAGUE, lambda m: ['vague:' + m.group(1).lower().split()[-1]], 'vague')

    def number(m):
        v = float(m.group(2).replace(',', '') + (m.group(3) or ''))
        if (m.group(4) or '').lower() in ('k', 'grand'):
            v *= 1000
        elif (m.group(4) or '').lower() == 'm':
            v *= 1000000
        return [_n(v)]
    sweep(_NUMBER, number, 'amount')
    sweep(_NUM_WORDS, lambda m: [_n(_NUMWORD[m.group(1).lower()])], 'amount')
    return sorted(out, key=lambda f: f[1])


_STOP = frozenset("""a an the and or but so if then than that this these those it its is are was were be been being am do does did done doing have has had having
    i me my mine we us our ours you your yours u ur he she they them their his her him who whom which what when where why how
    to of in on at by for with from into onto over under about around as up out off down back again away there here now just also too very really
    will would shall should can could may might must wont cant ill well ive im id youll not no yes ok okay please thanks thank
    one all any some each every both either more most much many few lot lots bit thing things stuff way ways
    going gonna let lets able happy glad sure know think hope need want like get got getting
    dont cant wont didnt isnt arent wasnt doesnt couldnt wouldnt shouldnt havent hasnt thats whats theres heres""".split())
_AUX = frozenset('promise guarantee commit plan intend aim expect willing ready owe'.split())
_FOLD = {'forward': 'send', 'shoot': 'send', 'email': 'send', 'mail': 'send', 'deliver': 'send', 'share': 'send', 'pass': 'send', 'provide': 'send',
         'drop': 'send', 'bring': 'send', 'hand': 'send', 'sent': 'send', 'phone': 'call', 'ring': 'call', 'reimburse': 'pay', 'paid': 'pay', 'cover': 'pay',
         'reply': 'respond', 'answer': 'respond', 'look': 'check', 'review': 'check', 'schedule': 'book', 'reserve': 'book', 'repair': 'fix', 'resolve': 'fix',
         'complete': 'finish', 'wrap': 'finish', 'sorry': 'sorry', 'apologize': 'sorry', 'apologise': 'sorry', 'apology': 'sorry', 'apologie': 'sorry',
         'grateful': 'thank', 'thankful': 'thank', 'appreciate': 'thank', 'thrilled': 'excit', 'stoked': 'excit', 'pumped': 'excit', 'psyched': 'excit',
         'excited': 'excit', 'exciting': 'excit', 'excite': 'excit', 'glad': 'happi', 'happy': 'happi', 'pleased': 'happi', 'delighted': 'happi', 'adore': 'love',
         'loved': 'love', 'loving': 'love', 'enjoy': 'love', 'fan': 'love', 'concerned': 'worri', 'worried': 'worri', 'worry': 'worri', 'nervous': 'worri', 'anxious': 'worri',
         'annoyed': 'frustrat', 'angry': 'frustrat', 'upset': 'frustrat', 'furious': 'frustrat', 'frustrated': 'frustrat', 'frustrating': 'frustrat',
         'swamped': 'busi', 'slammed': 'busi', 'busy': 'busi', 'hectic': 'busi', 'buried': 'busi', 'kids': 'kid', 'children': 'kid', 'child': 'kid', 'son': 'kid',
         'daughter': 'kid', 'mother': 'mom', 'father': 'parent', 'parent': 'parent', 'ill': 'sick', 'travelling': 'travel', 'traveling': 'travel', 'trip': 'travel',
         'apologizes': 'sorry', 'apologized': 'sorry', 'apologises': 'sorry', 'apologised': 'sorry', 'admiration': 'love', 'admire': 'love',
         'admires': 'love', 'likes': 'love', 'liked': 'love', 'praise': 'love', 'praises': 'love', 'praised': 'love', 'dissatisfied': 'happi',
         'satisfied': 'happi', 'unhappy': 'happi', 'demonstrate': 'show', 'demo': 'show', 'prepare': 'ready', 'prepared': 'ready', 'together': 'ready', 'watch': 'follow', 'followed': 'follow', 'following': 'follow', 'follows': 'follow', 'congratulate': 'congrat',
         'congratulated': 'congrat', 'congratulating': 'congrat', 'congratulates': 'congrat', 'congratulatory': 'congrat', 'kudos': 'congrat',
         'congratulations': 'congrat', 'congrats': 'congrat', 'tomorrow': 'tomorrow', 'confirm': 'confirm', 'confirming': 'confirm', 'confirmed': 'confirm'}


def _stem(word: str) -> str:
    w = word.lower().strip("'")
    w = re.sub(r"(?:n't|'(?:s|d|ll|ve|re|m|t))$", '', w)
    if w in _FOLD:
        return _FOLD[w]
    for end, least in (('ies', 5), ('ing', 6), ('ed', 5), ('es', 5), ('s', 4)):
        if w.endswith(end) and len(w) >= least and not w.endswith('ss'):
            w = w[:-len(end)] + ('y' if end == 'ies' else '')
            break
    return _FOLD.get(w, w)


def _words(text: str) -> list:
    return re.findall(r"[a-z][a-z']*|\d+", str(text or '').lower().translate(_QUOTES))


class Given:
    """Everything a draft may lean on: the owner's instruction, the thread, the job's own source facts. Built once per check."""

    def __init__(self, given):
        self.parts = {'instruction': [], 'mine': [], 'thread': [], 'facts': []}
        self.now = None
        self.flat = not isinstance(given, dict)
        if isinstance(given, dict):
            self.now = _ts(given.get('now')) or None
            for key, part in (('instruction', 'instruction'), ('his_words', 'instruction'), ('item', 'instruction'), ('thread', 'thread'),
                              ('facts', 'facts'), ('sources', 'facts')):
                self._spread(given.get(key), part)
        else:
            self._spread(given, 'instruction')
        self.text = '\n'.join(sum((self.parts[k] for k in ('instruction', 'mine', 'thread', 'facts')), [])).lower()
        self.own = '\n'.join(sum((self.parts[k] for k in ('instruction', 'mine', 'facts')), [])).lower() if not self.flat else self.text
        self.stems = {_stem(w) for w in _words(self.text)}
        self.pre = {w[:6] for w in self.stems | set(_words(self.text)) if len(w) >= 7}
        self.conditional = bool(_IF_GIVEN.search(self.text))
        self.keys = set()
        for keys, _s, _e, _kind, _shown in facts(self.text):
            self.keys.update(keys)
        self.keys.update(_n(float(d)) for d in re.findall(r'(?<![\w.])\d+(?:\.\d+)?', self.text.replace(',', '')) if len(d) < 12)
        self.keys.update(_n(v) for w, v in _NUMWORD.items() if w not in ('a', 'an') and re.search(r'\b%s\b' % w, self.text))
        if self.now:
            # "tomorrow", "Friday" and "Nov 20" are one day when the clock is known: a draft may use any of the three.
            day0 = datetime.fromtimestamp(self.now, WS_ZONE())
            for step, rel in ((0, 'rel:0'), (1, 'rel:1'), (-1, 'rel:-1')):
                d = day0 + timedelta(days=step)
                same = {rel, 'wd:%d' % d.weekday(), 'md:%d-%d' % (d.month, d.day)}
                if same & self.keys:
                    self.keys |= same

    def _spread(self, value, part):
        if value is None or value == '':
            return
        if isinstance(value, (list, tuple)):
            for v in value:
                self._spread(v, part)
        elif isinstance(value, dict):
            text = value.get('text') or value.get('body') or value.get('preview')
            if text is not None and part == 'thread':
                mine = value.get('mine') or value.get('from_him') or value.get('from_owner')
                self.parts['mine' if mine else part].append(_plain(text))
            else:
                self.parts[part].append(_plain(json.dumps(value, ensure_ascii=False, default=str)))
        else:
            self.parts[part].append(_plain(value))

    def has(self, *words) -> bool:
        return all(self.knows(_stem(w)) for w in words)

    def knows(self, stem: str) -> bool:
        """Is this word (already stemmed) in what the owner gave? The same word, or a word of the same family (the first six
        letters of words seven letters or longer: congratulate / congrats, apologize / apology, prepared / preparing)."""
        return stem in self.stems or (len(stem) >= 7 and stem[:6] in self.pre)


# A first-person promise, offer, plan or refusal.
_COMMIT = re.compile(
    r"(?<![a-z'])(?:i|we)(?:'ll|'d be (?:happy|glad)| will| shall| won't| will not| can(?:'t|not)?| could| would be (?:happy|glad)| am going to|'m going to"
    r"| are going to|'re going to|'m gonna| am gonna| gonna| promise| guarantee| commit| plan to| plan on| intend to| aim to| expect to| should be able to"
    r"| am (?:happy|glad|willing|able|ready) to|'m (?:happy|glad|willing|able|ready|down) to| owe you|(?: am|'m| are|'re) (?:sending|bringing|booking|shipping|getting|paying|covering|handling|emailing|calling|coming|dropping|delivering|refunding|waiving|putting))(?![a-z'])"
    r"|(?:^|[,;:.!?]\s*|\band\s+|\bbut\s+|\bso\s+|\bthen\s+)(?:will|can|could|gonna|going to|happy to|glad to|more than happy to|planning to|sending|bringing|booking)\b"
    r"(?!\s+(?:you|u|i|we|he|she|they|it|this|that|someone|anyone|somebody|anybody|there|be\b))"
    r"|\b(?:happy|glad|more than happy|willing) to\b|\blet me\b(?!\s+know)|\byou(?:'ll| will) (?:have|get|receive|hear|see)\b"
    r"|\b(?:count on (?:me|it|us)|rest assured|consider it done|will do\b|on it\b|you got it|count me in|i'm in\b|we're in\b|it's a deal|sounds like a plan"
    r"|see you (?:at|on|in)\b|i've got (?:it|this|you)|i got you)", re.I)
_IDIOMS = [(re.compile(r"\bget(?:ting)? back to\b|\bcircl(?:e|ing) back\b", re.I), re.compile(r"\bget(?:ting|s)? back\b|\bcircl(?:e|ing) back\b|\brespond|\brepl(?:y|ies)\b|\banswer", re.I)),
           (re.compile(r"\bfollow(?:ing)? up\b|\bfollow-up\b", re.I), re.compile(r"\bfollow(?:ing|s|ed)?[ -]?up\b", re.I)),
           (re.compile(r"\bkeep you (?:posted|updated|in the loop)\b|\blet you know\b|\bbe in touch\b|\btouch base\b", re.I),
            re.compile(r"\bkeep (?:you|him|her|them) (?:posted|updated)\b|\blet (?:you|him|her|them) know\b|\bin touch\b|\btouch base\b|\bupdate (?:you|him|her|them)\b", re.I)),
           (re.compile(r"\blook(?:ing)? into\b|\btake care of\b|\bmake sure\b|\bsort (?:it|this|that) out\b", re.I),
            re.compile(r"\blook(?:ing|s)? into\b|\btake care of\b|\bmake sure\b|\bsort\b|\bensur|\bpromis", re.I))]
_FIRST_PERSON = re.compile(r"\b(?:i|we|i'll|we'll|i'm|i'd|i've|me|my|us|let me)\b|^(?:will|following|getting|circling|looking|happy|glad)\b", re.I)
_ACCEPT = re.compile(r"\b(?:yes|yep|yeah|agree[sd]?|offer(?:s|ed|ing)?|willing|happy to|glad to|accept(?:s|ed)?|confirm(?:s|ed)?|approve[sd]?|he(?:'s| is) in\b|i(?:'m| am) in\b|will do|say yes|go ahead|works for (?:him|me)|"
                     r"sounds good|is fine|are fine|okay with|ok with|fine with|on board|deal\b)", re.I)
_FEEL = re.compile(
    r"\b(sorry|apolog(?:ize|ise|ies|y)|regret(?:s|ted)?|excited|exciting|thrilled|delighted|stoked|psyched|heartbroken|devastated|grateful|thankful|honou?red|"
    r"humbled|relieved|embarrassed|ashamed|frustrated|frustrating|disappointed|disappointing|worried|nervous|proud|overwhelmed|exhausted|stressed|annoyed|furious|"
    r"bummed|gutted|congrat(?:s|ulations)|condolences|sympathies)\b"
    r"|\b(?:i'm|i am|i was|i feel|i felt|we're|we are|so|really|very|truly|deeply|genuinely|incredibly|super|pretty)\s+(hurt|touched|moved|tired|concerned|pleased|"
    r"pumped|blessed|anxious|sad|upset|angry|glad(?!\s+to\b)|happy(?!\s+to\b)|pleased(?!\s+to\b))\b"
    r"|\b(glad|happy)\s+(?:you|that|for|with|about)\b"
    r"|\b(?:i|we)\s+(?:really |truly |absolutely |just |do |so )?(love|adore|hate|enjoy|loved|enjoyed|admire)\b|^\W*(love|loved|loving)\s+(?:the|this|that|your|it|how)\b"
    r"|\bmiss(?:ed|ing)? (?:you|y'?all|u)\b|\bcan(?:'t|not) wait\b|\bmeans? (?:a lot|the world|so much)\b|\bthinking (?:of|about) you\b|\bget well\b|\bfeel better\b"
    r"|\bhappy (?:birthday|anniversary|holidays)\b|\bproud of you\b|\b(?:big|huge) fan\b|\bthoughts and prayers\b|\bmy (?:heart|condolences)\b", re.I | re.M)
_PERSONAL = re.compile(
    r"\b(?:my|our)\s+(wife|husband|son|daughter|kids?|children|family|mom|mother|d[a]d|father|brother|sister|girlfriend|boyfriend|partner|fianc[e\xe9]e?|baby|dog|"
    r"parents|grandm\w+|grandf\w+|grandp\w+)\b"
    r"|\b(sick|flu|covid|surgery|hospital|migraine|funeral|wedding|honeymoon|vacation|newborn|pregnan\w+|jet ?lagged|swamped|slammed|hectic)\b"
    r"|\b(?:been|being|was|am|i'm|so|super|crazy|really|pretty)\s+(busy|ill)\b|\bunder the (weather)\b|\bpassed (away)\b|\bout of (town|the office|office|pocket)\b"
    r"|\bon the (road)\b|\bon (?:a|my) (?:work |business |family )?(trip)\b|\b(travell?ing)\b|\b(?:long|rough|crazy|busy) (week|month|day)\b|\btied (up)\b"
    r"|\bdoctor'?s? (appointment)\b|\bback-to-back (meetings)\b", re.I)
# A feeling said without a feeling word ("made my week", "blew me away"), and the owner's own history with the person or their
# product ("last time I was in Denver I bought your light", "I've been using your lights for years"). Backed only when
# most of that sentence's own words are in what the owner gave.
_EFFECT = re.compile(
    r"\bmade (?:my|our) (?:day|week|month|year|morning|night|weekend)\b|\bmade me (?:smile|laugh|cry|happy|so happy|proud)\b|\bput a smile on\b"
    r"|\bblew (?:me|us) away\b|\bblown away\b|\bwarm(?:s|ed)? (?:my|our) hearts?\b|\bbrighten(?:s|ed)? (?:my|our)\b|\bmeant (?:a lot|the world|so much)\b"
    r"|\bhit (?:me )?(?:close to )?home\b|\bstopped me cold\b|\bnear and dear\b|\bclose to (?:my|our) hearts?\b|\bbrought back (?:\w+ )?memories\b"
    r"|\btakes? me back\b|\b(?:i|we) (?:was|were|am|are|'m) (?:\w+ )?(?:floored|touched|moved|amazed|stunned|speechless|impressed|inspired|in awe)\b"
    r"|\bi can relate\b|\bwhat a (?:treat|joy|relief|pleasure)\b|\bkeeps? me up at night\b", re.I)
_STORY = re.compile(
    r"\blast (?:time|year|summer|spring|fall|winter|month|week) (?:i|we)\b|\b(?:back when|years ago|a while back|a few years (?:back|ago)|the first time|ever since) (?:i|we)\b"
    r"|\bwhen (?:i|we) (?:was|were|first|started|lived|worked|grew)\b|\b(?:i|we)(?:'ve| have) (?:been (?:using|buying|following|watching|reading|a (?:fan|customer|client|subscriber))"
    r"|used|bought|known|followed|worked with|ordered from|loved)\b|\b(?:i|we) used to\b|\b(?:i|we) (?:grew up|met (?:at|in|on)|bought|visited|stayed|ordered|went to|was in|were in)\b"
    r"|\bmy first\b|\bas a (?:long-?time|loyal|repeat|happy) (?:customer|client|fan|user)\b|\bsince (?:i|we) (?:started|first)\b|\btook (?:great |good )?care of (?:me|us)\b"
    r"|\b(?:got|helped) (?:me|us) through\b|\bwe(?:'ve| have) known each other\b", re.I)
_IF_GIVEN = re.compile(r"\b(?:if|once|when|as soon as|provided|after)\b[^.!?\n]{2,120}\b(?:can|could|will|would|'ll|then)\b", re.I)
_IF_DRAFT = re.compile(r"^\W*(?:if|once|when|as soon as)\b|\band (?:i|we)(?:'ll| will| can| could)\b|,\s*(?:and )?(?:i|we)(?:'ll| will| can| could)\b", re.I)

# Stock assistant phrasing ("and the like"). Each entry: a pattern.
_STOCK = [re.compile(p, re.I) for p in (
    r"\b(?:i )?(?:hope|trust) (?:this|that|my) (?:e-?mail|message|note|letter)?\s*(?:finds|reaches) you\b[^.!?\n]{0,30}",
    r"\bi hope (?:you(?:'re| are)|all is|everything is|you've been) (?:doing )?(?:well|great|good|fantastic)\b[^.!?\n]{0,30}",
    r"\bi hope you(?:'re| are) having (?:a|an) [a-z]+ (?:week|day|weekend|start)\b[^.!?\n]{0,20}",
    r"\bi hope you had (?:a|an) [a-z]+ (?:week|weekend|holiday|break)\b",
    r"\bi (?:just )?wanted to (?:reach out|touch base|check in|circle back|follow up|drop you a (?:quick )?(?:note|line))\b",
    r"\bi(?:'m| am) (?:reaching out|writing to (?:you )?(?:today )?to|writing to let you know)\b",
    r"\b(?:just )?(?:reaching out|touching base|circling back)\b",
    r"\b(?:it(?:'s| is)|this is|that(?:'s| is)|this isn't|it isn't) not (?:just |only |simply |merely |about )?[^.,;!?\n]{2,60}[,;:\u2014-]+\s*(?:it(?:'s| is)|but|rather)\b",
    r"\bisn't (?:just |only |simply |about )?[^.,;!?\n]{2,50}[,;:\u2014-]\s*it(?:'s| is)\b",
    r"\bnot (?:just|only)\b[^.!?\n]{2,80}\bbut (?:also)?\b",
    r"\bplease (?:do not|don't) hesitate to\b", r"\bfeel free to (?:reach out|contact me|let me know)\b", r"\bshould you have any (?:questions|concerns)\b",
    r"\bif you have any (?:further |other |additional )?questions(?: or concerns)?,? (?:please )?(?:feel free|don't hesitate|do not hesitate|let me know)\b",
    r"\b(?:thank you|thanks) (?:so much |very much )?for your (?:patience|understanding|time and consideration|continued|prompt|kind)\b",
    r"\bi (?:really |truly |greatly |sincerely )?appreciate your (?:patience|understanding|time|flexibility|prompt|attention|continued)\b",
    r"\bat your earliest convenience\b", r"\bi look forward to hearing (?:from you|back)\b", r"\blooking forward to (?:connecting|working together|collaborating)\b",
    r"\brest assured\b", r"\bi (?:completely|totally|fully) understand\b", r"\bi understand (?:your|how) (?:frustrat|concern|this (?:can|must) be)\w*",
    r"\bi wanted to take a moment\b", r"\bplease be advised\b", r"\bas per (?:our|my|your)\b", r"\bper (?:our|my) (?:last|previous|earlier) (?:e-?mail|conversation|discussion)\b",
    r"\bkindly\b", r"\bi am pleased to\b", r"\bit was (?:a|an) (?:pleasure|delight) (?:to|speaking|meeting|connecting)\b", r"\bgreat (?:connecting|speaking|chatting) with you\b",
    r"\bthank you for (?:reaching out|getting in touch|your (?:e-?mail|message|inquiry|interest))\b", r"\bthanks for reaching out\b",
    r"\bwarm(?:est)? regards\b", r"\bi hope this helps\b", r"\bdelve\b", r"\bseamless(?:ly)?\b", r"\bleverag(?:e|ing)\b", r"\ba testament to\b",
    r"\bin today's [a-z-]+ (?:world|landscape|environment)\b", r"\bgame[- ]changer\b", r"\bmoving forward\b", r"\bgoing forward,",
    r"\bi value (?:our|your)\b", r"\byour (?:valued|continued) (?:partnership|business|support)\b", r"\bwith that (?:said|in mind)\b", r"\bthat being said\b",
    r"\bto ensure (?:a |that )?(?:smooth|we|everything)\b", r"\bplease let me know if (?:there(?:'s| is) anything|you need anything|i can)\b[^.!?\n]{0,40}",
    r"\bdon't hesitate\b", r"\bhappy to (?:assist|help) (?:with anything|in any way|however)\b", r"\bthank you in advance\b", r"\bi wanted to (?:let you know|share|inform you)\b")]

WHY = {'date': 'a day or date the owner did not give', 'time': 'a time or a time span the owner did not give', 'amount': 'a price, an amount or a number the owner did not give',
       'promise': 'a promise, an offer or a refusal the owner did not make', 'feeling': 'a feeling the owner did not state', 'personal': 'a personal detail the owner did not give',
       'stock': 'stock assistant phrasing'}


def stock_phrases(text: str) -> list:
    """Stock assistant phrases in a text: [(start, end, the words)]."""
    t = str(text or '').translate(_QUOTES)
    out, taken = [], []
    for rx in _STOCK:
        for m in rx.finditer(t):
            words = re.sub(r'\s+', ' ', m.group(0)).strip()
            if any(s < m.end() and m.start() < e for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            out.append((m.start(), m.end(), words))
    return sorted(out)


def _wrapped(text: str, m) -> bool:
    """A single line break in the middle of a sentence (mail wrapped at a fixed width): the line before is long and
    does not end a sentence, and the next line goes on in lower case or with a number."""
    if m.group(0) != '\n':
        return False
    before = text[:m.start()].rsplit('\n', 1)[-1].rstrip()
    after = text[m.end():m.end() + 2]
    return len(before) >= 45 and not re.search(r'[.!?:;,)\]"]$', before) and bool(re.match(r'[a-z0-9(]', after))


def sentences(text: str) -> list:
    """[(start, end)] of every sentence: split after . ! ? followed by a space, and at every line break, except a
    single break inside a hard-wrapped sentence."""
    out, start = [], 0
    for m in re.finditer(r'[.!?]+["\')\]]*(?=\s)|\n+', text):
        if _wrapped(text, m):
            continue
        end = m.end()
        if text[start:end].strip():
            out.append((start, end))
        start = end
    if text[start:].strip():
        out.append((start, len(text)))
    return out


def _content(clause: str, aux: bool = False) -> list:
    masked = list(clause)
    for _keys, s, e, _kind, _shown in facts(clause):
        masked[s:e] = ' ' * (e - s)
    low = ''.join(masked).lower()
    delivers = bool(re.search(r"\b(?:get|getting|have|having)\b[^.]{0,40}\b(?:to you|over|your way|back to you|to y'?all|to him|to her|to them)\b", low))
    out = []
    for w in _words(low):
        if w in ('get', 'getting', 'have', 'having') and delivers:
            out.append('send')
            continue
        st = _stem(w)
        if w.replace("'", '') in _STOP or st in _STOP or (st in _AUX and not aux) or len(st) < 2 or w.isdigit():
            continue
        out.append(st)
    return list(dict.fromkeys(out))


def _clause_end(low: str, start: int, joins: bool = False) -> int:
    m = re.compile(r'[,;:]\s|\s(?:but|so|because|although%s)\s' % ('|and|then|which|who' if joins else '')).search(low, start)
    return m.start() if m else len(low.rstrip(' .!?\n'))


def _promise(sentence: str, G: Given):
    """Is this sentence a commitment of the owner's that nothing given supports? -> (why in plain words, start, end of the
    commitment inside the sentence) or None."""
    low = sentence.lower().translate(_QUOTES)
    hit = _COMMIT.search(low)
    idioms = [(rx, ok) for rx, ok in _IDIOMS if rx.search(low)] if _FIRST_PERSON.search(low.strip()) else []
    if not hit and not idioms:
        return None
    for rx, ok in idioms:
        if not ok.search(G.text):
            m = rx.search(low)
            return 'it promises to come back on this, and nothing the owner gave says so', m.start(), _clause_end(low, m.end())
    if not hit:
        return None
    end = _clause_end(low, hit.end())
    lead = len(low) - len(low.lstrip())
    if G.conditional and _IF_DRAFT.search(low):
        # "send me the bill and I can check" for the instruction "if Alex sends the bill, I can check": the condition and the
        # promise are one sentence, and it is read as a whole against what the owner gave
        whole = _content(low)
        if whole and sum(G.knows(w) for w in whole) >= max(2, math.ceil(0.5 * len(whole))):
            return None
    words = _content(low[hit.start():end]) or [w for w in _content(low[hit.start():end], aux=True) if G.knows(w)]
    if not words:
        if idioms or _ACCEPT.search(G.own):
            return None
        return 'it agrees or commits, and nothing the owner gave says they do', lead, len(low.rstrip())
    hits = [w for w in words if G.knows(w)]
    if (G.knows(words[0]) and len(hits) >= max(1, math.ceil(0.6 * len(words)))) or (len(words) >= 3 and len(hits) >= math.ceil(0.75 * len(words))):
        return None
    missing = [w for w in words if not G.knows(w)][:3]
    return 'it commits the owner to something that is not in what the owner gave (%s)' % ', '.join(missing), hit.start(), end


def check_draft(draft, given) -> list:
    """The code check on a draft written in the owner's name. -> a list of flags, empty when the draft adds nothing.

    draft   the draft's text (the words that would go out)
    given   everything the draft may lean on. Either one string (all of it together), or a dict with any of:
              'instruction'  what the owner asked for, in the owner's words or as the item states it (a string or a list)
              'thread'       the thread so far: a string, a list of strings, or a list of messages
                             ({'text' or 'body': ..., 'mine': True for the owner's own})
              'facts'        the job's own source facts (what it checked and where): a string or a list
              'now'          the moment the draft is written (epoch seconds or ISO). With it "tomorrow", the weekday
                             and the date of the same day count as one
    Each flag: {'kind': 'date' | 'time' | 'amount' | 'promise' | 'feeling' | 'personal' | 'stock',
                'text': the words that are not supported, 'sentence': the sentence they stand in,
                'why': plain words, 'start', 'end': where `text` is in the draft}
    Rules: a date, time or amount must be in `given` as the same day, time or number (any spelling). A promise, offer,
    plan or refusal in the first person must have its own words in `given`. A feeling or personal detail must be named
    in `given`. Pure code, no model, no clock of its own."""
    return _public(_check(draft, given))


def _check(draft, given) -> list:
    """check_draft with the sentence each flag stands in ('_sentence', which the fix needs) still on each flag."""
    raw = str(draft or '')
    text = raw.translate(_QUOTES)                            # one character for one, so every position is also true in `raw`
    G = given if isinstance(given, Given) else Given(given)
    flags = []
    spans = sentences(text)

    def sentence_of(pos):
        return next(((s, e) for s, e in spans if s <= pos < e), (0, len(text)))

    def add(kind, start, end, why=None):
        s, e = sentence_of(start)
        flags.append({'kind': kind, 'text': raw[start:end].strip(), 'sentence': raw[s:e].strip(), 'why': why or WHY[kind], 'start': start, 'end': end,
                      '_sentence': (s, e)})

    committed = {}
    for s, e in spans:
        got = _promise(text[s:e], G)
        committed[(s, e)] = bool(_COMMIT.search(text[s:e].lower()))
        if got:
            add('promise', s + got[1], s + max(got[2], got[1] + 1), WHY['promise'] + ': ' + got[0])
        for rx, kind in ((_EFFECT, 'feeling'), (_STORY, 'personal')):
            m = rx.search(text[s:e])
            if m:
                backed = lambda own: bool(own) and sum(G.knows(w) for w in own) >= max(2, math.ceil(0.6 * len(own)))
                clause = text[s + m.start():s + _clause_end(text[s:e].lower(), m.end(), joins=True)]
                if not (backed(_content(clause)) or backed(_content(text[s:e]))):
                    add(kind, s + m.start(), s + m.end(), 'a feeling the owner did not state' if kind == 'feeling' else "the owner's own history with them, which the owner did not give")
    for keys, s, e, kind, shown in facts(text):
        if set(keys) & G.keys:
            continue
        span = re.fullmatch(r'span:([\d.]+):(\w+)', keys[0])
        if span and 'n:' + span.group(1) in G.keys and _stem(span.group(2)) in G.stems | {span.group(2)}:
            continue
        if keys == ('rel:0',) and re.fullmatch(r'(?i)today|tonight|tonite', shown.strip()) and not committed.get(sentence_of(s)):
            continue
        if kind == 'vague':
            if committed.get(sentence_of(s)) and not G.has(keys[0].split(':', 1)[1]):
                add('time', s, e, 'a "how soon" the owner did not give')
            continue
        add(kind, s, e)
    for m in _MONEY_WORDS.finditer(text):
        if not any(G.knows(_stem(w)) for w in _words(m.group(0)) if w not in _STOP and len(w) > 2):
            add('amount', m.start(), m.end(), 'a price term the owner did not give')
    for rx, kind in ((_FEEL, 'feeling'), (_PERSONAL, 'personal')):
        for m in rx.finditer(text):
            key = next((g for g in m.groups() if g), None) or m.group(0)
            if any(G.knows(_stem(w)) for w in _words(key) if w not in _STOP):
                continue
            add(kind, m.start(), m.end())
    for s, e, _words_ in stock_phrases(text):
        add('stock', s, e)
    flags.sort(key=lambda f: (f['start'], f['end']))
    out, seen = [], set()
    for f in flags:
        key = (f['kind'], f['start'], f['end'])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# ---- the one remove-only fix -------------------------------------------------------------------------------------------

_LEAD_IN = re.compile(r"(?:(?:,\s*|\s+)(?:and|or)\s+)?(?:(?:,\s*)?\b(?:by|on|at|before|until|till|til|around|about|for|within|in|no later than|as early as|starting|from|"
                      r"after|since|due|come|early|late|later|sometime|first thing|through|thru|approximately|roughly|just|only|at least|up to|over|under)\s+"
                      r"(?:the\s+|a\s+|an\s+|this\s+|next\s+|about\s+|around\s+)?(?:(?:end|start|beginning|middle|morning|afternoon|evening|night|close) of\s+)?)+$", re.I)
_DANGLING = frozenset("""to by on at for with of in the a an and or but is are was were be than from about around until before after as that this my your our off
    will can i'll we'll i we it's its so if when then also plus per each every like around approximately roughly only just""".split())


def _reads(rest: str) -> bool:
    """Does what is left of a sentence still read as one? At least two words, and it does not end on a word that
    needs something after it."""
    words = re.findall(r"[A-Za-z0-9$%']+", rest)
    return len(words) >= 2 and words[-1].lower() not in _DANGLING and not re.search(r"[$\u20ac\xa3]\s*$", rest)


_GREETING = re.compile(r"^\s*(?:hi|hey|hello|dear|morning|good (?:morning|afternoon|evening)|thanks|thank you)\b[^,.!?\n]{0,30}[,!]\s*", re.I)
_CLAUSE_END = re.compile(r"^\s*(?:[.!?\"')\]]+\s*$|$|,|;|\s(?:and|but|so|or)\s)", re.I)


def _holds_given(text: str, s: int, e: int, group: list, G: Given) -> bool:
    """Does this sentence hold something the owner gave, once the flagged parts are set aside? Then cutting the sentence
    would cut the owner's own request with it."""
    keep = list(text[s:e])
    for f in group:
        a, b = f['start'] - s, f['end'] - s
        keep[a:b] = ' ' * (b - a)
    rest = _GREETING.sub(' ', ''.join(keep))
    own = _content(rest)
    known = [w for w in own if G.knows(w)]
    if len(known) >= 2 or (len(known) == 1 and len(own) <= 2):
        return True
    whole = _content(_GREETING.sub(' ', text[s:e]))      # the flagged part itself half in the owner's words: a doubtful bit, so it is named
    return sum(G.knows(w) for w in whole) >= max(2, math.ceil(len(whole) / 3))


def _plan(text: str, flags: list, G: Given) -> tuple:
    """The one remove-only fix, planned. -> (cuts [(start, end)], flags that are NAMED instead of cut).
    Rule of thumb (the owner's side): a cut that takes their real request with it is worse than naming a doubtful bit.
    - A sentence that holds nothing the owner gave (besides a greeting): the sentence goes; a greeting on the same line stays.
    - A sentence that also holds what the owner gave: only a date, time or amount at the end of a clause goes, with its own
      lead-in ("by Friday", "for $25"), and only when what is left still reads. Everything else there is NAMED."""
    by_sentence = {}
    for f in flags:
        by_sentence.setdefault(f['_sentence'], []).append(f)
    cuts, named = [], []
    for (s, e), group in sorted(by_sentence.items()):
        if not _holds_given(text, s, e, group, G):
            g = _GREETING.match(text[s:e])
            cuts.append((s + g.end(), e) if g and all(f['start'] >= s + g.end() for f in group) else (s, e))
            continue
        piece = []
        for f in group:
            a, b = f['start'], f['end']
            if f['kind'] not in ('date', 'time', 'amount') or not _CLAUSE_END.match(text[b:e]):
                named.append(f)
                continue
            lead = _LEAD_IN.search(text[s:a])
            if lead:
                a = s + lead.start()
            elif text[s:a].strip():
                named.append(f)
                continue
            elif text[b:b + 1] == ',':
                b += 1                                    # "Tomorrow, I'll send it."
            rest = text[s:a] + text[b:e]
            if _reads(rest) and _holds_given(rest, 0, len(rest), [], G):
                piece.append((a, b))
            else:
                named.append(f)
        cuts += piece
    return cuts, named


def _cut(text: str, cuts: list) -> tuple:
    merged = []
    for a, b in sorted(cuts):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    out, removed, last = '', [], 0
    for a, b in merged:
        out += text[last:a]
        removed.append(text[a:b].strip())
        last = b
    out += text[last:]
    return _tidy(out), [r for r in removed if r]


def _tidy(t: str) -> str:
    t = re.sub(r'[ \t]+([,.;:!?])', r'\1', t)
    t = re.sub(r'([,;:])\s*([.!?])', r'\2', t)
    t = re.sub(r'(?m)^[ \t]*[,;:]\s*', '', t)
    t = re.sub(r'[ \t]{2,}', ' ', t)
    t = re.sub(r'[ \t]+\n', '\n', t)
    return re.sub(r'\n{3,}', '\n\n', t).strip()


def only_removed(before: str, after: str) -> bool:
    """True when `after` is `before` with words taken out and nothing else: every word of it, in order, is in `before`."""
    a = re.findall(r"[a-z0-9$%']+", str(before).lower().translate(_QUOTES))
    b = re.findall(r"[a-z0-9$%']+", str(after).lower().translate(_QUOTES))
    i = 0
    for w in b:
        while i < len(a) and a[i] != w:
            i += 1
        if i == len(a):
            return False
        i += 1
    return True


def _public(flags: list) -> list:
    return [{k: v for k, v in f.items() if not k.startswith('_')} for f in flags]


def fix_draft(draft, given) -> dict:
    """Check, ONE remove-only fix, check again. Only what clearly was added is cut (see _plan); a doubtful bit that
    shares a sentence with what the owner gave is NAMED instead, and so is anything the second check still finds. When
    cutting would leave no draft (under 20 characters or 4 words, or more than 75% of its words gone), nothing is cut and every
    unsupported part is named. Never passed as clean while anything is named.
    -> {'text': the draft to use, 'clean': True when nothing unsupported is left in it, 'changed', 'flags': what the
        first check found, 'removed': the pieces taken out, 'named': flags still standing in 'text'}"""
    original = str(draft or '')
    G = given if isinstance(given, Given) else Given(given)
    first = _check(original, G)
    if not first:
        return {'text': original, 'clean': True, 'changed': False, 'flags': [], 'removed': [], 'named': []}
    cuts, _named = _plan(original.translate(_QUOTES), first, G)
    text, removed = _cut(original, cuts) if cuts else (original, [])
    kept = len(re.findall(r'\w+', text))
    was = max(1, len(re.findall(r'\w+', original)))
    if len(text) < MIN_LEFT or kept < 4 or kept < 0.25 * was or not only_removed(original, text):
        text, removed = original, []
    left = _check(text, G)                                   # checked again: what still stands is named, never cut again
    return {'text': text, 'clean': not left, 'changed': text != original, 'flags': _public(first), 'removed': removed, 'named': _public(left)}


# ---- the worker hook: the draft that came back -------------------------------------------------------------------------

def _item_given(opp: dict, packet: dict, thread_msgs, now) -> dict:
    steps = (opp.get('decision') or {}).get('steps') or []
    commitment = opp.get('commitment') if isinstance(opp.get('commitment'), dict) else {}
    instruction = [opp.get('title'), opp.get('why'), opp.get('evidence'), opp.get('proposed_action'), opp.get('text'), opp.get('details'),
                   opp.get('deadline'), opp.get('event_start'), (opp.get('followup') or {}).get('check'), commitment.get('facts'), commitment.get('said')]
    instruction += [s.get('do') for s in steps if isinstance(s, dict)]
    job_facts = [packet.get('details'), packet.get('learned'), packet.get('sources'), [f.get('check') for f in packet.get('followups') or [] if isinstance(f, dict)]]
    thread = [{'text': m.get('body'), 'mine': m.get('mine')} for m in thread_msgs or []] + [m.get('subject') for m in (thread_msgs or [])[:1]]
    return {'instruction': [x for x in instruction if x], 'thread': thread, 'facts': [x for x in job_facts if x], 'now': now}


def on_packet(con, key: str, opp: dict, packet: dict, *, read=None, now=None):
    """Hook at the top of worker._ready, before the step is frozen (so the owner's yes is bound to the checked words).
    For an item that drafts in the owner's name: the draft that came back is checked against the item, the thread (read
    again here, read only) and the job's own facts; unsupported parts get the one remove-only fix; what cannot be
    cut is named plainly in the result's details. Without the switch, or for any other item: nothing. Never raises."""
    try:
        if not isinstance(packet, dict) or packet.get('draft_check') or not WS.drafts_in_his_name(opp):
            return
        import actions as A
        life = A.home_of(con)
        if not on(life):
            return
        dec = packet.get('decision') if isinstance(packet.get('decision'), dict) else None
        send = packet.get('send') if isinstance(packet.get('send'), dict) else None
        body = str((send or {}).get('body') or '').strip()
        ready = str((dec or {}).get('ready') or '').strip()
        if not body and not ready:
            return
        now = now if now is not None else time.time()
        thread_msgs, read_ok = [], True
        mail, thread = opp.get('latest_email') or {}, opp.get('thread') or {}
        mailbox, tid = mail.get('mailbox') or thread.get('mailbox'), thread.get('thread_id') or mail.get('thread_id')
        if mailbox and tid:
            try:
                thread_msgs = [m for m in _reader(read).thread(mailbox, tid)]
                read_ok = bool(thread_msgs)
            except Exception:
                read_ok = False
        G = Given(_item_given(opp, packet, thread_msgs, now))
        note = {'at': now, 'thread_read': read_ok, 'flags': 0, 'removed': [], 'named': []}
        res = fix_draft(body or ready, G)
        note['flags'] = len(res['flags'])
        note['kinds'] = sorted({f['kind'] for f in res['flags']})
        if res['flags'] and not read_ok:
            # The thread could not be read again, so a part that looks unsupported may be in it. Nothing is cut on
            # that; the parts are named, and that the thread was not read is said.
            res = {**res, 'text': body or ready, 'changed': False, 'clean': False, 'removed': [], 'named': res['flags']}
        if res['changed']:
            note['removed'] = res['removed'][:8]
            if body:
                send['body'] = res['text']
                if dec is not None and ready:
                    dec['ready'] = _same_cuts(ready, body, res, G)
            else:
                dec['ready'] = res['text']
        if res['named']:
            note['named'] = [{'kind': f['kind'], 'text': f['text'][:160]} for f in res['named'][:8]]
            said = '; '.join('"%s"' % f['text'][:90] for f in res['named'][:3])
            line = ('Before this goes, the owner should look at these parts of the draft: they are not in their request, the thread or what was checked: %s.' % said
                    if read_ok else 'The thread could not be read again to check the draft, so these parts of it are not backed up: %s.' % said)
            packet['details'] = (str(packet.get('details') or '').strip() + ' ' + line).strip()[:900]
        packet['draft_check'] = note
    except Exception as e:
        try:
            opp['draft_voice_error'] = f'{type(e).__name__}: {str(e)[:160]}'
        except Exception:
            pass


def _same_cuts(ready: str, body: str, res: dict, G: Given) -> str:
    """The step's description ("ready") usually repeats the reply. The same pieces are taken out of it: the whole reply
    swapped when it stands there word for word, else each removed piece; when neither is found, it gets its own fix."""
    if body and body in ready:
        return ready.replace(body, res['text'])
    out, found = ready, 0
    for piece in res['removed']:
        if piece and piece in out:
            out = out.replace(piece, '')
            found += 1
    if found == len(res['removed']):
        return _tidy(out)
    again = fix_draft(ready, G)
    return again['text'] if again['changed'] else out

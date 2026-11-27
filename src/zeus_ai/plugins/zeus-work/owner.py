"""Who is the owner (or a configured approver) on this turn, and what plain words mean.

Identity comes only from native runtime metadata (turn author, platform, the
email adapter's own envelope), never from message text a model or outside
sender wrote. Word matching is deliberately forgiving: the owner should never
have to repeat a stop or a yes in special syntax.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / 'engine'
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))
import settings  # noqa: E402

LOCAL_PLATFORMS = {'cli', 'tui', 'desktop', 'gui', 'local', 'shared_operator'}

# Fuzzy STOP: the whole message must be a stop instruction (after polite words),
# so "can you stop by the store" or "don't stop" never count.
_STOP_VERBS = {'stop', 'cancel', 'halt', 'abort', 'pause', 'quit', 'kill', 'end', 'freeze', 'enough'}
_STOP_TYPOS = {'stp': 'stop', 'stpo': 'stop', 'sotp': 'stop', 'stopp': 'stop', 'stpp': 'stop', 'sop': None,
               'cancle': 'cancel', 'cancell': 'cancel', 'cansel': 'cancel', 'cnacel': 'cancel',
               'cancl': 'cancel', 'canel': 'cancel', 'hault': 'halt', 'puase': 'pause', 'paus': 'pause'}
# The agent's own name counts as a polite lead/trail word ("zeus stop", "stop please zeus").
_AGENT = {w for w in re.findall(r"[a-z0-9']+", str(settings.AGENT_NAME or '').lower())}
_LEAD = {'hey', 'hi', 'ok', 'okay', 'oh', 'yo', 'um', 'umm', 'please', 'pls', 'plz', 'just', 'actually', 'whoa', 'woah', 'no', 'nope', 'ugh', 'hun', 'babe', 'girl', 'can',
         'you', 'could', 'would', 'u', 'go', 'ahead', 'and'} | _AGENT
_TRAIL = {'please', 'pls', 'plz', 'now', 'asap', 'immediately', 'thanks', 'thx', 'ty',
          'already', 'ok', 'okay', 'thank', 'you', 'for', 'then', 'right', 'away', 'today', 'for now'} | _AGENT
_OBJECTS = {'it', 'that', 'this', 'everything', 'all', 'of', 'the', 'work', 'working', 'job', 'jobs',
            'task', 'tasks', 'video', 'videos', 'image', 'images', 'render', 'rendering', 'generation',
            'generating', 'request', 'requests', 'email', 'emails', 'send', 'sending', 'what', "you're",
            'youre', 'you', 'are', 'doing', 'making', 'creating', 'those', 'these', 'them', 'stuff',
            'whatever', 'thing', 'things', 'now', 'process', 'run', 'running', 'please', 'pls', 'plz'}
_PHRASES = ('stand down', 'hold off', 'belay that', 'call it off', 'shut it down', 'shut down')
_STOP_EMOJI = {'\U0001f6d1', '✋', '⛔', '\U0001f6ab'}


def _clean(text):
    text = unicodedata.normalize('NFKC', str(text or '')).replace('’', "'").replace('‘', "'")
    return text.strip()


def words(text):
    return re.findall(r"[a-z0-9']+", _clean(text).lower())


# Optional: the owner's own terminal relay may wrap their words in a fixed prefix. Set `identity.relay_prefix`
# to a regular expression for that prefix to strip it before word matching. Identity is still checked natively.
try:
    _RELAY_PREFIX = re.compile(str(settings.get('identity.relay_prefix', '') or ''), re.I) \
        if settings.get('identity.relay_prefix') else None
except re.error:
    _RELAY_PREFIX = None


def unwrap_relay(text):
    text = str(text or '')
    return _RELAY_PREFIX.sub('', text, count=1) if _RELAY_PREFIX is not None and _RELAY_PREFIX.match(text) else text


def stop_kind(text):
    """'stop', 'pause' or None. Plain, forgiving, whole-message only."""
    raw = _clean(unwrap_relay(text))
    if not raw or len(raw) > 120:
        return None
    if raw.startswith('/'):
        command = raw[1:].split(None, 1)[0].lower() if raw[1:].strip() else ''
        return 'pause' if command == 'pause' else 'stop' if command in {'stop', 'cancel'} else None
    stripped = ''.join(ch for ch in raw if not unicodedata.category(ch).startswith(('S', 'P', 'Z')) or ch == ' ')
    if not stripped.strip() and any(e in raw for e in _STOP_EMOJI):
        return 'stop'
    tokens = [_STOP_TYPOS.get(t, t) for t in words(raw)]
    tokens = [t for t in tokens if t]
    while tokens and tokens[0] in _LEAD:
        tokens.pop(0)
    while tokens and tokens[-1] in _TRAIL:
        tokens.pop()
    if not tokens or len(tokens) > 8:
        return None
    joined = ' '.join(tokens)
    if any(joined == p or joined.startswith(p + ' ') and all(t in _OBJECTS for t in joined[len(p):].split())
           for p in _PHRASES):
        return 'pause' if joined.startswith('hold off') else 'stop'
    verb, rest = tokens[0], tokens[1:]
    if verb not in _STOP_VERBS:
        return None
    if verb == 'end' and not rest:
        return None  # a lone "end" is too ambiguous (end of a list, a word game)
    if not all(t in _OBJECTS for t in rest):
        return None
    return 'pause' if verb == 'pause' else 'stop'


# Fuzzy approvals: a clear yes plus filler is a yes, a clear no is a no; "ok" alone never approves.
_YES = {'yes', 'yeah', 'yea', 'yep', 'yup', 'ya', 'yas', 'yess', 'yesss', 'yse', 'ye', 'sure', 'approved',
        'approve', 'send', 'sent', 'go', 'ahead', 'do', 'it', 'ship', 'confirmed', 'confirm', 'absolutely',
        'definitely', 'please', 'pls', 'plz', 'to', 'all', 'them', 'both', 'that', 'this', 'one', 'is',
        'fine', 'good', 'sounds', 'looks', 'great', 'perfect', 'and', 'now', 'ok', 'okay', 'k'} | _AGENT
_YES_CORE = {'yes', 'yeah', 'yea', 'yep', 'yup', 'ya', 'yas', 'yess', 'yesss', 'yse', 'ye', 'sure',
             'approved', 'approve', 'send', 'ship', 'confirmed', 'confirm', 'absolutely', 'definitely'}
_YES_PHRASES = ('go ahead', 'do it', 'sounds good', 'looks good', 'send it', 'ship it')
_NO = {'no', 'nope', 'nah', 'skip', 'dont', "don't", 'decline', 'declined', 'reject', 'rejected', 'cancel'}


def approval(text):
    """('yes'|'no', numbers or 'all' or None) for a short plain-words reply, else None."""
    raw = _clean(unwrap_relay(text))
    if not raw or len(raw) > 80:
        return None
    if raw in {'\U0001f44d', '✅', '\U0001f44c'}:
        return ('yes', None)
    tokens = words(raw)
    if not tokens or len(tokens) > 8:
        return None
    numbers = [int(t) for t in tokens if t.isdigit() and 0 < int(t) < 100]
    plain = [t for t in tokens if not t.isdigit()]
    target = 'all' if 'all' in plain or 'both' in plain else (numbers or None)
    if plain and plain[0] in _NO and all(t in _NO | {'send', 'it', 'that', 'this', 'them', 'to', 'all', 'please',
                                                     'thanks', 'one', 'do', 'not', 'skip'} for t in plain):
        return ('no', target)
    joined = ' '.join(plain)
    if (any(t in _YES_CORE for t in plain) or any(p in joined for p in _YES_PHRASES)) and all(t in _YES for t in plain):
        return ('yes', target)
    return None


# ------------------------------------------------------------------ identity

def _key(platform, user_id):
    return str(platform or '').strip().lower() + ':' + str(user_id or '').strip()


def _id_set(values):
    out = set()
    for value in values or ():
        if isinstance(value, str) and value.strip() and value.strip() != '*':
            out.add(value.strip() if ':' in value else value.strip())
    return out


class Identities:
    """Configured people. Settings live in the plugin's own config entry.

    owner_ids:    'platform:user_id' strings for the owner (for example 'photon:+15550100000').
    relay_ids:    'platform:user_id' for the owner's own terminal relays, if any.
    approver_ids: {'Taylor': ['photon:+15550100001'], ...} native accounts of people the owner named as approvers.
                  Empty by default: then only the owner can approve.
    approver_emails: {'Taylor': ['taylor@example.test'], ...} verified-sender email accounts of those approvers.
    """

    def __init__(self, settings):
        settings = settings if isinstance(settings, dict) else {}
        self.owner_ids = _id_set(settings.get('owner_ids'))
        self.relay_ids = _id_set(settings.get('relay_ids'))
        self.local_is_owner = settings.get('local_is_owner', True) is True
        self.approver_ids = {}
        for name, ids in (settings.get('approver_ids') or {}).items():
            for value in _id_set(ids):
                self.approver_ids[value.lower()] = str(name)
        self.approver_emails = {}
        for name, addresses in (settings.get('approver_emails') or {}).items():
            for value in addresses or ():
                if isinstance(value, str) and '@' in value:
                    self.approver_emails[value.strip().casefold()] = str(name)
        self.self_ids = {v.lower() for v in _id_set(settings.get('self_ids'))}
        self.self_emails = {v.strip().casefold() for v in settings.get('self_emails') or () if isinstance(v, str)}

    @property
    def configured(self):
        return bool(self.owner_ids)

    def is_owner_account(self, platform, user_id):
        """The owner's own account, or their own terminal relay, on this transport."""
        key = _key(platform, user_id)
        return key in self.owner_ids or key in self.relay_ids

    def authority(self, context):
        """{'kind': 'owner'|'approver'|'none', 'who': str, 'why': str} from native metadata only."""
        none = lambda why: {'kind': 'none', 'who': '', 'why': why}
        if context.get('parent_session_id'):
            return none('helper_turn')
        if context.get('turn_display_kind'):
            return none('synthetic_turn')
        platform = str(context.get('platform') or '').strip().lower()
        if platform == 'cron':
            return none('scheduled_turn')
        author = context.get('turn_author')
        author = author if isinstance(author, dict) else None
        author_id = str((author or {}).get('id') or context.get('sender_id') or '')
        is_bot = (author or {}).get('is_bot')
        if author_id and (_key(platform, author_id).lower() in self.self_ids or author_id.lower() in self.self_ids):
            return none('own_account')
        if platform in LOCAL_PLATFORMS:
            if author and is_bot is not False:
                return none('bot_on_local_surface')
            if self.local_is_owner:
                return {'kind': 'owner', 'who': settings.OWNER_FIRST, 'why': 'local_operator'}
        if platform == 'agentmail':
            if author and is_bot is False:
                # Only the email adapter marks mail human, and only after the owner's own
                # address passed DMARC plus SPF or DKIM (agentmail adapter owner_mail).
                return {'kind': 'owner', 'who': settings.OWNER_FIRST, 'why': 'verified_owner_email'}
            sender = verified_email_sender(context.get('user_message'))
            if sender and sender in self.self_emails:
                return none('own_account')
            if sender and sender in self.approver_emails:
                return {'kind': 'approver', 'who': self.approver_emails[sender], 'why': 'verified_approver_email'}
            return none('outside_email')
        key = _key(platform, author_id)
        if author_id and is_bot is False and key in self.owner_ids:
            return {'kind': 'owner', 'who': settings.OWNER_FIRST, 'why': 'owner_account'}
        if author_id and is_bot is False and key in self.relay_ids:
            return {'kind': 'owner', 'who': settings.OWNER_FIRST, 'why': 'owner_terminal_relay'}
        if author_id and key.lower() in self.approver_ids:
            # An approver's yes counts only from that person's own account.
            return {'kind': 'approver', 'who': self.approver_ids[key.lower()], 'why': 'approver_account'}
        return none('not_owner_or_approver')


_ENVELOPE = 'Received envelope: '
_BODY = '\n--- BEGIN UNTRUSTED EMAIL ---\n'
_FRAME = 'New email delivered to your own AgentMail inbox by its native email channel.'


def verified_email_sender(user_message):
    """Sender address from the AgentMail adapter's own envelope, if DMARC + SPF/DKIM passed.

    The envelope line is written by the adapter before the untrusted body marker and
    JSON-encodes every field, so an email body cannot forge a second envelope.
    """
    text = user_message if isinstance(user_message, str) else ''
    if not text.startswith(_FRAME) or _BODY not in text:
        return None
    head = text.split(_BODY, 1)[0]
    if _ENVELOPE not in head:
        return None
    try:
        details = json.loads(head.split(_ENVELOPE, 1)[1].strip())
    except (ValueError, TypeError):
        return None
    from email.utils import getaddresses
    value = details.get('from')
    senders = [a.casefold() for _, a in getaddresses([value] if isinstance(value, str) else value or [])]
    auth = details.get('authentication_results') or {}
    if len(senders) != 1 or not isinstance(auth, dict):
        return None
    passed = lambda key: str(auth.get(key, '')).casefold() == 'pass'
    if not (passed('dmarc') and (passed('spf') or passed('dkim'))):
        return None
    return senders[0]

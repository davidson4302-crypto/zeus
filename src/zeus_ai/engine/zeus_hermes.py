"""Zeus's one owner test and its one bridge to Hermes turn identity.

Every Zeus plugin asks this module, and only this module, whether the current turn (or a message source) came
from the owner. The answer uses public Hermes Agent building blocks only (v0.23.4):

  - the turn's bound session context (``gateway.session_context.bound_session_env``: platform, chat type,
    sender id, chat id, message id, cron flag), with no ``os.environ`` fallback;
  - the turn's own inbound event, bound by the gateway for the turn (``build_session_context(turn_event=...)``),
    used only when it agrees with that session context;
  - the agent's own turn metadata (``_turn_author``, ``_turn_display_kind``) through
    ``agent.subagent_lifecycle.get_active_subagent_parent``.

"The owner" means: the configured owner platform (``owner.platform``, default ``photon``) in a direct (non-group,
non-thread) chat, from the sender id ``owner.phone``, on a real inbound message the gateway accepted. A local CLI or
TUI turn counts only when ``owner.allow_cli`` is true in Zeus's settings.

Fail closed: anything that cannot be determined (no turn bound, a missing Hermes API, an exception, an unknown
platform, a group chat, a cron or background job, a delegated helper, an internal event, a missing or garbled
source) answers "not the owner". Nothing here turns an error into "allowed".

This file is plain Python at import time (no Hermes imports), so the life engine can use ``owner_ids`` too.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Optional

import settings

LOCAL_SURFACES = frozenset({'cli', 'tui', 'local'})
_SURFACE_ALIASES = {'gui': 'desktop', 'local': 'cli'}


# ---- who the owner is (pure) ------------------------------------------------------------------------

def owner_platform() -> str:
    return str(settings.get('owner.platform', 'photon') or 'photon').strip().lower()


def normalize_id(value: Any) -> str:
    """A sender id in one comparable form: phone numbers as +digits, anything else trimmed and lower-cased."""
    text = str(value or '').strip()
    if not text:
        return ''
    for prefix in ('tel:', 'sms:', 'imessage:'):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    digits = re.sub(r'[\s().-]', '', text)
    if re.fullmatch(r'\+?\d{7,15}', digits):
        return '+' + digits.lstrip('+')
    return text.lower()


def owner_ids() -> frozenset:
    """The owner's sender ids on the owner platform. Empty when not configured (then nobody is the owner)."""
    phone = normalize_id(getattr(settings, 'OWNER_PHONE', ''))
    return frozenset({phone}) if phone else frozenset()


def allow_cli() -> bool:
    return settings.get('owner.allow_cli', False) in (True, 'true', 'True', '1', 1)


def surface(value: Any) -> str:
    value = str(getattr(value, 'value', value) or '').strip().lower()
    return _SURFACE_ALIASES.get(value, value)


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def is_owner_source(source: Any) -> bool:
    """True only for a direct chat on the owner platform from the owner's sender id.

    ``source`` may be a Hermes ``SessionSource`` or its ``to_dict()`` form. A bot sender, a group or channel chat,
    a thread, another platform, or any missing field is "not the owner"."""
    if source is None:
        return False
    platform = surface(_field(source, 'platform'))
    if platform != owner_platform() or platform in LOCAL_SURFACES:
        return False
    if str(_field(source, 'chat_type') or '').strip().lower() != 'dm':
        return False
    if _field(source, 'thread_id') or _field(source, 'parent_chat_id'):
        return False
    if _field(source, 'is_bot') is True:
        return False
    if not _field(source, 'chat_id'):
        return False
    sender = normalize_id(_field(source, 'user_id'))
    ids = owner_ids()
    return bool(sender) and sender in ids


# ---- the current turn (Hermes, lazily imported) -----------------------------------------------------

def _session(name: str) -> str:
    from gateway.session_context import bound_session_env
    return str(bound_session_env(name) or '')


def session_snapshot() -> Optional[dict]:
    """The turn's bound session context, or None when this context is not inside a turn."""
    try:
        names = {'platform': 'HERMES_SESSION_PLATFORM', 'source': 'HERMES_SESSION_SOURCE',
                 'chat_id': 'HERMES_SESSION_CHAT_ID', 'chat_type': 'HERMES_SESSION_CHAT_TYPE',
                 'user_id': 'HERMES_SESSION_USER_ID', 'thread_id': 'HERMES_SESSION_THREAD_ID',
                 'message_id': 'HERMES_SESSION_MESSAGE_ID', 'session_id': 'HERMES_SESSION_ID',
                 'key': 'HERMES_SESSION_KEY', 'profile': 'HERMES_SESSION_PROFILE',
                 'parent_chat_id': 'HERMES_SESSION_PARENT_CHAT_ID'}
        snap = {k: _session(v) for k, v in names.items()}
        snap['cron'] = _session('HERMES_CRON_SESSION')
    except Exception:
        return None
    if not snap['platform'] and not snap['source']:
        return None
    return snap


def turn_event() -> Any:
    """The inbound event the gateway bound for this turn, or None."""
    try:
        from gateway import session_context
        holder = getattr(session_context, '_SESSION_TURN_EVENT', None)
        return holder.get() if holder is not None else None
    except Exception:
        return None


def current_event() -> Any:
    """The current turn's inbound event, only when it agrees with the turn's bound session context."""
    event, snap = turn_event(), session_snapshot()
    if event is None or snap is None:
        return None
    source = getattr(event, 'source', None)
    if source is None:
        return None
    if (surface(getattr(source, 'platform', None)) != surface(snap['platform'])
            or str(getattr(source, 'chat_id', '') or '') != snap['chat_id']
            or normalize_id(getattr(source, 'user_id', '')) != normalize_id(snap['user_id'])):
        return None
    return event


def _parent() -> Any:
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
        return get_active_subagent_parent()
    except Exception:
        return None


def in_delegated_helper() -> bool:
    """True inside a delegated helper (or when that cannot be ruled out)."""
    try:
        from agent.delegation_context import is_delegated_child_context
        return bool(is_delegated_child_context())
    except Exception:
        return True


def turn_author(context: Optional[Mapping] = None) -> Optional[dict]:
    """The turn's author ``{"id", "is_bot"}``: the hook's own value when given, else the agent's."""
    value = (context or {}).get('turn_author') if isinstance(context, Mapping) else None
    if value is None:
        value = getattr(_parent(), '_turn_author', None)
    return dict(value) if isinstance(value, Mapping) else None


def turn_display_kind(context: Optional[Mapping] = None) -> Optional[str]:
    """The turn's display kind (None for an ordinary human message): the hook's value, else the agent's.
    An internal event (a helper result, a wake) reads as 'internal'."""
    if isinstance(context, Mapping) and context.get('turn_display_kind') is not None:
        return str(context['turn_display_kind'])
    kind = getattr(_parent(), '_turn_display_kind', None)
    if kind is not None:
        return str(kind)
    event = turn_event()
    if event is not None and getattr(event, 'internal', False) is not False:
        return 'internal'
    return None


def is_human_turn(author: Any, display_kind: Any = None, parent_session_id: Any = '') -> bool:
    """Missing provenance or a typed synthetic turn never becomes human from its text."""
    return (not parent_session_id and display_kind is None and isinstance(author, Mapping)
            and isinstance(author.get('id'), str) and author['id'].strip() not in {'', 'tool'}
            and author.get('is_bot') is False)


def owner_turn() -> Optional[dict]:
    """The current turn's coordinates when it came from the owner, else None (fail closed).

    Remote: the owner platform, a direct chat, the owner's sender id, a non-internal inbound event the gateway
    accepted, matching the bound session context; never a cron job, a delegated helper or another sender.
    Local: a CLI/TUI turn with no inbound event, only when ``owner.allow_cli`` is set."""
    try:
        snap = session_snapshot()
        if snap is None or snap['cron'] == '1' or in_delegated_helper():
            return None
        platform = surface(snap['platform'] or snap['source'])
        if platform in LOCAL_SURFACES:
            if not allow_cli() or turn_event() is not None or snap['user_id']:
                return None
            return {'platform': platform, 'local': True, 'session_id': snap['session_id'], 'author_id': None,
                    'chat_id': None, 'message_id': None}
        event = current_event()
        if (event is None or getattr(event, 'internal', True) is not False
                or getattr(event, '_gateway_accepted', None) is not True):
            return None
        source = event.source
        if not is_owner_source(source) or str(getattr(source, 'chat_type', '')) != snap['chat_type']:
            return None
        message_id = str(getattr(event, 'message_id', '') or getattr(source, 'message_id', '') or '')
        if not message_id:
            return None
        return {'platform': platform, 'local': False, 'session_id': snap['session_id'],
                'author_id': normalize_id(source.user_id), 'chat_id': str(source.chat_id),
                'message_id': message_id, 'session_key': snap['key']}
    except Exception:
        return None


def is_owner_turn() -> bool:
    return owner_turn() is not None


def is_owner_event(event: Any) -> bool:
    """An inbound event (a gateway dispatch hook's ``event``) from the owner: not internal, owner source."""
    try:
        return (event is not None and getattr(event, 'internal', True) is False
                and is_owner_source(getattr(event, 'source', None)))
    except Exception:
        return False

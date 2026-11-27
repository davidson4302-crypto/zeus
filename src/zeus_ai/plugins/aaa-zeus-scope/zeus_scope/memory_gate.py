"""Host-owned, current-turn private audience gate. Never a memory authority store.

The gateway source, native turn receipt and current agent must agree. A context
copy carries only a revocable reference, not authority to outlive its native turn.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import threading
import time
import weakref

from . import trusted_chat

_LOCK = threading.RLock()
_CURRENT_BY_SESSION = {}
_REMOTE_ALLOWLIST = {"photon": "PHOTON_ALLOWED_USERS", "slack": "SLACK_ALLOWED_USERS",
                     "discord": "DISCORD_ALLOWED_USERS"}
_LOCAL = frozenset({"cli", "tui", "desktop"})


@dataclass(frozen=True)
class _Grant:
    home: Path
    session_id: str
    turn_id: str
    holder: str
    parent: weakref.ReferenceType
    event: weakref.ReferenceType | None
    coordinates: tuple
    platform: str
    author_id: str
    receipt_id: str


def _parent():
    from agent.subagent_lifecycle import get_active_subagent_parent
    return get_active_subagent_parent()


def _surface(value):
    value = str(getattr(value, "value", value) or "").lower()
    return {"gui": "desktop", "local": "cli"}.get(value, value)


def _owned(home, parent):
    from hermes_constants import get_hermes_home
    if (parent is None or home.name not in trusted_chat.scoped_profiles() or Path(get_hermes_home()).resolve() != home
            or getattr(parent, "_parent_session_id", None) or getattr(parent, "_turn_origin", None)
            or getattr(parent, "_persist_disabled", False) or getattr(parent, "_interrupt_requested", False)):
        return False
    db = getattr(getattr(parent, "_session_db", None), "db_path", None)
    return db is not None and Path(db).resolve() == home / "state.db"


def _live_turn_lease(home, parent, holder):
    """Read native ownership without acquiring, renewing or reviving a lease."""
    native = getattr(parent, '_session_db', None)
    resolve = getattr(type(native), '_session_turn_lease_key_on_conn', None)
    if not callable(resolve):
        return False
    try:
        with trusted_chat._db(home / 'state.db') as db:
            # Resolve ancestry and its lease from one read-only snapshot. Native
            # compression children keep the original conversation's lease key.
            db.execute('BEGIN')
            key = resolve(native, db, parent.session_id)
            row = db.execute('SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?',
                             (key,)).fetchone()
            return bool(row and row['holder'] == holder
                        and type(row['expires_at']) in (int, float)
                        and math.isfinite(row['expires_at']) and row['expires_at'] > time.time())
    except (AttributeError, OSError, TypeError, ValueError, trusted_chat.sqlite3.Error):
        return False


def _coordinates(event):
    source = event.source
    from gateway.session_identity import identity_of
    identity = identity_of(source)
    if identity is None:
        return None
    return (identity.runtime_profile, str(identity.runtime_home.resolve()), identity.transport_profile,
            str(identity.authorization_home.resolve()), id(identity.adapter()), _surface(source.platform),
            source.chat_type, source.chat_id, source.user_id, source.thread_id, source.scope_id,
            event.message_id)


def _remote_audience(home, parent, event):
    from gateway.session_identity import identity_of
    from gateway.shared_conversation import binding_for_source, private_owner_source_allowed
    if (event is None or event.internal is not False or event._gateway_accepted is not True
            or event.source is None):
        return False
    source = event.source
    platform = _surface(source.platform)
    identity = identity_of(source)
    author = getattr(parent, "_turn_author", None) or {}
    if (platform not in _REMOTE_ALLOWLIST or platform != _surface(parent.platform)
            or source.chat_type != "dm" or source.is_bot is not False
            or not source.chat_id or not source.user_id or source.user_id != author.get("id")
            or source.thread_id or source.parent_chat_id or identity is None
            or identity.runtime_profile != home.name or identity.transport_profile != home.name
            or identity.runtime_home.resolve() != home or identity.authorization_home.resolve() != home
            or identity.transport_inferred or identity.adapter() is None):
        return False
    binding = binding_for_source(source)
    if (binding is None or binding["profile_home"] != str(home) or binding["profile"] != home.name):
        return False
    # Read native compression ancestry, never a caller-supplied session alias.
    with trusted_chat._db(home / "state.db") as db:
        lineage = trusted_chat._lineage(db, parent.session_id, home)
    if binding["session_id"] not in lineage:
        return False
    # Native intake already authorized this event. Private context additionally
    # requires an unambiguous, explicit sender pin; open/role/group grants do not count.
    return private_owner_source_allowed(source)


def clear_native_turn(home, context=None):
    """Revoke this session's grant in every copied context before a new turn binds."""
    home = Path(home).resolve()
    parent = _parent()
    sid = (context or {}).get("session_id") or getattr(parent, "session_id", None)
    if sid:
        with _LOCK:
            _CURRENT_BY_SESSION.pop((str(home), sid), None)


def bind_native_turn(home, context, native_receipt_or_none):
    """Bind after native attestation. Returns safe status only, never a grant token.

    Call first in pre_llm_call, even on a refused/generated turn. This does not
    enable recall or change configuration; the selected provider must consume it.
    """
    home = Path(home).resolve()
    clear_native_turn(home, context)
    parent = _parent()
    receipt = native_receipt_or_none
    if not _owned(home, parent) or not isinstance(receipt, dict):
        return {"allowed": False, "reason": "native_owner_or_receipt_missing"}
    try:
        sid, tid = context.get("session_id"), context.get("turn_id")
        holder = getattr(parent, "_active_session_turn_lease_holder", None)
        if (not sid or not tid or parent.session_id != sid or parent._current_turn_id != tid
                or not isinstance(holder, str) or not holder or not _live_turn_lease(home, parent, holder)):
            return {"allowed": False, "reason": "inactive_native_turn"}
        key = (receipt.get("session_id"), receipt.get("turn_id"))
        resolved = trusted_chat.resolve_native_turn_key(home, {key: {"receipt": receipt}}, context, agent=parent)
        if resolved != key:
            return {"allowed": False, "reason": "native_receipt_mismatch"}
        author = getattr(parent, "_turn_author", None) or {}
        if author.get("is_bot") is not False:
            return {"allowed": False, "reason": "not_direct_human"}
        platform = _surface(parent.platform)
        from gateway.delivery_attempts import current_source_event
        event = current_source_event()
        if platform in _LOCAL:
            # The existing local policy is not promoted into remote authority.
            if author.get("id") != f"local:{os.getuid()}":
                return {"allowed": False, "reason": "local_owner_unverified"}
            if event is not None and (event.internal or event.source.is_bot
                                      or _surface(event.source.platform) not in _LOCAL):
                return {"allowed": False, "reason": "local_source_mismatch"}
            coordinates = ()
        elif not _remote_audience(home, parent, event):
            return {"allowed": False, "reason": "private_audience_unverified"}
        else:
            coordinates = _coordinates(event)
        grant = _Grant(home, sid, tid, holder, weakref.ref(parent), weakref.ref(event) if event else None,
                       coordinates, platform, author["id"], receipt["id"])
        with _LOCK:
            _CURRENT_BY_SESSION[(str(home), sid)] = grant
            while len(_CURRENT_BY_SESSION) > 128:
                _CURRENT_BY_SESSION.pop(next(iter(_CURRENT_BY_SESSION)))
        return {"allowed": True, "scope": "native_current_private_owner"}
    except (AttributeError, KeyError, OSError, TypeError, ValueError, trusted_chat.sqlite3.Error):
        return {"allowed": False, "reason": "private_audience_unavailable"}


def _valid(home):
    parent = _parent()
    home = Path(home).resolve()
    if not _owned(home, parent):
        return None
    with _LOCK:
        grant = _CURRENT_BY_SESSION.get((str(home), getattr(parent, "session_id", None)))
    # Each native hook may execute in a different copied worker context. The
    # host registry is authoritative; current parent/turn/lease/event rechecks
    # below prevent a copied context from borrowing another turn's grant.
    if not isinstance(grant, _Grant) or grant.home != home or grant.parent() is not parent:
        return None
    author = getattr(parent, "_turn_author", None) or {}
    if (parent.session_id != grant.session_id or parent._current_turn_id != grant.turn_id
            or getattr(parent, "_active_session_turn_lease_holder", None) != grant.holder
            or author.get("is_bot") is not False or author.get("id") != grant.author_id
            or not _live_turn_lease(home, parent, grant.holder)):
        return None
    if grant.event is not None:
        from gateway.delivery_attempts import current_source_event
        event = current_source_event()
        if event is not grant.event():
            return None
        try:
            if grant.platform in _LOCAL:
                if (event.internal or event.source.is_bot or _surface(event.source.platform) not in _LOCAL):
                    return None
            elif grant.coordinates != _coordinates(event) or not _remote_audience(home, parent, event):
                return None
        except (AttributeError, KeyError, OSError, TypeError, ValueError, trusted_chat.sqlite3.Error):
            return None
    return grant


def allows_private_context(home, context, receipt):
    """For authenticated current-turn learned references, never public charter text."""
    grant = _valid(home)
    return bool(grant and isinstance(receipt, dict) and receipt.get("id") == grant.receipt_id
                and context.get("session_id") == grant.session_id and context.get("turn_id") == grant.turn_id)


def allows_recall(home, provider_context):
    """A current native private owner grant can lift the remote recall hold."""
    grant = _valid(home)
    c = provider_context
    return bool(grant and grant.platform in {"photon", "slack", "desktop", "cli", "tui"} and isinstance(c, dict)
                and c.get("hermesHome") == str(grant.home) and c.get("sessionId") == grant.session_id
                and c.get("turnId") == grant.turn_id and _surface(c.get("platform")) == grant.platform
                and c.get("userId") == grant.author_id and c.get("authorIsBot") is False
                and c.get("agentContext") == "primary" and not c.get("parentSessionId"))


def current_provider_source(home, base):
    """Refresh cached provider coordinates only from the host's current grant.

    Native session/history IDs stay unchanged. This is not a grant creator and
    never derives trust from model arguments or the remembered session opener.
    """
    grant = _valid(home)
    if not grant or base.get("sessionId") != grant.session_id:
        return base
    source = grant.event().source if grant.event is not None else None
    return {**base, "platform": grant.platform, "userId": grant.author_id,
            "chatId": source.chat_id if source is not None else base.get("chatId"),
            "chatType": source.chat_type if source is not None else base.get("chatType")}


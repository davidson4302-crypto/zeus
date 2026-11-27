"""Host-owned, current-turn private audience gate. Never a memory authority store.

A grant exists only for the turn that created it, and only when Zeus's one owner test (engine/zeus_hermes.py)
says the turn came from the owner. Every read re-checks the live turn: a copied context, a later turn, a helper,
a cron job or another sender never inherits it. Anything unknown answers "no grant".
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import threading

from . import trusted_chat

_LOCK = threading.RLock()
_CURRENT_BY_SESSION = {}
_LOCAL = frozenset({"cli", "tui", "desktop"})


def zeus_hermes():
    """Zeus's shared owner test (engine/zeus_hermes.py)."""
    engine = Path(__file__).resolve().parents[3] / "engine"
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    import zeus_hermes as module
    return module


@dataclass(frozen=True)
class _Grant:
    home: Path
    session_id: str
    turn_id: str
    platform: str
    author_id: str | None
    chat_id: str | None
    message_id: str | None
    receipt_id: str


def _parent():
    from agent.subagent_lifecycle import get_active_subagent_parent
    return get_active_subagent_parent()


def _surface(value):
    value = str(getattr(value, "value", value) or "").lower()
    return {"gui": "desktop", "local": "cli"}.get(value, value)


def _current_session_id():
    parent = _parent()
    return getattr(parent, "session_id", None) if parent is not None else None


def _current_turn_id():
    parent = _parent()
    return getattr(parent, "_current_turn_id", None) if parent is not None else None


def clear_native_turn(home, context=None):
    """Revoke this session's grant before a new turn binds."""
    home = Path(home).resolve()
    sid = (context or {}).get("session_id") or _current_session_id()
    if sid:
        with _LOCK:
            _CURRENT_BY_SESSION.pop((str(home), sid), None)


def bind_native_turn(home, context, native_receipt_or_none=None):
    """Bind a grant for this turn when it is the owner's. Returns safe status only, never a grant token.

    Call first in pre_llm_call, even on a refused turn."""
    home = Path(home).resolve()
    clear_native_turn(home, context)
    try:
        from hermes_constants import get_hermes_home
        if Path(get_hermes_home()).resolve() != home or home.name not in trusted_chat.scoped_profiles():
            return {"allowed": False, "reason": "other_profile"}
        zh = zeus_hermes()
        sid, tid = context.get("session_id"), context.get("turn_id")
        if not isinstance(sid, str) or not sid or not isinstance(tid, str) or not tid:
            return {"allowed": False, "reason": "inactive_native_turn"}
        if context.get("parent_session_id") or zh.turn_display_kind(context) is not None:
            return {"allowed": False, "reason": "not_direct_human"}
        author = zh.turn_author(context) or {}
        if author.get("is_bot") is True:
            return {"allowed": False, "reason": "not_direct_human"}
        owner = zh.owner_turn()
        if owner is None:
            return {"allowed": False, "reason": "not_the_owner"}
        if owner.get("session_id") and owner["session_id"] != sid:
            return {"allowed": False, "reason": "session_mismatch"}
        if _current_session_id() not in (None, sid) or _current_turn_id() not in (None, tid):
            return {"allowed": False, "reason": "inactive_native_turn"}
        receipt_id = (native_receipt_or_none or {}).get("id") if isinstance(native_receipt_or_none, dict) else None
        grant = _Grant(home, sid, tid, owner["platform"], owner.get("author_id"), owner.get("chat_id"),
                       owner.get("message_id"), receipt_id or "")
        with _LOCK:
            _CURRENT_BY_SESSION[(str(home), sid)] = grant
            while len(_CURRENT_BY_SESSION) > 128:
                _CURRENT_BY_SESSION.pop(next(iter(_CURRENT_BY_SESSION)))
        return {"allowed": True, "scope": "native_current_private_owner"}
    except Exception:
        return {"allowed": False, "reason": "private_audience_unavailable"}


def _valid(home):
    """The current turn's grant, re-checked against the live owner test; None otherwise."""
    try:
        home = Path(home).resolve()
        sid = _current_session_id()
        if not sid:
            snap = zeus_hermes().session_snapshot()
            sid = snap.get("session_id") if snap else None
        if not sid:
            return None
        with _LOCK:
            grant = _CURRENT_BY_SESSION.get((str(home), sid))
        if not isinstance(grant, _Grant) or grant.home != home:
            return None
        tid = _current_turn_id()
        if tid is not None and tid != grant.turn_id:
            return None
        owner = zeus_hermes().owner_turn()
        if (owner is None or owner["platform"] != grant.platform or owner.get("author_id") != grant.author_id
                or owner.get("message_id") != grant.message_id
                or (owner.get("session_id") and owner["session_id"] != grant.session_id)):
            return None
        return grant
    except Exception:
        return None


def allows_private_context(home, context, receipt):
    """For authenticated current-turn learned references, never public charter text."""
    grant = _valid(home)
    return bool(grant and isinstance(receipt, dict) and (not grant.receipt_id or receipt.get("id") == grant.receipt_id)
                and context.get("session_id") == grant.session_id and context.get("turn_id") == grant.turn_id)


def allows_recall(home, provider_context):
    """A current owner grant can lift the remote recall hold."""
    grant = _valid(home)
    c = provider_context
    if not grant or not isinstance(c, dict):
        return False
    zh = zeus_hermes()
    return bool(c.get("hermesHome") == str(grant.home) and c.get("sessionId") == grant.session_id
                and c.get("turnId") == grant.turn_id and _surface(c.get("platform")) == grant.platform
                and (grant.author_id is None or zh.normalize_id(c.get("userId")) == grant.author_id)
                and c.get("authorIsBot") is False
                and c.get("agentContext") == "primary" and not c.get("parentSessionId"))


def current_provider_source(home, base):
    """Refresh cached provider coordinates only from the host's current grant."""
    grant = _valid(home)
    if not grant or base.get("sessionId") != grant.session_id:
        return base
    return {**base, "platform": grant.platform, "userId": grant.author_id,
            "chatId": grant.chat_id if grant.chat_id is not None else base.get("chatId"),
            "chatType": "dm" if grant.chat_id is not None else base.get("chatType")}

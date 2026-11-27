"""Profile-scoped native human-turn receipts for conversation continuity.

Receipts label learning evidence. They grant no tool, send or action authority.
No transcript text is copied into this store. Old unlabelled history remains
searchable history; it is never retroactively labelled human from its wording.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

HUMAN_SURFACES = ("cli", "desktop", "slack", "discord", "telegram", "web", "tui", "photon", "inkbox")
SOURCE_NAMES = (*HUMAN_SURFACES, "gui", "local", "gateway_botmode")
TURN_META = frozenset({"title_preview", "gateway_input_owner", "reply_expected"})


class AdmissionError(ValueError):
    pass


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback like SQLite's context manager, then release the handle."""
    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _surface(value):
    value = str(getattr(value, "value", value) or "").lower()
    return {"gui": "desktop", "local": "cli"}.get(value, value)


def _home(home):
    raw = Path(home).expanduser()
    if raw.is_symlink():
        raise AdmissionError("Profile symlink refused")
    home = raw.resolve()
    for path in (home / "state.db", home / "plugin-data", home / "plugin-data/zeus-scope",
                 home / "plugin-data/zeus-scope/chat_admission.sqlite3"):
        if path.is_symlink():
            raise AdmissionError("Cross-profile storage symlink refused")
    return home


def _db(path):
    result = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3, factory=ClosingConnection)
    result.row_factory = sqlite3.Row
    return result


def load_effective_config(home):
    """Use Hermes's own parser/managed layer, including reused YAML anchors.

    Parser or scope failures propagate. There is deliberately no permissive
    fallback to an empty bot list.
    """
    home = _home(home)
    try:
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from gateway.config_loader import read_yaml_layers
        token = set_hermes_home_override(home)
        try:
            config = read_yaml_layers(home)
        finally:
            reset_hermes_home_override(token)
    except Exception as exc:
        raise AdmissionError("Effective profile configuration unavailable") from exc
    if not isinstance(config, Mapping):
        raise AdmissionError("Invalid effective profile configuration")
    return config


def bot_users(home, effective_config=None):
    config = load_effective_config(home) if effective_config is None else effective_config
    if not isinstance(config, Mapping):
        raise AdmissionError("Invalid effective profile configuration")
    result = set()
    platforms = config.get("platforms") or {}
    if not isinstance(platforms, Mapping):
        raise AdmissionError("Invalid platform configuration")
    for entry in platforms.values():
        if not isinstance(entry, Mapping):
            raise AdmissionError("Invalid platform entry")
        extra = entry.get("extra") or {}
        room = extra.get("bounded_shared_room") or {} if isinstance(extra, Mapping) else None
        if not isinstance(room, Mapping):
            raise AdmissionError("Invalid bot-sender configuration")
        for field in ("bot_users", "receivers"):
            values = room.get(field) or []
            if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) for v in values):
                raise AdmissionError("Invalid bot-sender list")
            result.update(v for v in values if v)
    return frozenset(result)


def human_turn(raw_meta):
    """Structural gate only. This does not authenticate a sender."""
    try:
        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
    except (ValueError, TypeError):
        return False
    return isinstance(meta, Mapping) and not (set(meta) - TURN_META)


def _metadata(value):
    if not human_turn(value):
        raise AdmissionError("Synthetic or unknown message metadata")
    return json.loads(value) if isinstance(value, str) else (value or {})


def _lineage(db, session_id, home):
    """Allow native compression ancestry, never arbitrary fork ancestry."""
    result = []
    for _ in range(64):
        if not session_id or session_id in result:
            raise AdmissionError("Invalid session lineage")
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise AdmissionError("Missing native session")
        row = dict(row)
        if row.get("profile_name") not in (None, home.name):
            raise AdmissionError("Foreign profile session")
        if row.get("source") not in SOURCE_NAMES:
            raise AdmissionError("Non-conversation native session")
        result.append(session_id)
        parent = row.get("parent_session_id")
        if not parent:
            return result
        previous = db.execute("SELECT end_reason FROM sessions WHERE id=?", (parent,)).fetchone()
        if previous is None or previous[0] != "compression":
            raise AdmissionError("Subagent or imported session")
        session_id = parent
    raise AdmissionError("Session lineage too deep")


def scoped_profiles(home_name=None):
    """Profile names this scope treats as Zeus: the configured profile plus any retained predecessor profiles
    (settings `scope.retained_profiles`). Falls back to the default profile name when settings cannot be loaded."""
    names = {"zeus"}
    try:
        import sys
        engine = Path(__file__).resolve().parents[3] / "engine"
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        import settings
        names = {settings.PROFILE, *[str(p) for p in (settings.get("scope.retained_profiles", []) or []) if p]}
    except Exception:
        pass
    return frozenset(names)


def _receipt_path(home):
    return home / "plugin-data/zeus-scope/chat_admission.sqlite3"


def certify_native_turn(home, context, *, agent=None, effective_config=None):
    """Called by a native pre_llm_call hook, before reading/learning its turn.

    The actual agent supplies author, database and current-turn identity. A model
    claim in user_message or a context flag cannot provide these attestations.
    ``agent`` is dependency injection for isolated tests/host-owned integrations;
    normal plugins omit it and resolve the native current agent.
    """
    home = _home(home)
    if agent is None:
        try:
            from agent.subagent_lifecycle import get_active_subagent_parent
            agent = get_active_subagent_parent()
        except Exception:
            return None
    if agent is None or not isinstance(context, Mapping):
        return None
    if (context.get("parent_session_id") or getattr(agent, "_parent_session_id", None)
            or getattr(agent, "_persist_disabled", False) or getattr(agent, "_turn_origin", None)
            or context.get("turn_display_kind")):
        return None
    sid, tid = context.get("session_id"), context.get("turn_id")
    if (not isinstance(sid, str) or not sid or not isinstance(tid, str) or not tid
            or getattr(agent, "session_id", None) != sid
            or getattr(agent, "_current_turn_id", None) != tid):
        return None
    native_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
    if native_path is None or Path(native_path).resolve() != home / "state.db":
        return None
    platform = _surface(getattr(agent, "platform", None))
    if platform not in HUMAN_SURFACES or platform != _surface(context.get("platform")):
        return None
    author = getattr(agent, "_turn_author", None)
    if not isinstance(author, Mapping) or author.get("is_bot") is not False:
        return None
    author_id = author.get("id")
    if (not isinstance(author_id, str) or not author_id.strip() or author_id in {"tool", "system"}
            or author_id.startswith(("bot:", "a2a:"))
            or author_id in bot_users(home, effective_config)):
        return None
    claimed = context.get("turn_author")
    if isinstance(claimed, Mapping) and (claimed.get("id") != author_id or claimed.get("is_bot") is not False):
        return None
    history = context.get("conversation_history")
    if not isinstance(history, list) or not history or not isinstance(history[-1], Mapping):
        return None
    latest = history[-1]
    if (latest.get("role") != "user" or latest.get("display_kind") or latest.get("_shared_archive")
            or latest.get("observed") or latest.get("_compressed_summary")):
        return None
    try:
        meta = _metadata(latest.get("display_metadata"))
    except AdmissionError:
        return None
    text = context.get("user_message")
    stamp = latest.get("timestamp")
    if (not isinstance(text, str) or not text.strip() or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)):
        return None
    override = getattr(agent, "_persist_user_message_override", None)
    if override is not None and override != text:
        return None
    if override is None and latest.get("content") != text:
        return None
    owner = meta.get("gateway_input_owner") or ""
    platform_id = latest.get("platform_message_id") or ""
    message_uid = latest.get("message_uid") or ""
    if any(not isinstance(v, str) for v in (owner, platform_id, message_uid)):
        return None
    try:
        with _db(home / "state.db") as db:
            ancestry = _lineage(db, sid, home)
    except (AdmissionError, sqlite3.Error):
        return None
    identity = (["gateway", owner] if owner else ["native", ancestry[-1], platform,
                platform_id or message_uid or [float(stamp), _sha(text)]])
    receipt = {
        "id": _sha(json.dumps([str(home), identity])), "profile_home": str(home),
        "session_id": sid, "turn_id": tid, "platform": platform,
        "author_sha256": _sha(author_id), "text_sha256": _sha(text),
        "message_timestamp": float(stamp), "gateway_input_owner": owner,
        "platform_message_id": platform_id, "message_uid": message_uid,
        "attested_at": time.time(),
    }
    path = _receipt_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(path, timeout=3, factory=ClosingConnection) as db:
        db.execute("CREATE TABLE IF NOT EXISTS direct_human_turns(id TEXT PRIMARY KEY, session_id TEXT, text_sha256 TEXT, message_timestamp REAL, payload TEXT)")
        db.execute("CREATE INDEX IF NOT EXISTS direct_human_lookup ON direct_human_turns(text_sha256,message_timestamp)")
        db.execute("CREATE INDEX IF NOT EXISTS direct_human_session ON direct_human_turns(session_id)")
        prior = db.execute("SELECT payload FROM direct_human_turns WHERE id=?", (receipt["id"],)).fetchone()
        if prior:
            original = json.loads(prior[0])
            mutable = {"turn_id", "session_id", "attested_at"}
            if (original["session_id"] not in ancestry or
                    any(original.get(key) != value for key, value in receipt.items() if key not in mutable)):
                raise AdmissionError("Native turn receipt changed")
            # A transport retry is the same human input, never a second lesson.
            return original
        db.execute("INSERT OR IGNORE INTO direct_human_turns VALUES (?,?,?,?,?)", (
            receipt["id"], sid, receipt["text_sha256"], receipt["message_timestamp"], json.dumps(receipt, sort_keys=True)))
    path.chmod(0o600)
    return receipt


def resolve_native_turn_key(home, keys, context, *, agent=None, effective_config=None):
    """Resolve an existing host-owned turn key across native compression only.

    Return the original key without changing its source, text or authority. Keys
    may be a mapping of keys to existing source/authorization records or an
    iterable of keys. A durable direct-human receipt must match the actual native
    author's hash. This is a lookup repair, never a new grant of human authority.
    """
    home = _home(home)
    if agent is None:
        try:
            from agent.subagent_lifecycle import get_active_subagent_parent
            agent = get_active_subagent_parent()
        except Exception:
            return None
    if agent is None or not isinstance(context, Mapping):
        return None
    sid = context.get("session_id") or getattr(agent, "session_id", None)
    tid = context.get("turn_id") or getattr(agent, "_current_turn_id", None)
    if (not sid or not tid or getattr(agent, "session_id", None) != sid
            or getattr(agent, "_current_turn_id", None) != tid
            or getattr(agent, "_parent_session_id", None) or context.get("parent_session_id")
            or getattr(agent, "_persist_disabled", False) or getattr(agent, "_turn_origin", None)
            or context.get("turn_display_kind")):
        return None
    native_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
    if native_path is None or Path(native_path).resolve() != home / "state.db":
        return None
    if _surface(getattr(agent, "platform", None)) not in HUMAN_SURFACES:
        return None
    author = getattr(agent, "_turn_author", None)
    if not isinstance(author, Mapping) or author.get("is_bot") is not False:
        return None
    author_id = author.get("id")
    if (not isinstance(author_id, str) or not author_id.strip() or author_id in {"tool", "system"}
            or author_id.startswith(("bot:", "a2a:")) or author_id in bot_users(home, effective_config)):
        return None
    try:
        with _db(home / "state.db") as db:
            ancestry = _lineage(db, sid, home)
    except (AdmissionError, OSError, sqlite3.Error):
        return None
    path = _receipt_path(home)
    if not path.is_file():
        return None
    candidates = list(keys.items()) if isinstance(keys, Mapping) else [(key, {}) for key in keys]
    matched = []
    with _db(path) as db:
        for key, entry in candidates:
            if (not isinstance(key, tuple) or len(key) != 2 or key[1] != tid
                    or key[0] not in ancestry or entry is None):
                continue
            entry = entry if isinstance(entry, Mapping) else {}
            if (entry.get("role", "user") != "user" or entry.get("origin", "direct") != "direct"):
                continue
            supplied = entry.get("receipt")
            if isinstance(supplied, Mapping) and supplied.get("id"):
                rows = db.execute("SELECT payload FROM direct_human_turns WHERE id=?", (supplied["id"],)).fetchall()
            else:
                rows = db.execute("SELECT payload FROM direct_human_turns WHERE session_id=?", (key[0],)).fetchall()
            valid = []
            for row in rows:
                receipt = json.loads(row[0])
                if (receipt.get("profile_home") != str(home) or receipt.get("session_id") != key[0]
                        or receipt.get("author_sha256") != _sha(author_id)):
                    continue
                if supplied:
                    if receipt != supplied:
                        continue
                elif receipt.get("turn_id") != tid:
                    continue
                if isinstance(entry.get("text"), str) and _sha(entry["text"]) != receipt.get("text_sha256"):
                    continue
                valid.append(receipt)
            if len(valid) == 1:
                matched.append(key)
    return matched[0] if len(matched) == 1 else None


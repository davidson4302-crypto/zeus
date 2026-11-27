"""Native hooks and one bounded tool. No scheduler, sender, model or personal store."""
from __future__ import annotations

import json
import threading
import time

from .engine import Ledger, digest
from .actions import Extensions, native_cron

READ_TOOLS = {"web_search", "web_extract", "web_fetch", "read_file", "session_search", "um_memory", "zeus_work", "skills_list", "skill_view"}
GUIDANCE = (
    "Use zeus_insight for sourced ideas, checked preparation and procedural lessons. Personal facts stay in retained memory. "
    "Use zeus_work for durable work; bounded quiet review inherits approved parent scope. Track adoption, use and outcome separately. "
    "Package only evidenced procedures through native skills. Current-chat suggestions are enabled with topic mute, urgency and quiet controls; mute never means work STOP. "
    "Routines need current explicit enablement and use native cron. External sends need their own authority. Private native scope gates insight reads. Scheduled, performed and delivered differ."
)

SCHEMA = {"name": "zeus_insight", "description": (
    "Private derived preparation and procedural learning. sources lists observed native receipts; "
    "propose records idea/lesson/briefing/feed/memory_review/routine with citations, expiry and transparent ranking; "
    "transition accepts/dismisses/adopts/retires, never executes; artifact checks actual file; attention only decides "
    "current reply suitability, never sends; use/outcome verifies native zeus_work; promote requires two outcomes. "
    "package_skill/retire_skill use profile-local native skills. quiet_review_start derives bounded local work from an approved parent. "
    "routine_enable/pause/resume/status use canonical native cron, with current explicit instruction and separate outward delivery authority. Personal facts stay in um_memory."
), "parameters": {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["sources", "propose", "list", "get", "transition", "artifact", "attention", "policy", "work_plan", "use", "outcome", "promote", "package_skill", "skill_status", "retire_skill", "quiet_reviews", "quiet_review_start", "routine_plan", "routine_enable", "routine_pause", "routine_resume", "routine_status", "routine_result", "audit"]},
    "id": {"type": "string"}, "kind": {"type": "string", "enum": ["idea", "lesson", "briefing", "feed", "memory_review", "routine"]},
    "title": {"type": "string"}, "guidance": {"type": "string", "description": "Derived task/procedure guidance, never copied personal facts."},
    "scope": {"type": "string"}, "source_ids": {"type": "array", "items": {"type": "string"}},
    "topic": {"type": "string", "description": "Exact topic label used for explicit mute/unmute."},
    "urgency": {"type": "object", "properties": {"deadline": {"type": "string"}, "source_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["deadline", "source_id", "reason"], "additionalProperties": False},
    "expires_at": {"type": "string", "description": "ISO timestamp with timezone; freshness limit, not a reminder."},
    "correction_quote": {"type": "string", "description": "Exact current native instruction words; only its hash is retained. Local CLI provenance is local_operator, never a fictional human-author claim."},
    "ranking": {"type": "object", "properties": {"relevance": {"type": "integer", "minimum": 0, "maximum": 5}, "feasibility": {"type": "integer", "minimum": 0, "maximum": 5}, "effort_saved": {"type": "integer", "minimum": 0, "maximum": 5}, "reason": {"type": "string"}}, "required": ["relevance", "feasibility", "effort_saved", "reason"], "additionalProperties": False},
    "query": {"type": "string"}, "state": {"type": "string", "enum": ["accepted", "dismissed", "expired", "adopted", "retired"]},
    "reason": {"type": "string", "description": "Acceptance/adoption/policy needs exact current admitted native instruction quote; other decisions need a reason."},
    "path": {"type": "string"}, "checks": {"type": "array", "items": {"type": "string"}, "description": "Required literal artifact content. Every source ID must also appear. These are mechanical checks, not quality judgment."},
    "work_id": {"type": "string"}, "evidence_source_id": {"type": "string"},
    "execution_id": {"type": "string", "description": "Exact native routine occurrence for private result retrieval; omit for latest observed meaningful final."},
    "requested_reply_quote": {"type": "string", "description": "Exact current admitted native request for this result; bypasses proactive mute only for this reply."},
    "settings": {"type": "object", "properties": {"muted": {"type": "boolean"}, "muted_topics": {"type": "array", "items": {"type": "string"}}, "urgent_within_hours": {"type": "integer", "minimum": 0, "maximum": 12}}, "additionalProperties": False},
    "routine": {"type": "object", "properties": {"schedule": {"type": "string"}, "window": {"type": "string"}, "input_window_hours": {"type": "integer", "minimum": 1, "maximum": 720}, "delivery": {"type": "string", "enum": ["local", "origin"]}, "repeat": {"type": "integer", "minimum": 1, "maximum": 1000}}, "required": ["schedule", "delivery"], "additionalProperties": False},
    "delivery_quote": {"type": "string", "description": "Exact current instruction authorizing routine output to this private remote chat. Required for origin delivery; local saves only."},
}, "required": ["action"], "additionalProperties": False}}


def register(ctx):
    # Maps only the current native turn. It contains no cross-profile persisted author cache.
    turns = {}
    lock = threading.RLock()

    def home():
        from hermes_constants import get_hermes_home
        return str(get_hermes_home().resolve())

    def ledger():
        from plugins.plugin_storage import plugin_db
        return Ledger(plugin_db("zeus-insight"))

    def private(session_id):
        try:
            from zeus_scope import allowed_private
            return bool(allowed_private(home(), session_id=session_id))
        except Exception:
            return False

    def current(session_id):
        with lock:
            value = turns.get((home(), str(session_id)))
        if not value or time.monotonic() - value["seen"] > 1800 or not private(session_id):
            raise ValueError("No current admitted private native turn; private insight access is closed")
        from zeus_scope import current_source
        native = current_source(home())
        if not native or native.get("turn_id") != value["turn_id"] or native.get("profile_home") != home():
            raise ValueError("Native turn changed; stale insight context refused")
        return value

    def pre_llm_call(session_id=None, user_message=None, platform=None, turn_author=None,
                     turn_display_kind=None, parent_session_id=None, **kwargs):
        sid = str(session_id or "")
        platform = str(getattr(platform, "value", platform) or "").lower()
        value = {"session_id": sid, "home": home(), "platform": platform, "seen": time.monotonic()}
        if not sid or not private(sid):
            with lock:
                turns.pop((home(), sid), None)
            return None
        from zeus_scope import current_source
        native = current_source(home())
        if not native or native.get("session_id") != sid or native.get("profile_home") != home():
            return None
        value["turn_id"] = native["turn_id"]
        value["message"] = str(native.get("text") or "")
        value["origin"] = str(native.get("origin") or "native_unknown")
        value["human_author_claimed"] = bool(
            value["origin"] == "direct" and native.get("author_id")
            or value["origin"] == "desktop_submit" and (native.get("receipt") or {}).get("human_author_claimed") is True)
        db = ledger()
        try:
            source_kind = {"direct": "owner_turn", "local_operator": "local_operator_turn", "desktop_submit": "desktop_submit_turn"}.get(value["origin"], "native_turn")
            value["source_id"] = db.source(source_kind, f"native-{value['origin']}:session:{sid}/turn:{value['turn_id']}", digest(value["message"]), sid)
            value["observed_sources"] = [value["source_id"]]
            with lock:
                # Bound process memory even if a gateway never emits session-end.
                for key in list(turns):
                    if time.monotonic() - turns[key]["seen"] > 1800:
                        del turns[key]
                if len(turns) >= 128:
                    turns.pop(min(turns, key=lambda key: turns[key]["seen"]))
                turns[(home(), sid)] = value
            # Keep the authenticated native receipt even for a greeting. Only discretionary context is skipped.
            import sys
            reader = sys.modules.get("zeus_turnctx")
            if reader and getattr(reader, "social_ping", lambda *args, **kw: False)(
                    home(), user_message=value["message"], platform=platform, session_id=sid,
                    turn_author=turn_author, turn_display_kind=turn_display_kind, parent_session_id=parent_session_id, **kwargs):
                return None
            candidates = db.listing(value["message"], 2, states=("adopted",))
            context = [{"id": item["id"], "scope": item["scope"], "guidance": item["guidance"][:1000],
                        "native_skill": (item.get("native_skill") or {}).get("name"),
                        "sources": item["source_ids"], "status": "adopted procedural guidance, not a personal fact"}
                       for item in candidates]
            pending = [{"id": row["id"], "parent_id": row["parent_id"], "state": row["state"]} for row in db.quiet_reviews() if row["state"] in {"pending", "needs_reconciliation"}][:2]
            if not context and not pending:
                return None
            return {"context": "Private procedural context. Handle the current user request first; do not recite internal review. " + json.dumps({"guidance": context, "quiet_review_intents": pending})}
        finally:
            db.db.close()

    def post_tool_call(tool_name=None, args=None, result=None, session_id=None, tool_call_id=None, **kwargs):
        if tool_name not in READ_TOOLS:
            return None
        try:
            turn = current(session_id)
        except ValueError:
            return None
        raw = result if isinstance(result, str) else json.dumps(result, sort_keys=True, default=str)
        # A failure is useful audit evidence, but is never a research receipt.
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict) and (decoded.get("error") or decoded.get("isError") or decoded.get("ok") is False):
            return None
        db = ledger()
        try:
            source_id = db.source("native_tool", f"tool:{tool_name}/call:{tool_call_id or digest(raw)[:20]}", digest(raw), turn["session_id"])
            with lock:
                turn["observed_sources"] = list(dict.fromkeys(turn.get("observed_sources", []) + [source_id]))[-12:]
                work = decoded.get("result", decoded) if isinstance(decoded, dict) else None
                if tool_name == "zeus_work" and isinstance(work, dict) and work.get("id") and work.get("revision"):
                    source = work.get("source") or {}
                    if source.get("session_id") == turn["session_id"] and source.get("turn_id") == turn["turn_id"] and not work.get("derivation") and source.get("origin") != "derived_preparation":
                        turn["parent_work"] = {key: work.get(key) for key in ("id", "revision", "state")}
        finally:
            db.db.close()

    def post_llm_call(session_id=None, assistant_response=None, **kwargs):
        if kwargs.get("platform") == "cron":
            db = ledger()
            try:
                Extensions(ctx, db, home()).observe_routine_final({"session_id": session_id, **kwargs}, assistant_response)
            finally:
                db.db.close()
            return None
        # A prior admitted private turn may finish after its scope lease closes.
        # This local completion map can only write response hashes, never expose data.
        with lock:
            turn = turns.get((home(), str(session_id)))
        if not turn:
            return None
        response = str(assistant_response or "")
        db = ledger()
        try:
            # A native final is a return prepared. Transport delivery remains separately unproven.
            for item in db.listing("", 50):
                artifact = item.get("artifact")
                if artifact and artifact["path"] in response and db.artifact_current(item):
                    db.event(item["id"], "return_prepared", {"session_id": turn["session_id"],
                             "response_sha256": digest(response), "delivery_state": "not_proven"})
            db.db.commit()
            intent = db.queue_quiet_review(turn, response, ctx.get_config("quiet_review.timezone", None))
            if intent and intent["state"] == "pending" and ctx.get_config("quiet_review.enabled", True):
                try:
                    Extensions(ctx, db, home()).quiet_start(intent["id"])
                except Exception:
                    # Saved intent/result retains the exact limitation; no user-facing routine chatter.
                    pass
        finally:
            db.db.close()
            with lock:
                turns.pop((home(), str(session_id)), None)

    def on_session_end(session_id=None, **kwargs):
        with lock:
            turns.pop((home(), str(session_id)), None)

    def routine_guard(tool_name, args, next_call, **context):
        if not str(context.get("task_id") or "").startswith("cron:"):
            return next_call(args)
        db = ledger()
        try:
            return Extensions(ctx, db, home()).routine_guard(tool_name, args, next_call, context)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)[:300], "blocked_by": "zeus-insight routine scope"})
        finally:
            db.db.close()

    def routine_transform(response_text="", **context):
        if not str(context.get("task_id") or "").startswith("cron:"):
            return None
        db = ledger()
        try:
            return Extensions(ctx, db, home()).routine_transform(context)
        finally:
            db.db.close()

    def pre_tool_call(tool_name=None, args=None, session_id=None, **kwargs):
        if tool_name != "skill_view" or not str((args or {}).get("name", "")).startswith("zeus-learned-"):
            return None
        if not private(session_id):
            return {"action": "block", "message": "This learned procedure requires a current admitted private native turn."}
        db = ledger()
        try:
            for row in db.db.execute("SELECT id,body FROM insights ORDER BY created_at DESC LIMIT 500").fetchall():
                body = json.loads(row["body"])
                if (body.get("native_skill") or {}).get("name") == args["name"]:
                    item = db.get(row["id"])
                    if item["state"] == "adopted" and (item.get("native_skill") or {}).get("state") != "retired":
                        return None
                    return {"action": "block", "message": "The learned procedure is expired or retired; do not use it."}
            return {"action": "block", "message": "No owned current procedure receipt matches this skill."}
        except Exception:
            return {"action": "block", "message": "The learned procedure could not be validated."}
        finally:
            db.db.close()

    def handle(args, session_id=None, **kwargs):
        try:
            turn = current(session_id)
            db = ledger()
            try:
                action = args.get("action")
                ident = args.get("id")
                if action == "sources":
                    result = {"sources": db.sources(turn["session_id"]), "content_retained": False}
                elif action == "propose":
                    result = db.propose(args, turn)
                elif action == "list":
                    result = {"items": db.listing(args.get("query", "")), "new_work_started": False}
                elif action == "get":
                    result = db.get(ident)
                elif action == "transition":
                    result = db.transition(ident, args.get("state"), turn, args.get("reason", ""))
                elif action == "artifact":
                    result = db.artifact(ident, args.get("path", ""), args.get("checks", []), ctx.get_config("artifact_roots", []))
                elif action == "attention":
                    quote = str(args.get("requested_reply_quote", "")).strip()
                    if quote and quote not in turn["message"]:
                        raise ValueError("Requested-reply quote is not in the admitted native current message")
                    result = db.attention(ident, turn, requested=len(quote) >= 3)
                elif action == "policy":
                    result = db.set_policy(args["settings"], turn, args.get("reason", "")) if "settings" in args else db.policy()
                elif action == "work_plan":
                    item = db.get(ident)
                    if item["state"] != "accepted":
                        raise ValueError("Accept the proposal before preparing a durable work request")
                    result = {"tool": "zeus_work", "arguments": {"action": "create", "title": item["title"],
                              "criteria": [{"id": "prepared", "description": "Produce and verify a source-linked artifact for: " + item["scope"],
                                            "artifact": "prepared.md", "checks": [{"kind": "contains", "value": sid} for sid in item["source_ids"]]}],
                              "steps": [{"id": "prepare", "description": item["guidance"], "depends_on": [], "criteria": ["prepared"]}],
                              "origin_source_ref": "zeus-insight:" + ident},
                              "executed": False, "note": "Tailor criteria to the entire actual requested outcome. Native work engine must independently bind current owner authority."}
                elif action in {"use", "outcome"}:
                    raw = ctx.dispatch_tool("zeus_work", {"action": "status", "id": args.get("work_id")})
                    work = json.loads(raw) if isinstance(raw, str) else raw
                    if not isinstance(work, dict) or work.get("error") or work.get("ok") is False:
                        raise ValueError("Native work status unavailable")
                    # Native tool may wrap its work object in a result envelope.
                    work = work.get("work", work.get("result", work))
                    result = db.learning_event(ident, action, work, args.get("evidence_source_id"))
                elif action == "promote":
                    result = db.promote(ident)
                elif action in {"package_skill", "skill_status", "retire_skill", "quiet_reviews", "quiet_review_start", "routine_enable", "routine_pause", "routine_resume", "routine_status", "routine_result"}:
                    ext = Extensions(ctx, db, home())
                    if action == "package_skill": result = ext.package_skill(ident, turn, args.get("reason"))
                    elif action == "skill_status": result = ext.skill_status(ident)
                    elif action == "retire_skill": result = ext.retire_skill(ident, turn, args.get("reason"))
                    elif action == "quiet_reviews": result = db.quiet_reviews(ident)
                    elif action == "quiet_review_start": result = ext.quiet_start(ident)
                    elif action == "routine_enable": result = ext.routine_enable(ident, turn, args.get("reason"), args.get("routine", {}), args.get("delivery_quote"))
                    elif action == "routine_pause": result = ext.routine_pause(ident, turn, args.get("reason"))
                    elif action == "routine_resume": result = ext.routine_resume(ident, turn, args.get("reason"))
                    elif action == "routine_result": result = ext.routine_result(ident, args.get("execution_id"))
                    else: result = ext.routine_status(ident)
                elif action == "routine_plan":
                    item = db.get(ident)
                    quote = str(args.get("reason", "")).strip()
                    if item["kind"] != "routine" or item["state"] != "accepted" or len(quote) < 3 or quote not in turn["message"]:
                        raise ValueError("Routine plan needs accepted proposal and current explicit owner instruction")
                    routine = args.get("routine", {})
                    if not all(isinstance(routine.get(k), str) and 0 < len(routine[k]) <= 300 for k in ("schedule", "delivery")) or not (routine.get("window") or routine.get("input_window_hours")):
                        raise ValueError("Routine needs explicit schedule, input window and delivery")
                    result = {"routine_plan": routine, "enabled": False, "installed": False,
                              "next": "Use native scheduler only after binding current owner instruction and verifying delivery scope; never revive archive schedules."}
                    db.event(ident, "routine_plan_prepared", {"plan": routine, "native_source": turn["source_id"], "origin": turn["origin"], "human_author_claimed": turn["human_author_claimed"], "enabled": False})
                    db.db.commit()
                elif action == "audit":
                    db.get(ident)
                    result = {"events": [dict(row) for row in db.db.execute(
                        "SELECT id,kind,detail,created_at FROM events WHERE insight_id=? ORDER BY created_at LIMIT 100", (ident,))]}
                else:
                    raise ValueError("Unknown insight action")
                return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
            finally:
                db.db.close()
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return json.dumps({"ok": False, "error": str(exc)[:600]})

    ctx.register_tool(name="zeus_insight", toolset="zeus_insight", schema=SCHEMA, handler=handle)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_llm_call", post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("transform_llm_output", routine_transform)
    ctx.register_middleware("tool_execution", routine_guard)
    ctx.register_system_prompt_section("zeus-insight.usage", GUIDANCE, max_chars=650)

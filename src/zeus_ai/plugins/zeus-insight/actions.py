"""Native adapters for reviewed preparation, learned skills and intentional routines."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re

from .engine import Ledger, digest, now, timestamp

CONTRACT_KEYS = ("prompt", "script", "schedule", "origin", "deliver", "skills", "context_from", "enabled_toolsets", "workdir", "no_agent")


def decode(raw):
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict) or value.get("error") or value.get("success") is False or value.get("ok") is False:
        raise ValueError("Native operation did not return a successful receipt; reconcile the saved intent before retrying")
    return value


def dispatch(ctx, tool, args):
    return decode(ctx.dispatch_tool(tool, args))


def instruction(current, quote):
    if not isinstance(quote, str) or len(quote.strip()) < 3 or quote not in current["message"]:
        raise ValueError("Exact current admitted native instruction required")
    return {"source_id": current["source_id"], "origin": current["origin"],
            "human_author_claimed": current["human_author_claimed"], "quote_sha256": digest(quote)}


@contextmanager
def native_cron(home):
    # Same native profile/store seam used by zeus-work, not a parallel timer.
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from cron import jobs
    selected = set_hermes_home_override(Path(home).resolve())
    try:
        with jobs.use_cron_store(Path(home).resolve()):
            yield jobs
    finally:
        reset_hermes_home_override(selected)


def skill_create_root(home):
    from agent.skill_utils import get_skill_create_dir
    root = (get_skill_create_dir() or Path(home) / "skills").resolve()
    if not root.is_relative_to(Path(home).resolve()):
        raise ValueError("Native skills.create_dir resolves outside this profile; use a profile-local learned-skills directory")
    return root


def skill_content(item, name):
    sources = "\n".join("- " + source for source in item["source_ids"])
    works = "\n".join("- " + event["work_id"] for event in item["promotion_evidence"])
    return ("---\nname: " + name + "\ndescription: " + json.dumps("Load a source-linked Zeus procedure in an admitted private conversation when zeus_insight identifies it as relevant.") + "\n---\n\n"
            "# Evidenced Zeus procedure\n\n"
            "Before using this procedure, call zeus_insight get with id " + item["id"] +
            ". Continue only while it is adopted and current. A retired or expired procedure is not authority.\n\n"
            "## Procedure\n\nRead the returned scope, guidance and evidence. Apply that version only when the current task matches its scope. "
            "Record zeus_insight use before completing the active zeus_work assignment, and outcome only after its actual artifact passes. "
            "Keep the original task's criteria and check final bytes. If private admission fails, stop loading this procedure. "
            "This skill grants no send, spend, access or personal-fact authority and copies no private guidance into the global skill index.\n\n"
            "## Provenance\n\nVersion: " + str(item["procedure_version"]) + "\n\nSource receipts:\n" + sources +
            "\n\nSuccessful prior work:\n" + works + "\n")


class Extensions:
    def __init__(self, ctx, ledger, home):
        self.ctx, self.db, self.home = ctx, ledger, Path(home).resolve()

    def quiet_start(self, ident):
        rows = self.db.quiet_reviews(ident)
        if not rows:
            raise ValueError("Unknown quiet-review intent")
        row = rows[0]
        prior = row["result"]
        if prior.get("work_id"):
            result = dispatch(self.ctx, "zeus_work", {"action": "status", "id": prior["work_id"]})["result"]
            if result.get("state") not in {"completed", "stopped"} and not result.get("schedule"):
                schedule = dispatch(self.ctx, "zeus_work", {"action": "schedule", "id": prior["work_id"]})["result"]
                prior["schedule"] = {key: schedule.get(key) for key in ("job_id", "state", "next_run_at")}
                self.db.quiet_update(ident, "scheduled", prior)
            return {"intent": row, "work": result, "performed": result.get("state") == "completed" and result.get("completion_current") is True}
        criteria = [{"id": "review", "description": "Produce a concise source-linked review of the exact parent outcome, remaining work and evidenced corrections; no invented facts or general goal expansion.",
                     "artifact": "review.md", "checks": [{"kind": "contains", "value": row["parent_id"]}, {"kind": "contains", "value": "Source receipts"}]}]
        args = {"action": "create_derived", "parent_id": row["parent_id"], "intent_digest": row["intent_digest"],
                "kind": "quiet_review", "title": "Bounded review of " + row["parent_id"], "criteria": criteria,
                "steps": [{"id": "review", "depends_on": [], "criteria": ["review"],
                           "description": "Read the frozen parent context through own status and read_parent_artifact. Review only substantial new decisions, unresolved work and potential procedural lessons. Preserve uncertainty; personal changes stay with retained memory. Write review.md with parent ID, source receipts " + ", ".join(row["source_ids"]) + ". Do not send a review-complete message or create unrelated work."}],
                "origin_source_ref": "zeus-insight:quiet:" + ident}
        self.db.quiet_update(ident, "delegation_attempted", {"args_sha256": digest(json.dumps(args, sort_keys=True)), "performed": False})
        try:
            created = dispatch(self.ctx, "zeus_work", args)["result"]
            result = {"work_id": created["id"], "parent_id": row["parent_id"], "performed": False}
            # Save canonical ID before a separate scheduling operation can fail.
            self.db.quiet_update(ident, "delegated", result)
            if created.get("state") not in {"completed", "stopped"}:
                schedule = dispatch(self.ctx, "zeus_work", {"action": "schedule", "id": created["id"]})["result"]
                result["schedule"] = {key: schedule.get(key) for key in ("job_id", "state", "next_run_at")}
                self.db.quiet_update(ident, "scheduled", result)
            return result
        except Exception as exc:
            # The work contract deduplicates exact parent/revision/intent. Never mint a new intent on retry.
            saved = self.db.quiet_reviews(ident)[0]
            self.db.quiet_update(ident, "needs_reconciliation", {**saved["result"], "reason": str(exc)[:300], "performed": False})
            raise

    def package_skill(self, ident, current, quote):
        authority = instruction(current, quote)
        item = self.db.get(ident)
        if item["kind"] != "procedure" or item["state"] != "adopted" or len(item.get("promotion_evidence", [])) < 2:
            raise ValueError("Package only a current adopted procedure with two distinct successful work outcomes")
        for proof in item["promotion_evidence"]:
            work = dispatch(self.ctx, "zeus_work", {"action": "status", "id": proof["work_id"]})["result"]
            if work.get("state") != "completed" or work.get("completion_current") is not True:
                raise ValueError("Prior procedure evidence is no longer verified current work")
        name = "zeus-learned-" + ident.replace("-", "")[:16] + "-v" + str(item["procedure_version"])
        content = skill_content(item, name)
        target = skill_create_root(self.home) / name / "SKILL.md"
        saved = item.get("native_skill")
        if target.exists() and (not saved or saved.get("sha256") != digest(content) or target.read_text() != content):
            raise ValueError("Skill path exists without matching own intent; never edit or adopt a foreign skill")
        item["native_skill"] = {"name": name, "path": str(target), "sha256": digest(content), "state": "create_intent", "authority": authority}
        self.db.save(item)
        self.db.event(ident, "skill_create_intent", item["native_skill"])
        self.db.db.commit()
        if not target.exists():
            dispatch(self.ctx, "skill_manage", {"operations": [{"action": "create", "name": name, "content": content}]})
        return self.skill_status(ident)

    def skill_status(self, ident):
        item = self.db.get(ident)
        skill = item.get("native_skill")
        if not skill:
            raise ValueError("No native skill intent exists")
        catalog = dispatch(self.ctx, "skills_list", {})
        present = any(row.get("name") == skill["name"] for row in catalog.get("skills", []))
        if skill.get("state") == "retired":
            return {"name": skill["name"], "retired": True, "discoverable": present, "executed": False}
        viewed = dispatch(self.ctx, "skill_view", {"name": skill["name"]})
        target = Path(skill["path"])
        if not present or not target.is_file() or not target.resolve().is_relative_to(self.home) or digest(target.read_bytes()) != skill["sha256"] or Path(str(viewed.get("_source_path") or Path(str(viewed.get("skill_dir"))) / "SKILL.md")).resolve() != target.resolve():
            raise ValueError("Native skill discovery/path/content differs from the saved intent")
        skill.update(state="discoverable", checked_at=now(), catalog_receipt_sha256=digest(json.dumps(catalog, sort_keys=True)), view_receipt_sha256=digest(json.dumps(viewed, sort_keys=True)))
        self.db.save(item)
        self.db.event(ident, "skill_discovered", {"name": skill["name"], "path": str(target), "sha256": skill["sha256"], "executed": False})
        self.db.db.commit()
        return {"name": skill["name"], "path": str(target), "sha256": skill["sha256"], "discoverable": True, "executed": False}

    def retire_skill(self, ident, current, quote):
        authority = instruction(current, quote)
        item = self.db.get(ident)
        skill = item.get("native_skill")
        if not skill:
            raise ValueError("No owned native skill exists")
        target = Path(skill["path"])
        if target.exists():
            if not target.resolve().is_relative_to(self.home) or digest(target.read_bytes()) != skill["sha256"]:
                raise ValueError("Skill changed or is outside this profile; do not delete it")
            if {path.name for path in target.parent.iterdir()} != {"SKILL.md"}:
                raise ValueError("Skill directory has additional files; do not retire someone else's additions")
            # The explicit retire operation removes only this generated profile-local skill.
            dispatch(self.ctx, "skill_manage", {"operations": [{"action": "delete", "name": skill["name"]}]})
        catalog = dispatch(self.ctx, "skills_list", {})
        if target.exists() or any(row.get("name") == skill["name"] for row in catalog.get("skills", [])):
            raise ValueError("Native skill retirement has not been independently observed")
        item["state"] = "retired"
        skill.update(state="retired", retired_at=now())
        self.db.save(item)
        self.db.event(ident, "skill_retired", {"name": skill["name"], "authority": authority, "original_recipe_retained": True})
        self.db.db.commit()
        return {"retired": True, "name": skill["name"], "original_procedure_and_provenance_retained": True}

    def routine_enable(self, ident, current, quote, settings, delivery_quote=None):
        authority = instruction(current, quote)
        item = self.db.get(ident)
        if item["kind"] != "routine" or item["state"] != "accepted":
            raise ValueError("An accepted current routine proposal is required")
        schedule = str(settings.get("schedule") or "").strip()
        hours = settings.get("input_window_hours")
        deliver = settings.get("delivery", "local")
        if not schedule or len(schedule) > 150 or type(hours) is not int or not 1 <= hours <= 720 or deliver not in {"local", "origin"}:
            raise ValueError("Require native schedule, input_window_hours 1..720 and delivery local/origin")
        if deliver == "origin":
            instruction(current, delivery_quote)
            from zeus_scope import current_source
            native = current_source(str(self.home))
            if not native or not native.get("chat_id") or native.get("origin") != "direct":
                raise ValueError("Outward routine delivery needs the current verified private remote chat and explicit current delivery instruction")
            authority["native_route"] = {"platform": native["platform"], "chat_id": native["chat_id"]}
        repeat = settings.get("repeat")
        if repeat is not None and (type(repeat) is not int or not 1 <= repeat <= 1000):
            raise ValueError("repeat must be 1..1000 when specified")
        name = "zeus-routine:" + ident
        prompt = ("Run only the intentionally enabled Zeus routine described below. Native script data binds this occurrence and its input window. "
                  "If wakeAgent is false, do no work. Read only sources inside input_start/input_end; show uncertainty and cite actual sources. "
                  "This grant does not authorize unrelated writes, purchases or new messages. Use the native final response only. "
                  "Avoid repeats from previous output. No empty routine-complete chatter.\n\nRoutine: " + item["title"] + "\nScope: " + item["scope"] +
                  "\nRequested work: " + item["guidance"] + "\nSource receipts: " + ", ".join(item["source_ids"]))
        script, script_hash = self._routine_script(ident)
        args = {"action": "create", "name": name, "schedule": schedule, "prompt": prompt, "deliver": deliver,
                "failure_deliver": "local", "paused": True, "paused_reason": "Zeus routine intent awaiting exact native reconciliation",
                "continuity": True, "script": str(script), "enabled_toolsets": ["web", "file"], "attach_to_session": deliver == "origin"}
        if repeat is not None:
            args["repeat"] = repeat
        intent_hash = digest(json.dumps([args, hours], sort_keys=True))
        saved = item.get("native_routine")
        if saved and saved.get("intent_sha256") != intent_hash:
            raise ValueError("Existing routine intent differs; pause and create a materially revised proposal instead of silently replacing it")
        if not saved:
            saved = {"name": name, "intent_sha256": intent_hash, "input_window_hours": hours, "state": "create_intent",
                     "script_sha256": script_hash, "script": str(script), "authority": authority,
                     "delivery_authorized": deliver, "catch_up": "native schedule policy; each occurrence uses its scheduled instant, never replay a previous result"}
            item["native_routine"] = saved
            self.db.save(item)
            self.db.event(ident, "routine_create_intent", saved)
            self.db.db.commit()
        with native_cron(self.home) as jobs:
            found = [job for job in jobs.list_jobs(include_disabled=True) if job.get("name") == name]
        if len(found) > 1:
            raise ValueError("Multiple native jobs share this exact intent; reconcile rather than recreate")
        if not found:
            if saved.get("state") == "create_attempted":
                raise ValueError("A prior create was uncertain; absence of a current match does not authorize retry")
            saved["state"] = "create_attempted"
            self.db.save(item)
            self.db.db.commit()
            dispatch(self.ctx, "cronjob_manage", args)
            with native_cron(self.home) as jobs:
                found = [job for job in jobs.list_jobs(include_disabled=True) if job.get("name") == name]
        if len(found) != 1:
            raise ValueError("Native job creation is not reconciled")
        job = found[0]
        if job.get("prompt") != prompt or job.get("script") != str(script) or job.get("deliver") != deliver:
            raise ValueError("Native job differs from frozen routine intent")
        if deliver == "origin" and any(str((job.get("origin") or {}).get(key)) != str(value) for key, value in authority["native_route"].items()):
            raise ValueError("Native delivery origin differs from the current admitted private route; job stays paused")
        if saved.get("state") in {"paused", "enabled"} and not job.get("enabled"):
            raise ValueError("Existing routine is paused or finished; use routine_resume with an explicit current resume instruction")
        saved.update(job_id=job["id"], state="acknowledged", contract={key: job.get(key) for key in CONTRACT_KEYS})
        saved["state"] = "enabled"
        item["routine_enabled"] = True
        self.db.save(item)
        self.db.db.commit()
        if not job.get("enabled"):
            dispatch(self.ctx, "cronjob_manage", {"action": "resume", "job_id": job["id"]})
        self.db.event(ident, "routine_enabled", {"job_id": job["id"], "executed": False})
        self.db.db.commit()
        return self.routine_status(ident)

    def _routine_script(self, ident):
        import hermes_bootstrap
        native_root = str(Path(hermes_bootstrap.__file__).resolve().parent)
        package_root = Path(__file__).resolve().parent
        path = self.home / "scripts" / ("zeus_routine_" + ident + ".py")
        content = ("# Native Zeus routine read-only admission gate. No model or send here.\n"
                   "import importlib.util,json,sys\n"
                   "try:\n"
                   f" sys.path.insert(0,{native_root!r})\n"
                   f" spec=importlib.util.spec_from_file_location('_zeus_routine_gate',{str(package_root/'__init__.py')!r},submodule_search_locations=[{str(package_root)!r}])\n"
                   " module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)\n"
                   " from _zeus_routine_gate.actions import routine_gate\n"
                   f" result=routine_gate({str(self.home)!r},{ident!r})\n"
                   "except Exception as error: result={'wakeAgent':False,'reason':'gate_error','error_type':type(error).__name__}\n"
                   "print(json.dumps(result))\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_text() != content:
                raise ValueError("Existing routine script differs; root must reconcile installed release before enabling")
        else:
            with path.open("x") as handle:
                handle.write(content)
            os.chmod(path, 0o700)
        return path, digest(content)

    def routine_pause(self, ident, current, quote):
        authority = instruction(current, quote)
        item = self.db.get(ident)
        saved = item.get("native_routine") or {}
        if not saved.get("job_id"):
            raise ValueError("No native routine job exists")
        # Persisted gate closes first. A racing occurrence cannot use the old enabled intent.
        saved["state"] = "paused"
        item["routine_enabled"] = False
        self.db.save(item)
        self.db.event(ident, "routine_pause_intent", {"job_id": saved["job_id"], "authority": authority})
        self.db.db.commit()
        dispatch(self.ctx, "cronjob_manage", {"action": "pause", "job_id": saved["job_id"], "reason": "Current user paused Zeus routine"})
        with native_cron(self.home) as jobs:
            job = jobs.get_job(saved["job_id"])
            claim = (job or {}).get("fire_claim") or {}
            if claim.get("by"):
                # The scheduler's own output/delivery fence prevents a later send by this owner.
                # An already-started transport remains uncertain and must be reconciled.
                with jobs.fire_claim_fence(saved["job_id"], expected_owner=claim["by"]) as owned:
                    if not owned:
                        raise ValueError("Native fire ownership changed during pause; persisted routine pause remains active")
                    jobs.update_job(saved["job_id"], {"fire_claim": None})
        return self.routine_status(ident)

    def routine_resume(self, ident, current, quote):
        authority = instruction(current, quote)
        if not re.search(r"\b(resume|restart|re-enable|enable|continue)\b", quote, re.I):
            raise ValueError("Explicit current resume instruction required")
        item = self.db.get(ident)
        saved = item.get("native_routine") or {}
        if item["state"] != "accepted" or not saved.get("job_id"):
            raise ValueError("Only an accepted current installed routine can resume")
        with native_cron(self.home) as jobs:
            job = jobs.get_job(saved["job_id"])
        if not job or {key: job.get(key) for key in CONTRACT_KEYS} != saved["contract"]:
            raise ValueError("Native routine contract changed; reconcile before resume")
        saved["state"] = "enabled"
        item["routine_enabled"] = True
        self.db.save(item)
        self.db.event(ident, "routine_resume_intent", {"job_id": saved["job_id"], "authority": authority})
        self.db.db.commit()
        if not job.get("enabled"):
            dispatch(self.ctx, "cronjob_manage", {"action": "resume", "job_id": saved["job_id"]})
        return self.routine_status(ident)

    def observe_routine_final(self, context, response):
        parts = str(context.get("task_id") or "").split(":", 2)
        if context.get("platform") != "cron" or len(parts) != 3 or parts[0] != "cron":
            return None
        from agent.subagent_lifecycle import get_active_subagent_parent
        parent = get_active_subagent_parent()
        if parent is None or parent.session_id != context.get("session_id") or getattr(parent, "_current_turn_id", None) != context.get("turn_id"):
            return None
        _, job_id, execution_id = parts
        for row in self.db.db.execute("SELECT id,body FROM insights WHERE state='accepted' ORDER BY created_at DESC LIMIT 250").fetchall():
            body = json.loads(row["body"])
            routine = body.get("native_routine") or {}
            if routine.get("job_id") != job_id:
                continue
            with native_cron(self.home) as jobs:
                from cron import executions
                job = jobs.get_job(job_id)
                execution = executions.get_execution(execution_id)
            if not job or not execution or execution.get("job_id") != job_id or execution.get("status") not in {"claimed", "running"} or {key: job.get(key) for key in CONTRACT_KEYS} != routine.get("contract"):
                return None
            self.db.event(row["id"], "routine_final_observed", {"native_job_id": job_id, "execution_id": execution_id,
                          "session_id": context.get("session_id"), "turn_id": context.get("turn_id"),
                          "response_sha256": digest(str(response or "")), "meaningful_output_present": bool(str(response or "").strip() and str(response).strip() != "[SILENT]"),
                          "returned": "native final observed; delivery and content quality not established"})
            self.db.db.commit()
            return None

    def routine_for_context(self, context):
        parts = str(context.get("task_id") or "").split(":", 2)
        if len(parts) != 3 or parts[0] != "cron":
            return None
        for row in self.db.db.execute("SELECT id,body FROM insights ORDER BY created_at DESC LIMIT 500").fetchall():
            routine = json.loads(row["body"]).get("native_routine") or {}
            if routine.get("job_id") == parts[1]:
                return row["id"], parts[1], parts[2]
        return None

    def assert_routine_active(self, binding):
        ident, job_id, execution_id = binding
        item = self.db.get(ident)
        with native_cron(self.home) as jobs:
            from cron import executions
            job = jobs.get_job(job_id)
            occurrence = executions.live_inflight_execution(job_id)
        if not occurrence or occurrence.get("id") != execution_id or not gate_decision(item, job, occurrence)["wakeAgent"]:
            raise ValueError("Routine was paused, expired, changed or lost its native occurrence")
        return True

    def routine_guard(self, tool_name, args, next_call, context):
        binding = self.routine_for_context(context)
        if binding is None:
            return next_call(args)
        try:
            self.assert_routine_active(binding)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc), "blocked_by": "zeus-insight routine"})
        result = next_call(args)
        try:
            self.assert_routine_active(binding)
        except Exception:
            return json.dumps({"ok": False, "error": "Result arrived after routine pause or ownership change; do not treat it as current proof"})
        return result

    def routine_transform(self, context):
        binding = self.routine_for_context(context)
        if binding is None:
            return None
        try:
            self.assert_routine_active(binding)
        except Exception:
            self.db.event(binding[0], "routine_return_suppressed", {"execution_id": binding[2], "reason": "paused, expired, changed or ownership lost"})
            self.db.db.commit()
            return "[SILENT]"
        return None

    def routine_status(self, ident):
        item = self.db.get(ident)
        saved = item.get("native_routine") or {}
        if not saved.get("job_id"):
            return {"installed": False, "enabled": False, "executed": False}
        with native_cron(self.home) as jobs:
            from cron import executions, delivery_queue
            job = jobs.get_job(saved["job_id"])
            rows = executions.list_executions(job_id=saved["job_id"], limit=5)
            receipts = []
            for row in rows:
                delivered = delivery_queue.get_status(row["id"])
                receipts.append({key: row.get(key) for key in ("id", "status", "claimed_at", "finished_at", "scheduled_instant", "delivery_outcome")})
                receipts[-1]["native_delivery_status"] = (delivered or {}).get("status")
        observed = [json.loads(row[0]) for row in self.db.db.execute("SELECT detail FROM events WHERE insight_id=? AND kind='routine_final_observed' ORDER BY created_at DESC LIMIT 5", (ident,))]
        return {"installed": job is not None, "enabled": bool(job and job.get("enabled") and saved["state"] == "enabled"),
                "job_id": saved["job_id"], "next_run_at": (job or {}).get("next_run_at"), "input_window_hours": saved["input_window_hours"],
                "contract_current": bool(job and {key: job.get(key) for key in CONTRACT_KEYS} == saved["contract"]),
                "executions": receipts, "native_final_receipts": observed,
                "claim": "Scheduled, native execution, observed final response and delivery status are separate. No independent destination or business-outcome proof is inferred."}

    def routine_result(self, ident, execution_id=None):
        item = self.db.get(ident)
        saved = item.get("native_routine") or {}
        if not saved.get("job_id"):
            raise ValueError("No native routine exists")
        observed = [json.loads(row[0]) for row in self.db.db.execute(
            "SELECT detail FROM events WHERE insight_id=? AND kind='routine_final_observed' ORDER BY created_at DESC LIMIT 20", (ident,))]
        receipt = next((row for row in observed if row.get("meaningful_output_present") and (execution_id is None or row["execution_id"] == execution_id)), None)
        if not receipt:
            raise ValueError("No observed meaningful native final matches this routine occurrence")
        with native_cron(self.home):
            from cron import executions, delivery_queue
            execution = executions.get_execution(receipt["execution_id"])
            delivery = delivery_queue.get_status(receipt["execution_id"])
        if not execution or execution.get("job_id") != saved["job_id"] or execution.get("status") != "completed":
            raise ValueError("The exact native occurrence has not completed")
        from hermes_state import SessionDB
        history = SessionDB(db_path=self.home / "state.db", read_only=True)
        try:
            messages = history.get_messages(receipt["session_id"], include_compacted=True, limit=100, latest=True)
        finally:
            history.close()
        matches = [row for row in messages if row.get("role") == "assistant" and isinstance(row.get("content"), str) and digest(row["content"]) == receipt["response_sha256"]]
        if len(matches) != 1 or len(matches[0]["content"]) > 65536:
            raise ValueError("Native final-history match is absent, ambiguous or exceeds the bounded result reader")
        return {"job_id": saved["job_id"], "execution_id": receipt["execution_id"], "session_id": receipt["session_id"],
                "message_id": matches[0].get("id"), "content": matches[0]["content"], "sha256": receipt["response_sha256"],
                "native_execution": "completed", "native_delivery_status": (delivery or {}).get("status") or execution.get("delivery_outcome"),
                "independent_destination_verified": False}


def gate_decision(item, job, execution, clock=None):
    clock = clock or datetime.now(timezone.utc)
    saved = item.get("native_routine") or {}
    if item.get("state") in {"expired", "dismissed", "retired"} or timestamp(item["expires_at"]) <= clock or saved.get("state") != "enabled" or not item.get("routine_enabled"):
        return {"wakeAgent": False, "reason": "routine_not_enabled_or_current"}
    if not job or not job.get("enabled") or {key: job.get(key) for key in CONTRACT_KEYS} != saved.get("contract"):
        return {"wakeAgent": False, "reason": "native_job_contract_changed"}
    if not execution or execution.get("job_id") != job["id"] or execution.get("status") not in {"claimed", "running"}:
        return {"wakeAgent": False, "reason": "no_current_native_occurrence"}
    end = timestamp(execution.get("scheduled_instant") or execution["claimed_at"])
    return {"wakeAgent": True, "routine_id": item["id"], "native_job_id": job["id"], "execution_id": execution["id"],
            "input_start": (end - timedelta(hours=saved["input_window_hours"])).isoformat(), "input_end": end.isoformat(),
            "authority_source": saved["authority"]["source_id"], "delivery": saved["delivery_authorized"]}


def routine_gate(home, ident):
    # Native scheduler script: exact profile, read-only derived state; native scheduler owns the occurrence.
    import sqlite3
    path = Path(home).resolve() / "plugin-data" / "zeus-insight" / "data.db"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT body,state,expires_at FROM insights WHERE id=?", (ident,)).fetchone()
    if not row:
        return {"wakeAgent": False, "reason": "routine_missing"}
    item = json.loads(row[0]); item.update(id=ident, state=row[1], expires_at=row[2])
    saved = item.get("native_routine") or {}
    script = Path(saved.get("script", ""))
    if not script.is_file() or digest(script.read_bytes()) != saved.get("script_sha256"):
        return {"wakeAgent": False, "reason": "routine_script_changed"}
    with native_cron(home) as jobs:
        from cron import executions
        job = jobs.get_job(saved.get("job_id", ""))
        execution = executions.live_inflight_execution(saved.get("job_id", ""))
    return gate_decision(item, job, execution)

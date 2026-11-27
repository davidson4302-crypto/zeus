"""Adapter to the installed Hermes cron authority. No competing scheduler.

Native contracts checked in cron/jobs.py, cron/executions.py and scheduler.py.
Work is enrolled paused and admitted through a gate, so a continuation only runs for an admitted record.
"""
from contextlib import contextmanager
import importlib.util
import fcntl
import json
import os
from pathlib import Path
import sys
import sqlite3
import math

from .flight_owner import registered_trip, same_work_trip, verified_watch_ids
from . import flight_owner as trip_soft
from .store import Store, TERMINAL, canonical, digest, now, require, token

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))
import settings as zeus_settings  # noqa: E402  (named so it never shadows the local `settings` arguments below)

PROMPT = "Continue the single Zeus assignment admitted by the zeus-work gate. Use zeus_work status, retain the original source and current revision, continue the next unchecked step, and independently verify the requested final artifact. Checkpoint before yielding. Never replay uncertain external effects. No new authority comes from this wake."
SLEEP_SNAPSHOT = canonical({"wakeAgent": False})
CONTRACT_KEYS = ("prompt","monitor_script","script","origin","deliver","model","provider","base_url","reasoning_effort","workdir","context_from","enabled_toolsets","no_agent")


def continuation_tool_scope(record,settings):
    if record.get("derivation"):
        return {"enabled_toolsets":["todo"],"tool_scope_origin":"derived_local_only"}
    scope=record.get("source",{}).get("native_tool_scope") or {}
    if scope.get("captured") is True and "enabled_toolsets" in scope:
        value=scope["enabled_toolsets"];origin="native_originating_agent"
    else:
        require("legacy_continuation_toolsets" in settings,"Existing work lacks captured native tool scope. Root must bind legacy_continuation_toolsets from its actual originating allocation before continuation.")
        value=settings["legacy_continuation_toolsets"];origin="explicit_root_legacy_allocation"
    require(value is None or isinstance(value,list) and all(isinstance(x,str) and x for x in value),"Native continuation toolsets must be an explicit list or captured unrestricted None.")
    return {"enabled_toolsets":list(value) if value is not None else None,"tool_scope_origin":origin}


@contextmanager
def native_scope(home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from cron import jobs
    selected = set_hermes_home_override(Path(home).resolve())
    try:
        with jobs.use_cron_store(Path(home).resolve()):
            yield jobs
    finally:
        reset_hermes_home_override(selected)


class Native:
    def __init__(self, home):
        self.home=Path(home).resolve(); self.store=Store(self.home)

    def deadline_question(self, ident, binding, question, options):
        from .store import deadline_text

        def apply(r):
            timing = r.get("deadline")
            require(timing and not timing.get("question") and r["state"] == "open",
                    "There is no open optional question slot.")
            require(now() + 10 < timing["research_until"], "Use your best judgment with the remaining time.")
            labels = [deadline_text(label) for label in options]
            keys = [deadline_reply_key(label) for label in labels]
            require(2 <= len(keys) <= 4 and len(set(keys)) == len(keys)
                    and all(0 < len(k) <= 40 for k in keys), "Use two to four short, distinct choices.")
            require(all(k not in {"yes", "no", "approve", "skip", "stop", "continue"} for k in keys),
                    "Use choices about the work. Approvals keep their normal route.")
            text = deadline_text(question)
            require(len(text) <= 300 and text.endswith("?"), "Use one short optional question.")
            # Native reply anchors disambiguate overlapping questions in this chat.
            timing["question"] = {
                "text": text + "\n" + " or ".join(labels) + "?",
                "options": dict(zip(keys, labels)), "expires_at": timing["research_until"],
                "revision": r["revision"], "generation": r["generation"],
                "operation_id": token(), "state": "prepared",
            }
            timing["next_check"] = now()
            return {"question_prepared": True, "expires_at": timing["research_until"],
                    "continue_work": True}

        return self.store.mutate(ident, binding, "deadline_optional_question_prepared", apply)

    def deadline_answer(self, origin, text, message_id, reply_to):
        """This runs after native owner authentication, before raw busy-chat steering."""
        if not message_id:
            return False
        key = deadline_reply_key(text)
        if not key or len(key) > 80:
            return False
        with self.store.db() as db:
            matches = []
            for (payload,) in db.execute("SELECT payload FROM work WHERE json_type(payload,'$.deadline.question')='object'"):
                r = json.loads(payload)
                timing = r["deadline"]
                q = timing["question"]
                if any(origin[k] != timing["origin"][k] for k in ("platform", "chat_id", "author_id", "profile")):
                    continue
                candidate = key
                for prefix in ("prioritize ", "focus on "):
                    if candidate.startswith(prefix):
                        candidate = candidate[len(prefix):]
                for suffix in (" please", " matters more", " is more important", " first"):
                    if candidate.endswith(suffix):
                        candidate = candidate[:-len(suffix)]
                exact = reply_to and reply_to == q.get("provider_id")
                if reply_to and not exact:
                    continue
                if not exact and (candidate not in q["options"] or now() >= q["expires_at"] + 180):
                    continue
                if q.get("provider_id"):
                    answer = q["options"].get(candidate) or text.strip()
                    matches.append((r, q, answer))
            if len(matches) != 1:
                return bool(matches)
            r, q, answer = matches[0]
            if (r["state"] == "open" and not q.get("answer_id") and now() < q["expires_at"]
                    and r["revision"] == q["revision"] and r["generation"] == q["generation"]):
                q.update(answer=answer, answer_id=message_id, answered_at=now())
                self.store.save(db, r, "deadline_preference_received", {"message_id": message_id})
            return True

    def deadline_pass(self, ident):
        """Select under the existing work fence, then hand one immutable result to native delivery."""
        with self.store.db() as db:
            r = self.store.load(db, ident)
            d = r.get("deadline")
            if not d or r["state"] == "stopped" or d["next_check"] is None or d["next_check"] > now():
                return None
            q = d.get("question") or {}
            if not d.get("result") and now() >= d["fallback_at"]:
                best = d.get("best")
                text = DEADLINE_EMPTY
                if best and digest(best["text"].encode()) == best["sha256"]:
                    text = "Partial answer. Here's what I could check in time.\n\n" + best["text"]
                    if best.get("missing"):
                        text += "\n\nStill missing: " + best["missing"]
                    if q.get("answer_id") and q["answer_id"] != best.get("answer_seen"):
                        text += "\n\nI got your preference, but I couldn't apply it before the deadline."
                self.store.prepare_deadline_return(r, text, "partial" if best else "nothing_verified")
                self.store.save(db, r, "deadline_fallback_prepared", {"outcome": d["result"]["outcome"]})
            item = d.get("result")
            question = item is None
            if question:
                item = q
                if not q or r["state"] != "open" or q["expires_at"] <= now():
                    d["next_check"] = d["fallback_at"]
                    self.store.save(db, r, "deadline_question_window_closed")
                    return None
            state = item.get("state", "prepared")
            operation_id = q["operation_id"] if question else r["return_execution_id"]
            # Only the first prepare-to-attempt transition may enter the sender. Anything
            # ambiguous is resolved from the original native receipt, never by resending.
            if state == "prepared" or state == "held":
                item.update(state="attempting", attempted_at=now())
                if not question:
                    r["return_state"] = "awaiting_native_return"
            d["next_check"] = min(now() + DEADLINE_RECEIPT_INTERVAL, d["fallback_at"]) if question else now() + DEADLINE_RECEIPT_INTERVAL
            self.store.save(db, r, "deadline_return_claim" if state in {"held", "prepared"} else "deadline_receipt_check",
                            {"question": question, "operation_id": operation_id})
            generation, revision, origin = r["generation"], r["revision"], dict(d["origin"])
            content, attempt_id = item["text"], item.get("attempt_id")

        response = {}
        if state in {"held", "prepared"}:
            # STOP/correction wins if committed before the native operation begins.
            current = self.store.get(ident)
            if (current["generation"], current["revision"]) != (generation, revision) or current["state"] == "stopped":
                return None
            try:
                response = native_deadline_send(self.home, origin, operation_id, content)
            except Exception as exc:
                response = {"error": str(exc)[:200]}
            attempt_id = response.get("delivery_attempt_id")
        receipt = deadline_receipt(self.home, origin, operation_id, attempt_id)
        with self.store.db() as db:
            r = self.store.load(db, ident)
            if r["revision"] != revision or r["generation"] != generation:
                self.store.event(db, ident, "deadline_return_after_revision_or_stop", {"operation_id": operation_id,
                                 "attempt_id": attempt_id or (receipt or {}).get("attempt_id")})
                return None
            d = r["deadline"]
            item = d["question" if question else "result"]
            finished_at = now()
            if response.get("held") and not response.get("hold_id"):
                hold_until = response["deliver_after"]
                item.update(state="held", reason="quiet_hours_or_delivery_policy")
                retry_at = hold_until if math.isfinite(hold_until) else finished_at + DEADLINE_RECEIPT_INTERVAL
                if question:
                    retry_at = min(retry_at, item["expires_at"])
            elif receipt and receipt.get("provider_id") and receipt["state"] in {"accepted", "sent", "delivered", "read"}:
                item.update(state=receipt["state"], attempt_id=receipt["attempt_id"], provider_id=receipt["provider_id"])
                retry_at = None if receipt["state"] in {"delivered", "read"} else finished_at + DEADLINE_RECEIPT_INTERVAL
            elif item.get("state") in {"accepted", "sent", "delivered", "read"} and receipt is None:
                retry_at = finished_at + DEADLINE_RECEIPT_INTERVAL
            else:
                item.update(state="uncertain", attempt_id=attempt_id or (receipt or {}).get("attempt_id"),
                            reason="native_return_unconfirmed" if receipt else "native_transport_or_runtime_unavailable")
                if response.get("error"):
                    item["transport_error"] = response["error"]
                if response.get("hold_id"):
                    item["native_hold_id"] = response["hold_id"]
                retry_at = finished_at + DEADLINE_RECEIPT_INTERVAL
            if question:
                # A stalled question cannot displace a separately prepared final return.
                if not d.get("result"):
                    d["next_check"] = d["fallback_at"] if item["state"] in {"accepted", "sent", "delivered", "read"} else min(retry_at, d["fallback_at"])
            else:
                phase = item["state"]
                d["next_check"] = retry_at
                if phase in {"accepted", "sent", "delivered", "read"}:
                    item.setdefault("accepted_observed_at", finished_at)
                    r["state"] = "completed" if item["outcome"] == "complete" else "deadline_partial"
                    if r["state"] == "completed":
                        r.setdefault("completed_at", finished_at)
                    r["return_state"] = "native_delivered" if phase in {"delivered", "read"} else "native_adapter_accepted"
                elif phase == "held":
                    r["return_state"] = "held"
                else:
                    r["return_state"] = "uncertain"
                r["return_receipt"] = {
                    "execution_id": operation_id, "attempt_id": item.get("attempt_id"), "native_status": phase,
                    "provider_id": item.get("provider_id"), "observed_at": finished_at,
                    "destination_confirmed": phase in {"delivered", "read"}, "independent_destination_read": False,
                }
                if not item.get("accepted_observed_at") and (finished_at >= d["due_at"] or phase in {"held", "uncertain"}):
                    d.setdefault("deadline_delivery_obstacle", item.get("reason", phase))
                if finished_at >= d["due_at"] and (item.get("accepted_observed_at") or finished_at) >= d["due_at"]:
                    d.setdefault("missed_at", d["due_at"])
                    d.setdefault("deadline_delivery_obstacle", "native_acceptance_not_observed_by_deadline")
                if d["next_check"] is not None and not d.get("missed_at") and finished_at < d["due_at"]:
                    d["next_check"] = min(d["next_check"], d["due_at"])
                # Stop polling once all useful status has settled, or the native journal owns long-term reconciliation.
                if finished_at > d["due_at"] + 86400:
                    d["next_check"] = None
            self.store.save(db, r, "deadline_transport_observed", {"question": question, "state": item["state"]})
            return {"id": ident, "question": question, "state": item["state"]}

    def check_derived(self,r,db=None):
        if not r.get("derivation"):return
        from hermes_cli.config import load_config_readonly
        entry=((load_config_readonly().get("plugins") or {}).get("entries") or {}).get("zeus-work") or {}
        policy=(entry.get("settings") or entry.get("config") or {}).get("derived_preparation") or {}
        require(policy.get("enabled") is True and digest(policy)==r["derivation"]["policy"]["sha256"],"Preparation policy is disabled or changed.")
        self.store.parent_for(r,db)

    def capture_foreground(self,parent,source):
        native=getattr(parent,"_session_db",None)
        holder=getattr(parent,"_active_session_turn_lease_holder",None)
        require(native is not None and Path(native.db_path).resolve()==self.home/"state.db" and holder,"Actual native foreground lease required.")
        resolve=getattr(type(native),"_session_turn_lease_key_on_conn",None)
        require(callable(resolve),"Native foreground lease resolver unavailable.")
        with sqlite3.connect((self.home/"state.db").as_uri()+"?mode=ro",uri=True,timeout=2) as db:
            db.row_factory=sqlite3.Row
            conversation=resolve(native,db,source["session_id"])
            row=db.execute("SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?",(conversation,)).fetchone()
            require(row and row["holder"]==holder and float(row["expires_at"])>now(),"Native foreground lease is not current.")
        self.store.foreground_lease(conversation,holder,source["session_id"],source["turn_id"])

    def foreground_active(self,db=None):
        if db is None:
            with self.store.db() as owned:
                return self.foreground_active(owned)
        refs=db.execute("SELECT conversation_id,holder FROM foreground").fetchall()
        if not refs:return False
        # Fail closed on an unavailable native lease store; never fabricate expiry.
        with sqlite3.connect((self.home/"state.db").as_uri()+"?mode=ro",uri=True,timeout=2) as native:
            for conversation,holder in refs:
                row=native.execute("SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?",(conversation,)).fetchone()
                if row and row[0]==holder and isinstance(row[1],(int,float)) and math.isfinite(row[1]) and row[1]>now():return True
        return False

    def execution_config(self,record):
        from hermes_cli.config import load_config_readonly
        with native_scope(self.home):
            c = load_config_readonly()
        entry = ((c.get("plugins") or {}).get("entries") or {}).get("zeus-work") or {}
        settings = entry.get("settings") or entry.get("config") or {}
        tools = continuation_tool_scope(record, settings)
        if settings.get("inherit_cron_model") is True:
            # Omit job pins so cron resolves both primary and fallback efforts at execution.
            return tools
        model = c.get("model") or {}
        if isinstance(model, str):
            model = {"default": model}
        return {"model": model.get("default"), "provider": model.get("provider"),
                "base_url": model.get("base_url"), "reasoning_effort": (c.get("agent") or {}).get("reasoning_effort"),
                **tools}

    def bind_trip(self, ident, binding, trip_id, steps):
        """Only hand off the named remaining steps of this particular assignment."""
        trip = registered_trip(self.home, trip_id)
        require(trip and trip["state"] == "watching", "An existing active flight watch is required.")
        def bind(record):
            require(trip_id in verified_watch_ids(self.store, record) or trip_id == trip_soft.matched_trip_id(self.home, record, now()), "Verify the native trip ID in this assignment before handing over its pending steps.")
            pending = {step["id"] for step in record["steps"] if step["state"] != "checked"}
            require(isinstance(steps, list) and pending and set(steps).issubset(pending), "Name only unchecked steps owned by this watcher. Use an empty list when all remaining steps are separate work.")
            require(not record.get("derivation") and not any(effect["state"] in {"attempted","uncertain","accepted"} for effect in record["effects"].values()),
                    "Reconcile any existing uncertain effects before transferring the remaining watch.")
            record["flight_monitor"] = {**trip, "revision": record["revision"], "scope_hash": record["scope_hash"], "steps": sorted(set(steps))}
        return self.store.mutate(ident, binding, "flight_monitor_owns_remaining_steps", bind)

    def sleeping_reason(self, record):
        trip = same_work_trip(self.home, record)
        if trip:
            if trip["state"] == "watching":
                return "Zeus flight watcher owns tracking: " + trip["id"]
            return "Zeus flight watch " + trip["id"] + " ended; reconcile its original receipts"
        # A bound watch that is temporarily unreadable is still its original owner's
        # assignment. Expose this in doctor. Never race it with a second flight poller.
        binding = record.get("flight_monitor") or {}
        if binding.get("revision") == record["revision"] and binding.get("scope_hash") == record["scope_hash"]:
            pending = {step["id"] for step in record["steps"] if step["state"] != "checked"}
            if pending and pending.issubset(binding["steps"]):
                return "Zeus bound flight watcher needs a receipt check"
            return None
        # A registered watch can appear after this assignment already enrolled a
        # continuation. Do not start a duplicate while its remaining scope is resolved.
        for trip_id in sorted(verified_watch_ids(self.store, record)):
            trip = registered_trip(self.home, trip_id)
            if trip and trip["state"] == "watching":
                return "Zeus recorded flight watch " + trip_id + "; bind its watch steps before scheduling separate work"
        return None

    def pause_bound_job(self, jobs, record, reason):
        schedule = record.get("schedule") or {}
        job = jobs.get_job(schedule["job_id"]) if schedule.get("job_id") else None
        if job and job.get("name") == "zeus-work:" + record["id"] and job.get("enabled"):
            jobs.pause_job(job["id"], reason=reason)

    def refuse_occurrence(self, context):
        """A late hook from a previous occurrence cannot pause the current job."""
        parts = str(context.get("task_id") or "").split(":", 2)
        if len(parts) != 3 or parts[0] != "cron" or not parts[2]:
            return
        with native_scope(self.home) as jobs:
            from cron import executions
            job = jobs.get_job(parts[1])
            if not job or not job.get("enabled") or not job.get("name", "").startswith("zeus-work:"):
                return
            occurrence = executions.latest_execution(job["id"])
            if not occurrence or occurrence["id"] != parts[2] or occurrence.get("status") not in {"claimed", "running"}:
                return
            with self.store.db() as db:
                ident = job["name"].split(":",1)[1]
                record = self.store.load(db, ident)
                if (record.get("schedule") or {}).get("job_id") != job["id"]:
                    return
                worker = record.get("worker") or {}
                if (worker.get("execution_id") == parts[2] and worker.get("session_id") and
                        worker["session_id"] != context.get("session_id") and worker.get("state") == "running"):
                    return
                self.pause_bound_job(jobs, record, "Zeus worker admission failed; reconcile its native occurrence")
                self.store.event(db, ident, "unadmitted_wake_paused", {"execution_id": parts[2]})

    def ensure_continuation(self,ident):
        r=self.store.get(ident)
        if r.get("deadline"):
            return {"id": ident, "scheduled": False, "reason": "timed_return_uses_saved_work"}
        if r["state"] not in {"open","reconcile_first"}:return {"id":ident,"scheduled":False,"reason":r["state"]}
        if not r.get("continuation_intent",{}).get("allowed",True):return {"id":ident,"scheduled":False,"reason":"explicit_no_background"}
        self.check_derived(r)
        with native_scope(self.home) as jobs:
            reason = self.sleeping_reason(r)
            if reason:
                self.pause_bound_job(jobs, r, reason)
                return {"id": ident, "scheduled": False, "reason": reason}
            s=r.get("schedule") or {};j=jobs.get_job(s["job_id"]) if s.get("job_id") else None
            if j and j.get("enabled"):return {"id":ident,"scheduled":True,"job_id":j["id"],"existing":True}
            if s.get("state")=="acknowledged" and not j:return {"id":ident,"scheduled":False,"reason":"native_schedule_removed_preserved"}
            resumed=r.get("resume_authorization") or {}
            owner_resumed_stop=(j and j.get("paused_reason") in {"Zeus stop","Authenticated user STOP"}
                and resumed.get("generation")==r["generation"]
                and resumed.get("source_sha256")==digest(r.get("resume_source")))
            resolved_flight_scope = bool(r.get("flight_monitor") and str((j or {}).get("paused_reason") or "").startswith("Zeus recorded flight watch "))
            if j and not j.get("enabled") and not resolved_flight_scope and j.get("paused_reason") not in {"Zeus wake intent awaiting durable acknowledgement","Zeus revise"} and not owner_resumed_stop:
                return {"id":ident,"scheduled":False,"reason":"explicit_native_pause_preserved"}
        binding={"generation":r["generation"],"revision":r["revision"]}
        return self.enroll(ident,binding,self.execution_config(r))

    def maintenance(self,limit=3):
        """Bounded no-agent doctor consumer for root's one existing native health clock."""
        require(1<=int(limit)<=3,"Maintenance limit is 1..3.")
        summary={"wakeAgent":False,"no_new_authority":True,"external_task_scan":False,"sender":False,"llm":False,"records":[]}
        if self.foreground_active():return dict(summary,reason="native_foreground_lease_live")
        selected=sorted((r for r in self.store.all() if r["state"] not in TERMINAL and not r.get("deadline")),
                        key=lambda r:r["created_at"])[:int(limit)]
        for r in selected:
            try:
                if r["state"]=="output_ready":
                    result=self.gate(r["id"])
                elif r["state"] in {"open","reconcile_first"}:
                    self.repair_pending(limit=1,only_ids={r["id"]})
                    result=self.ensure_continuation(r["id"])
                else:result={"reason":r["state"]}
                summary["records"].append({"id":r["id"],**{k:result[k] for k in ("state","reason","scheduled","job_id","return_state","native_status") if k in result}})
            except Exception as exc:
                with self.store.db() as db:self.store.event(db,r["id"],"automatic_continuation_failed",{"error":str(exc)})
                summary["records"].append({"id":r["id"],"scheduled":False,"error_type":type(exc).__name__})
        return summary

    def script(self, ident):
        import hermes_bootstrap
        native_root=str(Path(hermes_bootstrap.__file__).resolve().parent)
        plugin_root=str(self.home / "plugins/zeus-work")
        path=self.home/"scripts"/("zeus_work_gate_"+ident+".py")
        path.parent.mkdir(parents=True,exist_ok=True)
        # Native monitor_script (unlike a regular pre-run script) ends the run
        # before model setup when the interpreter exits, fails or times out.
        body=("# Generated Zeus admission monitor. An unsuccessful process cannot wake the model.\n"
            "import logging,sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0,{native_root!r})\n"
            "from hermes_constants import set_hermes_home_override\n"
            f"set_hermes_home_override(Path({str(self.home)!r}))\n"
            "from plugins.plugin_loader import load_plugin_module\n"
            "logger=logging.getLogger('zeus-work-gate')\n"
            f"module=load_plugin_module('_zeus_gate.work',Path({plugin_root!r}).resolve(),parents=(),logger=logger,synthetic_namespace='_zeus_gate')\n"
            "if module is None: raise RuntimeError('Native continuation plugin unavailable')\n"
            "from _zeus_gate.work.native import Native\n"
            f"print(Native({str(self.home)!r}).monitor_snapshot({ident!r}))\n")
        data=body.encode()
        if path.exists():
            require(path.read_bytes()==data,"Existing continuation script differs. Root must reconcile the installed code before enrollment.")
        else:
            temp=path.with_suffix("."+token()+".tmp")
            with temp.open("xb") as f:
                f.write(data);f.flush();os.fsync(f.fileno())
            os.chmod(temp,0o700);os.replace(temp,path)
        return path,digest(data)

    def enroll(self, ident, binding, config):
        # One installer across processes, including recovery after partial enrollment.
        with (self.store.root/"enrollment.lock").open("a") as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            return self._enroll(ident,binding,config)

    def _enroll(self, ident, binding, config):
        with native_scope(self.home) as jobs:
            r = self.store.get(ident)
            self.store.fence(r, binding)
            reason = self.sleeping_reason(r)
            if reason:
                self.pause_bound_job(jobs, r, reason)
                return {"id": ident, "scheduled": False, "reason": reason}
        path,sha=self.script(ident)
        with native_scope(self.home) as jobs, self.store.db() as db:
            r=self.store.load(db,ident);self.store.fence(r,binding,db=db);self.check_derived(r,db)
            require(r.get("continuation_intent",{}).get("allowed",True),"Original owner explicitly disabled background continuation.")
            require(r["state"]!="needs_person","Resolve the specific human dependency before scheduling continuation.")
            # Commit an intent before creating anything in the separate native store.
            if not r.get("schedule"):
                r["schedule"]={"state":"pending","name":"zeus-work:"+ident,"script":str(path),"script_sha256":sha,"origin":r["source"].get("return_origin"),"execution_config":config,"catch_up":"one native occurrence; never replay business effects","created_at":now()}
                self.store.save(db,r,"wake_intent_saved")
        with native_scope(self.home) as jobs:
            r=self.store.get(ident);schedule=r["schedule"]
            config=schedule["execution_config"];origin=schedule.get("origin")
            require("enabled_toolsets" in config,"Saved enrollment lacks its originating tool scope. Reconcile the exact intent before activation.")
            delivery="origin" if origin else "local"
            found=[j for j in jobs.list_jobs(include_disabled=True) if j.get("name")==schedule["name"]]
            require(len(found)<=1,"Multiple native schedules have this exact assignment name. Reconcile before any wake.")
            if found:
                job=found[0]
                resolved_flight_scope = bool(r.get("flight_monitor") and str(job.get("paused_reason") or "").startswith("Zeus recorded flight watch "))
                if not job.get("enabled") and not resolved_flight_scope and job.get("paused_reason") not in {"Zeus wake intent awaiting durable acknowledgement", "Zeus revise", "Zeus stop", "Authenticated user STOP"}:
                    return {"id": ident, "scheduled": False, "reason": "explicit_native_pause_preserved"}
                require(job.get("monitor_script")==str(path) and not job.get("script") and job.get("prompt")==PROMPT and job.get("deliver")==delivery and job.get("origin")==origin, "Existing native schedule does not match this enrollment intent and original route.")
                require(job.get("enabled_toolsets")==config["enabled_toolsets"],"Existing native schedule widened or changed the originating toolsets.")
            else:
                # Native cron owns outgoing outbox, receipts and uncertain delivery.
                # The originating route is captured from native context, never model args.
                job=jobs.create_job(prompt=PROMPT,schedule="1m",name=schedule["name"],monitor_script=str(path),
                    deliver=delivery,origin=origin,context_from="self",workdir=str(self.home/"workspace"),
                    enabled_toolsets=config["enabled_toolsets"],
                    model=config.get("model"),provider=config.get("provider"),
                    base_url=config.get("base_url"),reasoning_effort=config.get("reasoning_effort"),
                    paused=True,paused_reason="Zeus wake intent awaiting durable acknowledgement")
                from cron.monitor import hash_monitor_output
                job = jobs.update_job(job["id"], {"monitor_state": {"last_output_hash": hash_monitor_output(SLEEP_SNAPSHOT)}})
            with self.store.db() as db:
                r=self.store.load(db,ident);self.store.fence(r,binding,db=db)
                schedule=r["schedule"]
                schedule.update(job_id=job["id"],state="acknowledged",contract={k:job.get(k) for k in CONTRACT_KEYS},acknowledged_at=now())
                self.store.save(db,r,"wake_acknowledged",{"native_job_id":job["id"]})
            # STOP could have won since the acknowledgement. Check before resume;
            # even a racing native timer cannot pass gate against persisted STOP.
            r=self.store.get(ident);self.store.fence(r,binding)
            resumed=jobs.resume_job(job["id"])
            require(resumed and resumed.get("enabled"),"Native scheduler did not acknowledge resume.")
            return dict(id=ident,job_id=job["id"],state="scheduled",next_run_at=resumed.get("next_run_at"),return_route=delivery,origin=origin,delivery_state="not_yet_attempted")

    def pause(self, ident, reason, revoke=False,expected_generation=None):
        with self.store.db() as db:
            r=self.store.load(db,ident)
            if expected_generation and r["generation"]!=expected_generation:return {"skipped":"newer generation owns the schedule"}
            if (r.get("schedule") or {}).get("job_id"):
                with native_scope(self.home) as jobs:
                    from cron import executions,delivery_queue
                    job_id=r["schedule"]["job_id"]
                    if revoke:
                        ids=set(r.get("revoked_execution_ids",[]))
                        latest=executions.latest_execution(job_id)
                        if latest and latest["status"] in {"claimed","running"}:ids.add(latest["id"])
                        receipts={eid:delivery_queue.cancel_execution(eid,reason) for eid in ids}
                        r.setdefault("return_cancellations",{}).update(receipts)
                        self.store.save(db,r,"native_return_cancellation",{"receipts":receipts})
                    paused=jobs.pause_job(job_id,reason=reason)
                    claim=(paused or {}).get("fire_claim") or {}
                    if revoke and claim.get("by"):
                        with jobs.fire_claim_fence(job_id,expected_owner=claim["by"]) as owned:
                            require(owned,"Native claim changed while stopping. Persisted STOP remains active; reconcile the native occurrence.")
                            paused=jobs.update_job(job_id,{"fire_claim":None})
                    return paused

    def assert_worker(self,binding):
        if not binding.get("execution_id"):return
        with native_scope(self.home) as jobs:
            from cron import executions
            r=self.store.get(binding["id"]);self.store.fence(r,binding)
            self.check_derived(r)
            e=executions.get_execution(binding["execution_id"])
            require(e and e["status"] in {"claimed","running"},"Native occurrence is no longer live.")
            w=r.get("worker") or {};j=jobs.get_job(r["schedule"]["job_id"])
            require(j and j.get("enabled"),"Native schedule is paused or missing.")
            if w.get("fire_owner"):
                require((j.get("fire_claim") or {}).get("by")==w["fire_owner"],"Native worker ownership was revoked or superseded.")

    def monitor_snapshot(self, ident):
        """Only an admitted occurrence changes the native monitor's observed value."""
        from cron import monitor
        decision = self.preflight(ident)
        if decision.get("wakeAgent") is True:
            return canonical(decision)
        with native_scope(self.home) as jobs:
            record = self.store.get(ident)
            job = jobs.get_job(record["schedule"]["job_id"])
            require(job is not None, "Native monitor job is missing.")
            # Public Hermes keeps no per-tick observation log, so only the sleeping baseline is accepted here.
            require((job.get("monitor_state") or {}).get("last_output_hash") == monitor.hash_monitor_output(SLEEP_SNAPSHOT),
                    "An unadmitted tick cannot establish a new native monitor baseline.")
            return SLEEP_SNAPSHOT

    def preflight(self, ident):
        try:
            return self.gate(ident)
        except (ValueError, KeyError) as exc:
            # A persistent invalid schedule cannot gain authority by retrying every
            # minute. A transient busy database is retried by its existing clock.
            with native_scope(self.home) as jobs, self.store.db() as db:
                record = self.store.load(db, ident)
                self.pause_bound_job(jobs, record, "Zeus continuation preflight refused")
                self.store.event(db, ident, "preflight_refused", {"reason": str(exc)[:400]})
            return {"wakeAgent": False, "state": "preflight_refused", "error_type": type(exc).__name__}

    def gate(self, ident):
        """Cheap non-consuming predicate; pending wake is acknowledged by the real turn."""
        with native_scope(self.home) as jobs:
            from cron import executions, delivery_queue
            with self.store.db() as db:
                r=self.store.load(db,ident);schedule=r.get("schedule") or {}
                try:self.check_derived(r,db)
                except ValueError as exc:
                    r.update(state="stopped",generation=token(),worker=None,derived_stop_reason=str(exc))
                    self.store.save(db,r,"derived_parent_or_policy_revoked",{"reason":str(exc)})
                    if schedule.get("job_id"):jobs.pause_job(schedule["job_id"],reason="Derived parent/policy revoked")
                    return {"wakeAgent":False,"state":"stopped","reason":str(exc)}
                if r["state"]=="output_ready":
                    self.pause_bound_job(jobs, r, "Zeus output prepared; existing return ledger owns reconciliation")
                    eid=r.get("return_execution_id") or ""
                    occurrence=executions.get_execution(eid)
                    receipt=delivery_queue.get_status(eid)
                    outcome=(receipt or {}).get("status") or (occurrence or {}).get("delivery_outcome")
                    # Native delivered is adapter success, not a claimed business outcome.
                    if outcome=="delivered" or (not schedule.get("origin") and occurrence and occurrence["status"]=="completed"):
                        r.update(state="completed",return_state="native_delivered" if schedule.get("origin") else "local_saved",return_receipt={"execution_id":eid,"native_status":outcome,"finished_at":(receipt or occurrence or {}).get("finished_at"),"independent_destination_read":False})
                        self.store.save(db,r,"native_return_accounted",r["return_receipt"])
                        jobs.pause_job(schedule["job_id"],reason="Zeus verified output and native return accounted")
                    elif outcome in {"failed","unknown","suppressed"} and r.get("return_state") != ("suppressed" if outcome=="suppressed" else "uncertain"):
                        r["return_state"]="uncertain" if outcome!="suppressed" else "suppressed"
                        self.store.save(db,r,"native_return_unsettled",{"execution_id":eid,"native_status":outcome})
                    return {"wakeAgent":False,"state":r["state"],"return_state":r["return_state"],"native_status":outcome}
                if r["state"] in TERMINAL or r["state"]=="needs_person":
                    if schedule.get("job_id"):jobs.pause_job(schedule["job_id"],reason="Zeus "+r["state"])
                    return {"wakeAgent":False,"state":r["state"]}
                reason = self.sleeping_reason(r)
                if reason:
                    self.pause_bound_job(jobs, r, reason)
                    return {"wakeAgent": False, "state": "flight_monitor", "reason": reason}
                require(schedule.get("state")=="acknowledged","Wake intent is not acknowledged.")
                job=jobs.get_job(schedule["job_id"])
                require(job and {k:job.get(k) for k in CONTRACT_KEYS}==schedule["contract"],"Native schedule contract changed; refusing wake.")
                require(job.get("schedule",{}).get("kind")=="interval" and job.get("schedule",{}).get("minutes")==1,"Native cadence changed.")
                require(Path(schedule["script"]).read_bytes() and digest(Path(schedule["script"]).read_bytes())==schedule["script_sha256"],"Gate bytes changed.")
                human=db.execute("SELECT value FROM meta WHERE key='last_human'").fetchone()
                if self.foreground_active(db):return {"wakeAgent":False,"state":"native_foreground_lease_live"}
                occurrence=executions.latest_execution(job["id"])
                require(occurrence and occurrence["status"] in {"claimed","running"},"No current native claimed occurrence.")
                old=r.get("worker") or {}
                if old.get("execution_id")==occurrence["id"]:
                    return {"wakeAgent":old.get("state")=="pending","work_id":ident,"generation":r["generation"],"execution_id":occurrence["id"],"state":"same_pending_occurrence"}
                if old.get("execution_id") and old["execution_id"]!=occurrence["id"]:
                    prior=executions.get_execution(old["execution_id"])
                    if prior and prior["status"] in {"claimed","running"}:
                        return {"wakeAgent":False,"state":"prior_occurrence_inflight"}
                    if prior and old.get("state") == "pending":
                        self.pause_bound_job(jobs, r, "Zeus preceding occurrence ended without worker admission")
                        self.store.event(db, ident, "unadmitted_wake_paused", {"execution_id": old["execution_id"]})
                        return {"wakeAgent": False, "state": "unadmitted_previous_occurrence"}
                    if any(e["state"] in {"attempted","uncertain","accepted"} for e in r["effects"].values()):
                        r["state"]="reconcile_first"
                if schedule.get("not_before",0)>now():return {"wakeAgent":False,"state":"bounded_backoff"}
                # Trip soft sleep (switch trip-soft-sleep-on). None = switch off or not applicable: unchanged path.
                soft=trip_soft.decide(self.home,r,now(),last_skip=self.last_trip_soft_skip(db,ident)) if trip_soft.switch_on(self.home,trip_soft.SOFT_SWITCH) else None
                if soft and soft["skip"]:
                    self.store.event(db,ident,"trip_soft_skip",{"why":soft["why"],**soft["event"]})
                    return {"wakeAgent":False,"state":"trip_soft_sleep","reason":soft["why"]}
                # Rotation fences stale results from any earlier attempt. Completed steps stay.
                r["generation"]=token()
                r["worker"]={"execution_id":occurrence["id"],"state":"pending","session_id":None,"generation":r["generation"],"revision":r["revision"],"scope_hash":r["scope_hash"],"admitted_at":now()}
                if soft:r["trip_soft"]=soft["mark"]
                self.store.save(db,r,"wake_pending",{"execution_id":occurrence["id"]})
                return {"wakeAgent":True,"work_id":ident,"generation":r["generation"],"revision":r["revision"],"execution_id":occurrence["id"],"state":r["state"]}

    def last_trip_soft_skip(self, db, ident):
        row=db.execute("SELECT payload FROM events WHERE work_id=? AND kind='trip_soft_skip' ORDER BY seq DESC LIMIT 1",(ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def claim_turn(self, context):
        task=str(context.get("task_id") or "")
        if context.get("platform")!="cron" or not task.startswith("cron:"):return None
        parts=task.split(":",2)
        if len(parts)!=3:return None
        _,job_id,execution_id=parts
        for record in self.store.all():
            if (record.get("schedule") or {}).get("job_id")!=job_id:continue
            with self.store.db() as db:
                r=self.store.load(db,record["id"]);w=r.get("worker") or {}
                self.check_derived(r,db)
                require(r["state"] not in TERMINAL and w.get("execution_id")==execution_id and w.get("generation")==r["generation"] and w.get("scope_hash")==r["scope_hash"],"Native worker is not admitted for this generation/scope.")
                require(w.get("session_id") in {None,context["session_id"]},"An occurrence already has a different native session owner.")
                with native_scope(self.home) as jobs:
                    j=jobs.get_job(job_id)
                require(j and j.get("enabled"), "Native schedule was paused before worker admission.")
                w.update(state="running",session_id=context["session_id"],acknowledged_at=now(),fire_owner=(j.get("fire_claim") or {}).get("by"))
                self.store.save(db,r,"wake_claim_acknowledged",{"execution_id":execution_id,"session_id":context["session_id"]})
                return r
        return None

    def doctor(self):
        issues=[]
        with native_scope(self.home) as jobs:
            from cron import executions
            for r in self.store.all():
                if r.get("deadline"):
                    d = r["deadline"]
                    if r["state"] != "stopped" and now() >= d["due_at"] and r["return_state"] not in {"native_delivered", "native_adapter_accepted"}:
                        issues.append(dict(id=r["id"], kind="deadline_return_unconfirmed",
                                           return_state=r["return_state"], reason=d.get("deadline_delivery_obstacle")))
                    continue
                if r["state"] in TERMINAL:
                    if r["state"]=="completed" and r["return_state"] in {"foreground_response_pending","foreground_response_observed","response_ready_for_transport","foreground_return_incomplete","uncertain"}:
                        issues.append(dict(id=r["id"],kind="owed_foreground_return",return_state=r["return_state"],repair="Inspect the existing native response/transport receipt. Do not create a duplicate send."))
                    continue
                s=r.get("schedule") or {};j=jobs.get_job(s["job_id"]) if s.get("job_id") else None
                flight = self.sleeping_reason(r)
                if flight:
                    trip = same_work_trip(self.home, r)
                    if not trip or trip["state"] != "watching":
                        issues.append(dict(id=r["id"],kind="flight_watch_receipt_check",repair=flight))
                if r["state"] not in {"needs_person", "output_ready"} and not flight and (not j or not j.get("enabled")):
                    issues.append(dict(id=r["id"],kind="missing_next_wake",repair="Use schedule on the same assignment after checking native intent and STOP."))
                if s and s.get("state")!="acknowledged":issues.append(dict(id=r["id"],kind="unacknowledged_wake",repair="Reconcile exact native name before creating another job."))
                if r["state"]=="output_ready":issues.append(dict(id=r["id"],kind="owed_return",return_state=r["return_state"],repair="Inspect the original native execution/outbox. Do not create another send."))
                w=r.get("worker") or {}
                if w.get("execution_id"):
                    e=executions.get_execution(w["execution_id"])
                    if not e or e["status"] not in {"claimed","running"}:
                        issues.append(dict(id=r["id"],kind="stranded_worker",native_status=e.get("status") if e else "missing",repair="Retain checked steps; next native occurrence rotates generation. Reconcile any uncertain effect first."))
                for effect in r["effects"].values():
                    if effect["state"] in {"attempted","uncertain","accepted"}:issues.append(dict(id=r["id"],kind="uncertain_effect",effect_id=effect["id"],repair="Read the exact original destination. No resend."))
        return {"checked_at":now(),"issues":issues,"claim":"Narrow execution/scheduler checks only. A healthy timer is not outcome proof."}

    def repair_pending(self,limit=1,only_ids=None):
        """Repair interrupted enrollment/stranded lease within existing saved intent.

        This consumes no source event and adds no timer or new assignment. Explicit
        operator pauses/deletions are reported, never interpreted as faults to undo.
        """
        repaired=[]
        with native_scope(self.home) as jobs:
            from cron import executions
            for original in self.store.all():
                if len(repaired)>=limit:break
                if only_ids is not None and original["id"] not in only_ids:continue
                s=original.get("schedule") or {}
                if original["state"] in TERMINAL|{"needs_person","output_ready"} or not s:continue
                if now()-float(s.get("last_repair_at",0))<60:continue
                j=jobs.get_job(s["job_id"]) if s.get("job_id") else None
                if j is None and s.get("state")=="pending":
                    matches=[item for item in jobs.list_jobs(include_disabled=True) if item.get("name")==s.get("name")]
                    if len(matches)>1:continue
                    j=matches[0] if matches else None
                if j and not j.get("enabled") and j.get("paused_reason")!="Zeus wake intent awaiting durable acknowledgement":continue
                pending=s.get("state")=="pending" or (j and not j.get("enabled") and j.get("paused_reason")=="Zeus wake intent awaiting durable acknowledgement")
                w=original.get("worker") or {}
                e=executions.get_execution(w["execution_id"]) if w.get("execution_id") else None
                stranded=bool(w.get("state") in {"pending","running"} and e and e["status"] not in {"claimed","running"})
                if not pending and not stranded:continue
                if stranded and w.get("state") == "pending":
                    with self.store.db() as db:
                        r = self.store.load(db, original["id"])
                        if r["generation"] == original["generation"] and r.get("worker") == w:
                            self.pause_bound_job(jobs, r, "Zeus preceding occurrence ended without worker admission")
                            self.store.event(db, r["id"], "unadmitted_wake_paused", {"execution_id": w["execution_id"]})
                            repaired.append({"id":r["id"],"repair":"paused unadmitted native occurrence"})
                    continue
                with self.store.db() as db:
                    r=self.store.load(db,original["id"])
                    if r["generation"]!=original["generation"] or r["state"] in TERMINAL:continue
                    r["schedule"]["last_repair_at"]=now()
                    if stranded:
                        r.update(generation=token(),worker=None)
                        if any(item["state"] in {"attempted","accepted","uncertain"} for item in r["effects"].values()):r["state"]="reconcile_first"
                    self.store.save(db,r,"bounded_repair_started",{"enrollment":bool(pending),"stranded_native_status":e.get("status") if e else None})
                try:
                    if pending:
                        result=self.enroll(r["id"],{"generation":r["generation"],"revision":r["revision"]},s["execution_config"])
                    else:result={"id":r["id"],"repair":"stale occurrence fenced; checked progress retained; existing native next wake unchanged"}
                    repaired.append(result)
                except Exception as exc:
                    with self.store.db() as db:
                        r=self.store.load(db,r["id"]);self.store.event(db,r["id"],"bounded_repair_failed",{"reason":str(exc)})
                    repaired.append({"id":r["id"],"repaired":False,"error":str(exc)})
        return {"repairs":repaired,"new_authority_created":False}

# These are the provider's fixed confirmation headings observed in the source workflow.
SKOOL_CONFIRMATIONS = {
    'broadcast': 'Admin broadcast emails are off',
    'digest': 'Weekly digest email is off',
    'daily': 'Daily notifications email is off',
    'event': 'Event reminder email is off',
}

def skool_notice(url):
    from urllib.parse import urlsplit
    u = urlsplit(url)
    return (u.scheme == 'https' and u.netloc == 'www.skool.com' and
            u.path == '/notifications' and bool(u.query) and not u.fragment)

def native_setting_scope(source, items, read_mail=None):
    """Resolve exact links against Gmail records from the mailbox in the original request."""
    import html,re
    from email.utils import parseaddr
    from urllib.parse import urlsplit,parse_qs
    require(source.get('receipt') and re.search(r'\bskool\b',source['text'],re.I),
            'Setting review needs its original Skool request.')
    require(isinstance(items,list) and 0 < len(items) <= 150, 'Supply the listed settings for this review.')
    if read_mail is None:
        # The engine's read-only Gmail reader; `mail.composio_accounts` maps each owner mailbox label to its
        # connected account. A mailbox without a mapping cannot be reviewed.
        intake = str(zeus_settings.ENGINE_DIR / 'intake')
        if intake not in sys.path:
            sys.path.insert(0,intake)
        import run_shadow
        accounts = dict(zeus_settings.get('mail.composio_accounts', {}) or {})
        def read_mail(mailbox, mid):
            require(mailbox in accounts, 'No mail reader is configured for this mailbox.')
            return run_shadow.read_message(accounts[mailbox], mid)
    from hashlib import sha256
    result = {}
    identities = set()
    mailbox = items[0]['mailbox']
    addresses = dict(zeus_settings.OWNER_EMAILS)   # label -> address, from the owner's config
    # Gmail ignores dots in the local part of gmail.com addresses.
    def address(value):
        value = value.strip().lower()
        local, _, domain = value.partition('@')
        return (local.replace('.','')+'@'+domain) if domain=='gmail.com' else value
    request_addresses = {address(a) for a in re.findall(r'[\w.+-]+@[\w.-]+\.\w+',source['text'])}
    require(mailbox in addresses and address(addresses[mailbox]) in request_addresses,'Mailbox must match the owner request.')
    mail = {}
    provider_settings = set()
    account_id = None
    for item in items:
        mid, group, category = item['message_id'], item['community'].strip(), item['category']
        require(item['mailbox']==mailbox and re.fullmatch(r'[a-zA-Z0-9_-]{1,160}', mid)
                and 0 < len(group) <= 160 and category in SKOOL_CONFIRMATIONS,'Invalid setting identity.')
        key = (group.casefold(),category)
        require(key not in identities,'A community and category may be counted only once.')
        identities.add(key)
        if mid not in mail:
            mail[mid] = read_mail(mailbox,mid)
        msg = mail[mid]
        require(msg.get('id')==mid,'Gmail returned a different message.')
        headers={h['name'].lower():h['value'] for h in reversed(msg.get('payload',{}).get('headers',[]))}
        sender=parseaddr(headers.get('from',''))[1].lower().partition('@')[2]
        auth=headers.get('authentication-results','')
        require(sender=='skool.com' or sender.endswith('.skool.com'),'Unexpected sender.')
        require(re.match(r'^\s*mx\.google\.com\s*;',auth,re.I) and any(
            re.search(r'\bdkim=pass\b',c,re.I) and re.search(r'\bheader\.(?:i=@|d=)(?:[\w-]+\.)*skool\.com(?:\s|$)',c,re.I)
            for c in auth.split(';')), 'The message lacks aligned provider authentication.')
        require(address(parseaddr(headers.get('delivered-to',''))[1])==address(addresses[mailbox]),
                'Message does not belong to the authorized account.')
        body = html.unescape(str(msg.get('messageText') or ''))
        links={u for u in re.findall(r'https://[^\s<>"]+',body) if skool_notice(u.rstrip("',)"))}
        links={u.rstrip("',)") for u in links}
        digests={sha256(u.encode()).hexdigest() for u in links}
        chosen=item.get('link_hash') or (next(iter(digests)) if len(digests)==1 else '')
        require(chosen in digests and chosen not in result,'The exact account link must be present once in the review.')
        url=next(u for u in links if sha256(u.encode()).hexdigest()==chosen)
        fields=parse_qs(urlsplit(url).query,keep_blank_values=True)
        require(all(len(fields.get(k,[]))==1 and fields[k][0] for k in ('u','t','g','c','v'))
                and fields['c'][0] in {'5','6','7','14'},'Unrecognized provider setting identity.')
        user,group_id,category_id=(fields[k][0] for k in ('u','g','c'))
        require(account_id is None or user==account_id,'A different provider account was present in this review.')
        account_id=user
        identity=(user,group_id,category_id)
        require(identity not in provider_settings,'Multiple source links describe the same provider setting.')
        provider_settings.add(identity)
        result[chosen]={'provider_identity':sha256(json.dumps(identity).encode()).hexdigest(),'community':group,'category':category,'mailbox':mailbox,'message_id':mid}
    return result

# Fixed, read-only native browser program. It never navigates or clicks. The
# returned fingerprints keep signed URLs and unrelated page content out of work history.
SETTING_PAGE_READ = """import hashlib,json
from urllib.parse import urlsplit,parse_qs,urljoin
href,body = js('[location.href,document.body.innerText]')
u=urlsplit(href)
h=lambda t: hashlib.sha256(t.encode()).hexdigest()
official=u.scheme=='https' and u.netloc=='www.skool.com'
lines=[v.strip() for v in body.splitlines() if v.strip()]
login=official and u.path in ('/login','/signup') and any(v.casefold() in ('log in','sign in','log in to skool') for v in lines)
redirects=[v for k,vs in parse_qs(u.query).items() if k in ('next','redirect','returnUrl') for v in vs]
target=urljoin(href,redirects[0]) if login and len(redirects)==1 else href
v=urlsplit(target)
known=official and v.scheme=='https' and v.netloc=='www.skool.com' and v.path=='/notifications' and bool(v.query) and not v.fragment
heads=('Admin broadcast emails are off','Weekly digest email is off','Daily notifications email is off','Event reminder email is off')
headline=lines[0] if len(lines)==2 and lines[0] in heads and lines[1].startswith('For ') else ''
community=lines[1][4:].removesuffix('.').casefold() if headline else ''
print(json.dumps({'target':h(target) if known else '', 'heading':headline,'community':h(community),'signin':bool(known and login)}))"""

def native_setting_observation(record, dispatch):
    from hashlib import sha256
    # The native tool runs the fixed program under Zeus's own existing browser.
    raw=dispatch('browser_exec',{'code':SETTING_PAGE_READ})
    value=json.loads(raw) if isinstance(raw,str) else raw
    require(isinstance(value,dict) and value.get('execution_success',value.get('success')) is True
            and value.get('exit_code',0)==0 and value.get('outcome')!='refused','No readable current browser page.')
    page=json.loads(value['output'].strip())
    key=page.get('target')
    expected=(record.get('setting_review') or {}).get('targets',{}).get(key)
    require(expected is not None,'This page is outside the frozen account review.')
    if page.get('signin') is True:
        status='signin'
    elif (SKOOL_CONFIRMATIONS[expected['category']]==page.get('heading') and page.get('community')==
          sha256(expected['community'].casefold().encode()).hexdigest()):
        status='off'
    else:
        status='unknown'
    return key,status


# Timed work still uses the existing work row and the native Photon attempt journal.
# Neither the clock nor a loose reply can supply a recipient or permission.
DEADLINE_EMPTY = "I couldn't verify anything in time to give you a checked answer."
DEADLINE_TOOL_RESERVE = 30
DEADLINE_DELIVERY_RESERVE = 8
DEADLINE_RECEIPT_INTERVAL = 30


def deadline_plan():
    steps = [
        ("findings", "Save the first useful findings and their sources, or an initial draft checked against the request."),
        ("answer", "Check the owner-facing answer against the request, verified facts and any timely preference."),
    ]
    return {
        "title": "Timed research or draft",
        "criteria": [{"id": key, "description": text, "artifact": key + ".md",
                      "checks": [{"kind": "min_bytes", "value": 1}]} for key, text in steps],
        "steps": [{"id": key, "description": text, "criteria": [key],
                   "depends_on": ["findings"] if key == "answer" else []} for key, text in steps],
    }


def deadline_request(text, started_at, zone=None):
    """Anchor an explicit hand-back time to the native request, in the owner's time zone."""
    import re
    from datetime import datetime, timedelta

    verbs = r"compare|comparison|research|look up|check|find out|investigate|brief|summari[sz]e|draft|write|outline"
    mode = re.search(r"\b(?:" + verbs + r")\b", text, re.I)
    if not mode:
        return None
    # Do not mistake "my meeting starts in 15 minutes" for the requested time
    # to return the comparison. The last explicit hand-back time wins.
    ending = r"(?=[.!?]?\s*$|[.!?]\s|[,;](?:\s|$))"
    cues = re.compile(r"\b(?:" + verbs + r"|give|send|bring|finish|deliver|return|answer|have it|need it)\b", re.I)
    deadlines = []
    for match in re.finditer(r"\b(?:in|within)\s+(\d+(?:\.\d+)?)\s*(seconds?|minutes?|hours?)" + ending, text, re.I):
        clause = re.split(r"[.!?]\s+|[;\n]", text[:match.start()])[-1]
        num, units = match.groups()
        seconds = float(num) * {"s": 1, "m": 60, "h": 3600}[units[0].lower()]
        if cues.search(clause) and math.isfinite(seconds):
            deadlines.append((match.start(), started_at + seconds))
    if zone is not None:
        pattern = r"\bby\s+(?:(today|tomorrow)\s+)?(?:(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)|(noon|midnight))(?:\s+(today|tomorrow))?"
        for match in re.finditer(pattern + ending, text, re.I):
            clause = re.split(r"[.!?]\s+|[;\n]", text[:match.start()])[-1]
            if not cues.search(clause):
                continue
            day, hour, minute, ampm, named, trailing_day = match.groups()
            hour, minute = int(hour or 12), int(minute or 0)
            if 1 <= hour <= 12 and 0 <= minute < 60:
                date = datetime.fromtimestamp(started_at, zone)
                if (day or trailing_day or "").lower() == "tomorrow":
                    date += timedelta(days=1)
                pm = named.lower() == "noon" if named else ampm[0].lower() == "p"
                due = date.replace(hour=hour % 12 + (12 if pm else 0),
                                   minute=minute, second=0, microsecond=0).timestamp()
                deadlines.append((match.start(), due))
    if not deadlines:
        return None
    due = max(deadlines)[1]
    if due <= started_at:
        return None
    duration = due - started_at
    fallback = due - min(DEADLINE_DELIVERY_RESERVE, duration * .2)
    return {
        "due_at": due, "fallback_at": fallback,
        "research_until": fallback - min(DEADLINE_TOOL_RESERVE, duration * .2),
        "next_check": fallback, "return_id": token(), "started_at": started_at,
        "kind": "draft" if mode[0].lower() in {"draft", "write", "outline"} else "research",
    }


def deadline_reply_key(text):
    import re
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def native_deadline_send(home, origin, operation_id, content):
    """A timed return to the owner is refused on public Hermes.

    Sending it safely needs a durable per-send journal (so an uncertain send is never repeated and a sent one can
    be proven). Public Hermes v0.23.4 has no such journal for send_message, so this fails closed: nothing is sent,
    and the caller records the work as needing the owner instead."""
    require(False, "Timed returns need a durable send journal that this Hermes does not have; nothing was sent.")


def deadline_receipt(home, origin, operation_id, attempt_id=None):
    """No durable send journal on public Hermes: there is never a receipt to find (fail closed)."""
    return None


def deadline_owner_zone(home, at):
    # The Zeus watcher and life-work guard use this same owner clock (engine/ownertime.py).
    import importlib.util
    engine = zeus_settings.ENGINE_DIR / "ownertime.py"
    try:
        spec = importlib.util.spec_from_file_location("zeus_work_ownertime", engine)
        clock = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(clock)
        return clock.zone(at=at, life=home / "workspace/life-engine")
    except (OSError, ValueError, AttributeError):
        return None

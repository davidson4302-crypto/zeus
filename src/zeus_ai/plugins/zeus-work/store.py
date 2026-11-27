"""Profile-local execution projection. External assignment systems remain authoritative.

No model calls, transports, timer, task scanner or memory copy lives in this module.
Every mutation is serialized; a caller's boolean can never substitute for verification.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import re
import stat
from datetime import datetime
from zoneinfo import ZoneInfo


def digest(value):
    return sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def now():
    return time.time()


def token():
    return uuid.uuid4().hex


def destination_name(value):
    require(isinstance(value,str) and value and "\x00" not in value,"Destination must be an exact absolute file path from the authentic request.")
    path=Path(value)
    require(path.is_absolute() and path.anchor=="/" and str(path)==value and ".." not in path.parts and len(path.parts)>1,"Destination must be a canonical absolute file path without path traversal.")
    return path


def source_names_destination(source,value):
    # Match an entire path, not a prefix of a different file. This is only a
    # source-boundary check; interpreting the requested outcome remains required.
    text=str((source or {}).get("text", ""))
    return bool(re.search(r"(?<![\w/.-])"+re.escape(value)+r"(?=$|[\s\"'`),;:\]]|\.(?:\s|$))",text))


def read_destination(value):
    """Read a stable regular file through non-symlink directory descriptors."""
    path=destination_name(value)
    directory=os.open(path.anchor,os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=child
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        try:
            before=os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_size<=4_000_000,"Destination must be a regular file up to 4 MB.")
            with os.fdopen(fd,"rb",closefd=False) as stream:data=stream.read(4_000_001)
            after=os.fstat(fd)
            identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
            require(identity(before)==identity(after) and len(data)==after.st_size,"Destination changed during verification.")
            current=os.stat(path.name,dir_fd=directory,follow_symlinks=False)
            require(stat.S_ISREG(current.st_mode) and identity(current)==identity(after),"Destination was replaced during verification.")
            return data
        finally:os.close(fd)
    finally:os.close(directory)


TERMINAL = {"completed", "stopped", "deadline_partial"}
UNCERTAIN = {"attempted", "uncertain", "accepted"}


def validate_plan(criteria, steps):
    require(isinstance(criteria, list) and criteria, "At least one original-outcome criterion is required.")
    require(isinstance(steps, list) and steps, "At least one concrete step is required.")
    require(len(criteria) <= 64 and len(steps) <= 64, "Split oversized plans into bounded assignments.")
    ids = [s.get("id") for s in steps]
    require(all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids), "Step IDs must be unique.")
    cids = [c.get("id") for c in criteria]
    require(all(isinstance(i, str) and i for i in cids) and len(set(cids)) == len(cids), "Criterion IDs must be unique.")
    for c in criteria:
        require(isinstance(c.get("description"), str) and c["description"].strip(), "Each criterion needs the requested outcome.")
        for check in c.get("checks", []):
            require(check.get("kind") in {"contains", "not_contains", "json_equals", "json_count", "sha256", "min_bytes"}, "Unsupported deterministic check.")
            require("value" in check, "Each check needs an expected value.")
    done = set()
    while len(done) < len(ids):
        ready = [s for s in steps if s["id"] not in done and set(s.get("depends_on", [])) <= done]
        require(ready, "Steps contain a cycle or an unknown dependency.")
        for s in ready:
            require(isinstance(s.get("description"), str) and s["description"].strip(), "Each step needs a description.")
            require(isinstance(s.get("criteria"),list) and s["criteria"],"Each step requires at least one verifiable criterion ID. For a source-reading step, retain a checked local evidence artifact or combine it with the output step. An unproved read cannot be checked.")
            require(set(s.get("criteria", [])) <= set(cids), "Step refers to an unknown criterion.")
            done.add(s["id"])



def owner_message(value, limit=1800):
    """An explicit owner-facing question is separate from private execution notes."""
    if value is None:return None
    require(isinstance(value,str) and 0<len(value.strip())<=limit,"Owner message must be a short plain-language question or decision.")
    value=value.strip()
    require(not re.search(r"cronjob|job_id|stop reminder|zeus[-_ ]work|work_[a-f0-9]{12}|wakeAgent|native turn|deterministic checks?|system prompt|/Users/|\[SILENT\]|do not resend|checkpoint|execution_id",value,re.I),"Keep internal execution instructions out of the owner message. Save them in next_action instead.")
    return value


def deadline_text(value):
    """One plain private message. Artifacts and transport directives never ride in it."""
    text = owner_message(value, limit=6000)
    require(text and not re.search(r"(?im)\b(?:MEDIA:|VOICE:)|\[\[|<file|<attachment", text),
            "Use a plain owner-facing answer without transport directives.")
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(text, force=True, redact_url_credentials=True)


class Store:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.root = self.home / "plugin-data" / "zeus-work"
        require(self.home in self.root.resolve().parents,"Execution storage escaped its native profile.")
        require(not (self.home/"plugin-data").is_symlink() and not self.root.is_symlink(),"Execution storage cannot use symlink ancestry.")
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "state.sqlite3"
        with self.db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS work(id TEXT PRIMARY KEY, source_key TEXT UNIQUE NOT NULL,
                external_key TEXT UNIQUE, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, work_id TEXT,
                at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS derivations(parent_id TEXT NOT NULL,parent_revision INTEGER NOT NULL,
                intent_digest TEXT NOT NULL,work_id TEXT UNIQUE NOT NULL,local_day TEXT NOT NULL,
                UNIQUE(parent_id,parent_revision));
              CREATE TABLE IF NOT EXISTS foreground(conversation_id TEXT PRIMARY KEY,holder TEXT NOT NULL,
                session_id TEXT NOT NULL,turn_id TEXT NOT NULL,observed_at REAL NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        require(not self.root.is_symlink() and self.home in self.root.resolve().parents,"Execution storage escaped its native profile.")
        for suffix in ("","-journal","-wal","-shm"):
            path=Path(str(self.path)+suffix)
            require(not path.is_symlink() and path.resolve().parent==self.root.resolve(),"Database files cannot be symlinks or leave the profile store.")
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, ident, kind, value):
        db.execute("INSERT INTO events(work_id,at,kind,payload) VALUES(?,?,?,?)", (ident, now(), kind, canonical(value)))

    def load(self, db, ident):
        row = db.execute("SELECT payload FROM work WHERE id=?", (ident,)).fetchone()
        require(row, "Unknown work ID.")
        return json.loads(row[0])

    def save(self, db, record, kind, detail=None):
        record["updated_at"] = now()
        db.execute("UPDATE work SET payload=? WHERE id=?", (canonical(record), record["id"]))
        self.event(db, record["id"], kind, detail or {})

    def get(self, ident):
        with self.db() as db:
            return self.load(db, ident)

    def all(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM work ORDER BY rowid DESC")]

    def history(self, ident):
        with self.db() as db:
            return [dict(seq=s, at=a, kind=k, detail=json.loads(p)) for s,a,k,p in db.execute(
                "SELECT seq,at,kind,payload FROM events WHERE work_id=? ORDER BY seq DESC LIMIT 100", (ident,))]

    def destination_sources(self,record):
        return [record["source"]]+[revision["source"] for revision in record.get("revisions",[]) if revision.get("source")]

    def validate_destinations(self,criteria,sources,derived=False):
        for criterion in criteria:
            if "destination" not in criterion:continue
            value=criterion["destination"];destination_name(value)
            require(not derived,"Derived preparation cannot introduce an outside destination.")
            require(any(source_names_destination(source,value) for source in sources),"Destination is not an exact path named in this assignment's authentic original or correction request.")
            require(criterion.get("artifact") and criterion.get("checks"),"A destination needs its exact managed artifact and independently checkable outcome criteria.")

    def require_destination_binding(self,record,value,resolved):
        if not isinstance(value,str):return
        sources=self.destination_sources(record)
        named=next((path for path in (value,resolved) if any(source_names_destination(source,path) for source in sources)),None)
        if named is None:return
        destination_name(named)
        require(value==resolved==named,"Use the exact canonical absolute requested destination. Relative or symlink aliases cannot bypass its criterion binding.")
        if (self.root/"artifacts"/record["id"]) in Path(named).parents:return
        require(any(c.get("destination")==named for c in record["criteria"]),"Before writing this requested destination, revise the SAME zeus_work assignment with a criterion destination equal to the exact original path, plus a managed artifact containing the SAME final bytes. Then retry this file call. Checkpoint and completion will independently compare both files; a narrative receipt is insufficient. No new user approval is needed.")

    def create(self, args, source, profile, setting_targets=None, deadline=None):
        validate_plan(args.get("criteria"), args.get("steps"))
        require(source.get("session_id") and source.get("turn_id") and source.get("text"), "Authentic current user source required.")
        self.validate_destinations(args["criteria"],[source])
        require(isinstance(args.get("title"), str) and args["title"].strip(), "Title required.")
        continuation=args.get("continuation","automatic")
        require(continuation in {"automatic","never"},"Continuation must be automatic or never.")
        if continuation=="never":
            require(re.search(r"\b(no background|never continue|do not continue|don't continue|do not schedule|don't schedule|no automatic|stop after this turn|do not keep working)\b",source["text"],re.I),"Disabling continuation needs the user's explicit no-background instruction.")
        external = args.get("external_ref")
        if external:
            require(set(external) <= {"system", "id", "url"} and external.get("system") and external.get("id"), "Use exact existing task-system reference.")
        source_key = digest([profile, source["session_id"], source["turn_id"]])
        external_key = canonical([external["system"],external["id"]]) if external else None
        with self.db() as db:
            row = db.execute("SELECT payload FROM work WHERE source_key=? OR external_key=?", (source_key, external_key)).fetchone()
            if row:
                return dict(json.loads(row[0]), deduplicated=True)
            ident = "work_" + token()[:12]
            record = dict(id=ident, title=args["title"], owner=profile, source=source,
                revisions=[], revision=1, generation=token(), state="open", criteria=args["criteria"],
                steps=[dict(s, state="pending") for s in args["steps"]], external_ref=external,
                origin_source_ref=args.get("origin_source_ref"), next_action=args["steps"][0]["description"],
                checkpoint=None, evidence={}, effects={}, schedule=None, worker=None,
                continuation_intent={"allowed":continuation!="never","policy":"finish_accepted_substantive_work","source_sha256":digest(source)},
                return_state="not_prepared", created_at=now(), updated_at=now())
            if deadline:
                record["deadline"] = deadline
                record["continuation_intent"].update(allowed=False, policy="timed_return_uses_saved_work")
            if setting_targets:
                record["setting_review"] = {"revision":1,"generation":record["generation"],
                                            "targets":setting_targets,"observations":{}}
            record["scope_hash"] = digest([record["source"], record["criteria"], record["steps"], external])
            db.execute("INSERT INTO work VALUES(?,?,?,?)", (ident, source_key, external_key, canonical(record)))
            self.event(db, ident, "created", {"revision":1,"source_sha256":digest(source)})
            return record

    def create_derived(self,args,parent_id,trigger,policy):
        """Internal preparation borrows a bounded parent scope, never a human source slot."""
        validate_plan(args.get("criteria"),args.get("steps"))
        self.validate_destinations(args["criteria"],[],derived=True)
        require(policy.get("enabled") is True and policy.get("policy_id") and policy.get("source_ref"),"Owner-approved preparation policy is not enabled and sourced.")
        require(args.get("kind") in {"quiet_review","local_preparation"},"Only quiet review or local preparation is permitted.")
        intent=args.get("intent_digest")
        require(isinstance(intent,str) and len(intent)==64 and all(c in "0123456789abcdef" for c in intent),"Use the SHA-256 of the observed source intent.")
        require(isinstance(args.get("title"),str) and args["title"].strip(),"Preparation title required.")
        require(not args.get("external_ref"),"Derived preparation cannot own an external assignment or effect.")
        day=datetime.fromtimestamp(now(),ZoneInfo(policy.get("timezone","UTC"))).date().isoformat()
        daily=min(3,max(0,int(policy.get("max_per_day",3))))
        with self.db() as db:
            parent=self.load(db,parent_id)
            require(parent["state"] in {"open","completed","output_ready"},"Parent must be active without a blocker, or completed. STOP/uncertainty does not authorize preparation.")
            require(not parent.get("derivation"),"Derived preparation cannot create another generation of preparation.")
            prior=db.execute("SELECT intent_digest,work_id FROM derivations WHERE parent_id=? AND parent_revision=?",(parent_id,parent["revision"])).fetchone()
            if prior:
                require(prior[0]==intent,"This parent revision already has its one preparation assignment.")
                return dict(self.load(db,prior[1]),deduplicated=True)
            count=db.execute("SELECT COUNT(*) FROM derivations WHERE local_day=?",(day,)).fetchone()[0]
            require(count<daily,"The preparation budget for this profile/day is exhausted.")
            ident="work_"+token()[:12]
            derivation={"parent_id":parent_id,"parent_revision":parent["revision"],"parent_scope_hash":parent["scope_hash"],"original_authority_ref":{"work_id":parent_id,"source_sha256":digest(parent["source"])},"kind":args["kind"],"intent_digest":intent,"trigger":trigger,"policy":{"policy_id":policy["policy_id"],"source_ref":policy["source_ref"],"sha256":digest(policy)},"local_day":day,"scope":"Only review the original parent request/criteria and its managed artifacts; write local preparation artifacts. No memory capture, outside effects or notifications."}
            source={"origin":"derived_preparation","parent_authority_ref":derivation["original_authority_ref"],"internal_trigger_ref":args.get("origin_source_ref"),"human_author_claimed":False,"text":"Internal local preparation under the referenced original parent only."}
            r=dict(id=ident,title=args["title"],owner=parent["owner"],source=source,derivation=derivation,
                revisions=[],revision=1,generation=token(),state="open",criteria=args["criteria"],steps=[dict(s,state="pending") for s in args["steps"]],external_ref=None,
                origin_source_ref=args.get("origin_source_ref"),next_action=args["steps"][0]["description"],checkpoint=None,evidence={},effects={},schedule=None,worker=None,
                continuation_intent={"allowed":True,"policy":"owner_approved_local_preparation","policy_sha256":digest(policy)},
                return_state="not_prepared",created_at=now(),updated_at=now())
            r["scope_hash"]=digest([derivation,args["criteria"],args["steps"]])
            source_key="derived:"+digest([parent_id,parent["revision"],intent])
            db.execute("INSERT INTO work VALUES(?,?,NULL,?)",(ident,source_key,canonical(r)))
            db.execute("INSERT INTO derivations VALUES(?,?,?,?,?)",(parent_id,parent["revision"],intent,ident,day))
            self.event(db,ident,"derived_preparation_created",derivation)
            return r

    def parent_for(self,record,db=None):
        ref=record.get("derivation")
        if not ref:return None
        parent=self.load(db,ref["parent_id"]) if db is not None else self.get(ref["parent_id"])
        require(parent["state"]!="stopped" and parent["revision"]==ref["parent_revision"] and parent["scope_hash"]==ref["parent_scope_hash"],"Original parent was stopped or superseded. Derived preparation is fenced.")
        require(digest(parent["source"])==ref["original_authority_ref"]["source_sha256"],"Original parent authority changed.")
        return parent

    def fence(self, record, binding, allow_terminal=False, db=None):
        require(record["generation"] == binding["generation"] and record["revision"] == binding["revision"], "Stale generation or corrected scope. Re-read current work.")
        require(allow_terminal or record["state"] not in TERMINAL|{"output_ready"}, "Work is stopped or output is already sealed.")
        self.parent_for(record,db)
        if binding.get("execution_id"):
            worker = record.get("worker") or {}
            require(worker.get("execution_id") == binding["execution_id"] and worker.get("session_id") == binding.get("session_id"), "This occurrence does not own the work.")

    def mutate(self, ident, binding, kind, fn, allow_terminal=False):
        with self.db() as db:
            record = self.load(db, ident)
            self.fence(record, binding, allow_terminal,db)
            result = fn(record)
            self.save(db, record, kind)
            return record if result is None else result

    def revise(self, ident, args, source):
        validate_plan(args.get("criteria"), args.get("steps"))
        with self.db() as db:
            r = self.load(db, ident)
            require(r["state"] != "stopped", "STOP persists. Explicit resume of the same job is required first.")
            self.validate_destinations(args["criteria"],self.destination_sources(r)+[source],derived=bool(r.get("derivation")))
            r["revisions"].append(dict(revision=r["revision"], source=source, old_scope_hash=r["scope_hash"], old_criteria=r["criteria"], old_steps=r["steps"]))
            r["revoked_execution_ids"]=list(dict.fromkeys(r.get("revoked_execution_ids",[])+[x for x in [(r.get("worker") or {}).get("execution_id"),r.get("return_execution_id")] if x]))
            r.update(revision=r["revision"]+1, generation=token(), state="open", criteria=args["criteria"],
                steps=[dict(s,state="pending") for s in args["steps"]], evidence={}, worker=None,
                next_action=args["steps"][0]["description"], return_state="not_prepared")
            if r.get("deadline"):
                # A correction has its own intent. Prior timed drafts/questions cannot follow it.
                r.setdefault("prior_deadlines", []).append(r.pop("deadline"))
                r.pop("return_execution_id", None)
            r["scope_hash"] = digest([r["source"], source, r["criteria"], r["steps"], r["external_ref"]])
            self.save(db, r, "scope_corrected", {"revision":r["revision"],"source_sha256":digest(source)})
            return r

    def stop(self, ident, source):
        with self.db() as db:
            r = self.load(db, ident)
            r["revoked_execution_ids"]=list(dict.fromkeys(r.get("revoked_execution_ids",[])+[x for x in [(r.get("worker") or {}).get("execution_id"),r.get("return_execution_id")] if x]))
            r.update(state="stopped", generation=token(), worker=None, stopped_at=now(), stop_source=source)
            self.save(db, r, "stop_committed", {"source_sha256":digest(source)})
            return r

    def resume(self, ident, source):
        with self.db() as db:
            r = self.load(db, ident)
            require(r["state"] == "stopped", "Only a stopped assignment can be resumed.")
            generation=token()
            authorization={"stopped_generation":r["generation"],"generation":generation,"source_sha256":digest(source)}
            if r.get("deadline"):
                r.setdefault("prior_deadlines", []).append(r.pop("deadline"))
                r.update(return_state="not_prepared")
                r.pop("return_execution_id", None)
            r.update(state="open",generation=generation,worker=None,resume_source=source,resume_authorization=authorization)
            self.save(db,r,"resumed",{"source_sha256":digest(source)})
            return r

    def artifact_path(self, ident, name):
        require(isinstance(name,str) and name and not Path(name).is_absolute(), "Use a relative artifact name inside this work folder.")
        folder = self.root / "artifacts" / ident
        path = folder / name
        require(self.root.resolve() in folder.resolve().parents and folder.resolve() in path.resolve().parents,"Artifact path must stay inside its profile and work folder.")
        current=path
        while current!=self.root:
            require(not current.is_symlink(),"Artifact path cannot have symlink ancestry.")
            require(current!=current.parent,"Artifact path escaped its profile store.")
            current=current.parent
        folder.mkdir(parents=True,exist_ok=True)
        return path

    def write_artifact(self, ident, binding, name, content):
        require(isinstance(content,str) and len(content.encode()) <= 4_000_000, "Artifact must be UTF-8 text up to 4 MB.")
        with self.db() as db:
            r = self.load(db,ident); self.fence(r,binding,db=db)
            path = self.artifact_path(ident,name); path.parent.mkdir(parents=True,exist_ok=True)
            temp = path.with_name(path.name+"."+token()+".tmp")
            with temp.open("xb") as f:
                f.write(content.encode()); f.flush(); os.fsync(f.fileno())
            os.chmod(temp,0o600); os.replace(temp,path)
            r["evidence"] = {}; r["return_state"] = "not_prepared"
            self.save(db,r,"artifact_written",{"name":name,"sha256":digest(content.encode())})
            return {"path":str(path),"sha256":digest(content.encode()),"bytes":len(content.encode())}

    def verify_record(self, record):
        results = {}
        for criterion in record["criteria"]:
            result = dict(description=criterion["description"], passed=False, revision=record["revision"], checked_at=now())
            try:
                require(criterion.get("artifact") and criterion.get("checks"), "No deterministic artifact checks. Criterion remains unverified.")
                path = self.artifact_path(record["id"], criterion["artifact"])
                require(path.is_file() and path.stat().st_size <= 4_000_000, "Artifact missing or above 4 MB verification bound.")
                data = path.read_bytes(); text = data.decode("utf-8")
                checks = []
                for c in criterion["checks"]:
                    kind,value = c["kind"],c["value"]
                    if kind == "contains": passed = isinstance(value,str) and value in text
                    elif kind == "not_contains": passed = isinstance(value,str) and value not in text
                    elif kind == "sha256": passed = digest(data) == value
                    elif kind == "min_bytes": passed = isinstance(value,int) and len(data) >= value
                    else:
                        selected = json.loads(text)
                        for part in c.get("path", []): selected = selected[part]
                        passed = selected == value if kind == "json_equals" else len(selected) == value
                    checks.append(dict(kind=kind,passed=bool(passed)))
                result.update(passed=all(c["passed"] for c in checks),path=str(path),sha256=digest(data),bytes=len(data),checks=checks)
                if "destination" in criterion:
                    # A content receipt in the managed folder cannot stand in for
                    # the user's actual requested file. Re-read it every time.
                    result["passed"]=False
                    self.validate_destinations([criterion],self.destination_sources(record),derived=bool(record.get("derivation")))
                    destination_data=read_destination(criterion["destination"])
                    matches=destination_data==data
                    result["destination"]={"path":criterion["destination"],"sha256":digest(destination_data),"bytes":len(destination_data),"matches_managed_bytes":matches}
                    result["passed"]=all(c["passed"] for c in checks) and matches
                    if not matches:result["reason"]="Requested destination bytes differ from the managed artifact. A separate narrative receipt is not the requested output."
            except (ValueError, OSError, KeyError, IndexError, TypeError) as exc:
                result["reason"] = str(exc)
            results[criterion["id"]] = result
        return results

    def verify(self, ident, binding, complete=False, return_context=None):
        with self.db() as db:
            r=self.load(db,ident); self.fence(r,binding,db=db)
            r["evidence"]=self.verify_record(r)
            passed=all(e["passed"] for e in r["evidence"].values())
            pending_effects=any(e["state"] in UNCERTAIN for e in r["effects"].values())
            if complete:
                require(passed,"Original-outcome artifact checks have not all passed.")
                require(all(s["state"]=="checked" for s in r["steps"]),"There are unchecked dependent steps.")
                require(not pending_effects,"External effect needs destination reconciliation before completion.")
                if return_context and return_context["kind"]=="deadline":
                    timing = r["deadline"]
                    require(now() < timing["fallback_at"], "The deadline return already owns this answer.")
                    draft = self.checked_deadline_text(r, "answer", r["evidence"]["answer"])
                    checked = next(s for s in r["steps"] if s["id"] == "answer")
                    require(draft and checked["evidence"]["answer"]["sha256"] == r["evidence"]["answer"]["sha256"],
                            "Check the current owner-facing answer before completing.")
                    question = timing.get("question") or {}
                    require(not question.get("answer_id") or question["answer_id"] == checked.get("answer_seen")
                            == return_context.get("answer_seen"),
                            "A timely preference arrived. Read it and recheck the answer before completing.")
                    self.prepare_deadline_return(r, draft, "complete")
                    self.save(db,r,"deadline_normal_result_prepared",{"sha256":digest(draft.encode())})
                    return r
                r.update(state="completed",completed_at=now(),return_state="prepared")
                if return_context and return_context["kind"]=="worker":
                    require(binding.get("execution_id"),"Native worker return needs its actual occurrence.")
                    r.update(state="output_ready",return_state="awaiting_native_return",return_execution_id=binding["execution_id"])
                elif return_context and return_context["kind"]=="foreground":
                    r.update(worker=None,return_state="foreground_response_pending",foreground_return_source=return_context["source"])
                elif return_context and return_context["kind"]=="local_preparation":
                    r.update(worker=None,return_state="local_preparation_available")
                else:r["worker"]=None
            self.save(db,r,"completed" if complete else "verified",{"passed":passed,"revision":r["revision"]})
            return r

    def checkpoint(self, ident, binding, args):
        def apply(r):
            step=next((s for s in r["steps"] if s["id"]==args.get("step_id")),None)
            require(step,"Unknown step.")
            require(all(s["state"]=="checked" for s in r["steps"] if s["id"] in step.get("depends_on",[])),"Dependencies are not checked yet.")
            evidence=self.verify_record(r)
            cids=step.get("criteria",[])
            require(cids and all(evidence[c]["passed"] for c in cids),"A checked step needs passing independent artifact criteria.")
            step["state"]="checked"; step["checked_at"]=now(); step["evidence"]={k:evidence[k] for k in cids}
            r.update(evidence=evidence,checkpoint=args.get("note"),next_action=args.get("next_action") or "Verify the final original outcome.",state="open")
            if r.get("deadline"):
                timing = r["deadline"]
                require(now() < timing["fallback_at"], "The deadline return already owns this answer.")
                draft = self.checked_deadline_text(r, step["id"], evidence[step["id"]])
                if draft:
                    step["answer_seen"] = args.get("_answer_seen")
                    missing = deadline_text(args["missing"]) if args.get("missing") else None
                    require(not missing or len(missing) <= 300, "Keep open points to one short line.")
                    timing["best"] = {"text": draft, "sha256": digest(draft.encode()),
                                      "artifact": step["id"], "checked_at": step["checked_at"],
                                      "answer_seen": step["answer_seen"], "missing": missing}
        return self.mutate(ident,binding,"checkpoint",apply)

    def checked_deadline_text(self, record, step_id, evidence):
        if step_id not in {"findings", "answer"} or not evidence.get("passed"):
            return None
        data = Path(evidence["path"]).read_bytes()
        require(digest(data) == evidence["sha256"], "Checked artifact changed. Recheck the new bytes.")
        text = deadline_text(data.decode("utf-8"))
        timing = record["deadline"]
        if timing["kind"] == "research":
            reads = timing.get("read_evidence", {})
            require(reads, "Check at least one source using an existing read tool before saving verified findings.")
            links = {link for evidence in reads.values() for link in evidence}
            if links:
                require(any(link in text for link in links), "Cite at least one source from the actual lookup.")
        return text

    @staticmethod
    def prepare_deadline_return(record, text, outcome):
        timing = record["deadline"]
        # This is the same return slot for both contenders. It is set only under the
        # work transaction, before the existing native sender can claim the effect.
        require(not timing.get("result"), "The existing return already owns this assignment.")
        timing["result"] = {"text": text, "outcome": outcome, "prepared_at": now(),
                            "sha256": digest(text.encode())}
        timing["next_check"] = now()
        record.update(state="output_ready", worker=None, return_state="prepared",
                      return_execution_id=timing["return_id"])

    def report(self, ident, binding, args):
        def apply(r):
            require(args.get("disposition") in {"unfinished","needs_person","uncertain"},"Use unfinished, needs_person or uncertain. Completion is independently verified.")
            require(args.get("next_action"),"Save the next concrete action.")
            message=owner_message(args.get("owner_message"))
            require(args["disposition"]!="needs_person" or message,"needs_person requires a plain owner_message explaining the actual decision or access needed. Builder work is unfinished, not an owner dependency.")
            r.update(state="needs_person" if args["disposition"]=="needs_person" else "reconcile_first" if args["disposition"]=="uncertain" else "open",next_action=args["next_action"],checkpoint=args.get("note"),owner_message=message,owner_message_revision=r["revision"],owner_message_generation=r["generation"])
        return self.mutate(ident,binding,"report",apply)

    def effect_prepare(self, ident,binding,tool,args,reader,read_args,checks):
        """Store intent before the native call; callers cannot mark its outcome delivered."""
        def apply(r):
            key=digest([r["revision"],tool,args])
            require(key not in r["effects"],"Exact effect already exists. Inspect/reconcile it; never blindly resend.")
            require(not any(e["state"] in UNCERTAIN for e in r["effects"].values()),"An earlier effect is uncertain. Reconcile it first.")
            r["effects"][key]=dict(id=key,tool=tool,args=args,reader=reader,read_args=read_args,checks=checks,state="prepared",revision=r["revision"],generation=r["generation"],scope_hash=r["scope_hash"],at=now())
            return r["effects"][key]
        return self.mutate(ident,binding,"effect_prepared",apply)

    def observe_setting(self, ident, binding, key, state):
        def save(record):
            scope = record.get("setting_review") or {}
            require(scope.get("generation")==record["generation"] and scope.get("revision")==record["revision"]
                    and key in scope.get("targets",{}), "This setting belongs to another review.")
            require(state in {"off","signin","unknown"}, "Invalid provider observation.")
            scope["observations"][key] = state
            return {"observed":state,"reviewed":len(scope["observations"]),"listed":len(scope["targets"])}
        return self.mutate(ident,binding,"provider_setting_observed",save)

    def verified_setting_status(self, record):
        scope=record.get("setting_review") or {}
        if not scope or scope.get("revision")!=record["revision"] or scope.get("generation")!=record["generation"]:
            return None
        states=[scope["observations"].get(key) for key in scope["targets"]]
        off,signin,total=states.count("off"),states.count("signin"),len(states)
        unknown=total-off-signin
        parts=[f"{off} of {total} listed email settings are verified off."]
        if signin:
            parts.append(f"{signin} {'needs' if signin==1 else 'need'} sign-in.")
        if unknown:
            parts.append(f"{unknown} still {'needs' if unknown==1 else 'need'} verification.")
        return " ".join(parts)

    def activity(self):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES('last_human',?)",(str(now()),))

    def foreground_lease(self,conversation_id,holder,session_id,turn_id):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO foreground VALUES(?,?,?,?,?)",(conversation_id,holder,session_id,turn_id,now()))
            db.execute("DELETE FROM foreground WHERE conversation_id NOT IN (SELECT conversation_id FROM foreground ORDER BY observed_at DESC LIMIT 128)")

    def last_human(self):
        with self.db() as db:
            row=db.execute("SELECT value FROM meta WHERE key='last_human'").fetchone()
            return float(row[0]) if row else 0

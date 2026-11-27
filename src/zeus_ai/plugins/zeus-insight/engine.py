"""Derived preparation ledger. Never a canonical fact store, task queue or sender."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KINDS = {"idea", "lesson", "briefing", "feed", "procedure", "memory_review", "routine"}
TERMINAL = {"dismissed", "expired", "retired"}


_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))
import settings as zeus_settings  # noqa: E402


def _hour(h):
    return f"{(h % 12) or 12} {'a.m.' if h < 12 else 'p.m.'}"


QUIET_LABEL = f"{_hour(zeus_settings.QUIET_START)} to {_hour(zeus_settings.QUIET_END)}"


# The owner's clock and quiet-hours window come from the life engine's own ownertime module.
def owner_clock():
    import ownertime
    return ownertime


def digest(value):
    data = value if isinstance(value, bytes) else str(value).encode()
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps require an explicit timezone")
    return parsed


def words(value):
    return set(re.findall(r"[a-z0-9]{3,}", str(value).lower()))


class Ledger:
    def __init__(self, conn):
        self.db = conn
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
          sha256 TEXT NOT NULL, observed_at TEXT NOT NULL, session TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS insights(
          id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
          state TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, insight_id TEXT, kind TEXT NOT NULL,
          detail TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quiet_reviews(
          id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, parent_revision INTEGER NOT NULL,
          intent_digest TEXT UNIQUE NOT NULL, source_ids TEXT NOT NULL, state TEXT NOT NULL,
          created_at TEXT NOT NULL, result TEXT NOT NULL,
          UNIQUE(parent_id,parent_revision));
        """)
        self.db.commit()

    def event(self, item_id, kind, detail):
        ident = str(uuid.uuid4())
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                        (ident, item_id, kind, json.dumps(detail, sort_keys=True), now()))
        return ident

    def source(self, kind, ref, sha256, session):
        # Retain provenance, not source content or personal facts.
        ident = digest(f"{kind}|{ref}|{sha256}|{session}")[:24]
        self.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?)",
                        (ident, kind, ref, sha256, now(), session))
        self.db.commit()
        return ident

    def sources(self, session):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM sources WHERE session=? ORDER BY observed_at DESC LIMIT 12", (session,))]

    def get(self, ident):
        row = self.db.execute("SELECT * FROM insights WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise ValueError("Unknown insight ID")
        item = json.loads(row["body"])
        item.update(id=row["id"], state=row["state"], created_at=row["created_at"], expires_at=row["expires_at"])
        if item["state"] not in TERMINAL and timestamp(item["expires_at"]) <= datetime.now(timezone.utc):
            self.db.execute("UPDATE insights SET state='expired' WHERE id=?", (ident,))
            self.event(ident, "expired", {"reason": "freshness deadline passed"})
            self.db.commit()
            item["state"] = "expired"
        item["sources"] = [dict(self.db.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone())
                           for sid in item["source_ids"]]
        return item

    def save(self, item):
        body = {k: v for k, v in item.items() if k not in {"id", "state", "created_at", "expires_at", "sources"}}
        self.db.execute("UPDATE insights SET body=?,state=? WHERE id=?",
                        (json.dumps(body, sort_keys=True), item["state"], item["id"]))

    def propose(self, args, current):
        kind = args.get("kind")
        if kind not in KINDS:
            raise ValueError("Use a supported derived insight kind, never personal facts")
        title, guidance, scope = [str(args.get(k, "")).strip() for k in ("title", "guidance", "scope")]
        if not title or not guidance or not scope or len(title) > 160 or len(guidance) > 4000 or len(scope) > 300:
            raise ValueError("Require title <=160, procedural guidance <=4000 and scope <=300 characters")
        deadline = timestamp(args.get("expires_at"))
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0 or remaining > 366 * 86400:
            raise ValueError("Expiry must be future and within one year")
        if kind in {"feed", "briefing"} and remaining > (7 if kind == "feed" else 30) * 86400:
            raise ValueError("Feed freshness is bounded to seven days; briefings to thirty days")
        source_ids = list(dict.fromkeys(args.get("source_ids", [])))
        if not source_ids or len(source_ids) > 8:
            raise ValueError("Link 1-8 observed native source receipts; call sources first")
        for sid in source_ids:
            row = self.db.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise ValueError("Unobserved source receipt")
        if kind == "lesson":
            quote = str(args.get("correction_quote", "")).strip()
            if len(quote) < 8 or quote not in current["message"] or current["source_id"] not in source_ids:
                raise ValueError("A lesson needs an exact current admitted native correction quote and its source receipt")
            # Persist the quote digest only. Genuine source; interpretation remains a proposal.
            correction = {"quote_sha256": digest(quote), "source_id": current["source_id"],
                          "origin": current.get("origin", "unspecified"),
                          "human_author_claimed": current.get("human_author_claimed", False),
                          "classification": "agent interpretation of admitted native correction; source kind determines author provenance"}
        else:
            correction = None
        score = args.get("ranking", {})
        for key in ("relevance", "feasibility", "effort_saved"):
            value = score.get(key)
            if type(value) is not int or not 0 <= value <= 5:
                raise ValueError("Ranking values must be integers from 0 to 5")
        rationale = str(score.get("reason", "")).strip()
        if not rationale or len(rationale) > 500:
            raise ValueError("Ranking needs a short explicit reason")
        fingerprint = digest("|".join((kind, " ".join(sorted(words(title))), " ".join(sorted(words(scope))))))
        duplicate = self.db.execute("SELECT id FROM insights WHERE fingerprint=?", (fingerprint,)).fetchone()
        if duplicate:
            return {"duplicate": True, "item": self.get(duplicate["id"])}
        item = {"kind": kind, "title": title, "guidance": guidance, "scope": scope,
                "source_ids": source_ids, "ranking": score,
                "score": 2 * score["relevance"] + score["feasibility"] + score["effort_saved"],
                "score_formula": "2*relevance + feasibility + effort_saved (each 0..5)",
                "correction": correction, "artifact": None, "work_ids": [],
                "origin_session": current["session_id"], "routine_enabled": False,
                "topic": str(args.get("topic") or scope).strip().lower()[:100], "urgency": None}
        urgency = args.get("urgency")
        if urgency:
            if not isinstance(urgency, dict) or urgency.get("source_id") not in source_ids or not str(urgency.get("reason", "")).strip():
                raise ValueError("Urgency needs a linked observed source and explicit reason")
            urgency_deadline = timestamp(urgency.get("deadline"))
            item["urgency"] = {"deadline": urgency_deadline.isoformat(), "reason": str(urgency["reason"])[:500],
                               "source_id": urgency["source_id"], "classification": "agent assessment of linked source, not independent deadline verification"}
        ident = str(uuid.uuid4())
        self.db.execute("INSERT INTO insights VALUES (?,?,?,?,?,?)",
                        (ident, fingerprint, json.dumps(item, sort_keys=True), "proposed", now(), deadline.isoformat()))
        self.event(ident, "proposed", {"source_ids": source_ids, "classification": "proposal, not established fact"})
        self.db.commit()
        return {"duplicate": False, "item": self.get(ident)}

    def transition(self, ident, state, current, reason):
        item = self.get(ident)
        if item["state"] in TERMINAL:
            raise ValueError("Terminal insight cannot be revived; propose a materially different insight")
        allowed = {"accepted", "dismissed", "retired", "expired", "adopted"}
        if state not in allowed:
            raise ValueError("Unsupported transition")
        if state == "adopted" and item["kind"] not in {"lesson", "procedure"}:
            raise ValueError("Only procedural lessons/procedures can be adopted")
        if state == "adopted" and item["kind"] == "procedure" and not item.get("promotion_evidence"):
            raise ValueError("Procedure needs two distinct verified successful work outcomes before adoption")
        if state in {"accepted", "adopted"}:
            quote = str(reason).strip()
            if len(quote) < 3 or quote not in current["message"]:
                raise ValueError("Acceptance/adoption needs a current admitted native quote, not an invented approval")
            # Author says the source words; semantic interpretation remains explicit in audit.
            detail = {"native_source": current["source_id"], "quote_sha256": digest(quote),
                      "origin": current.get("origin", "unspecified"),
                      "human_author_claimed": current.get("human_author_claimed", False),
                      "meaning": "agent interpreted the quoted admitted native instruction"}
        else:
            detail = {"reason": str(reason)[:500], "native_source": current["source_id"]}
        item["state"] = state
        self.save(item)
        self.event(ident, state, detail)
        self.db.commit()
        return self.get(ident)

    def listing(self, query="", limit=8, states=None):
        # Filter eligibility before either bounded candidate selection or ranking.
        if states is None:
            rows = self.db.execute("SELECT id FROM insights ORDER BY created_at DESC LIMIT 250")
        else:
            states = tuple(states)
            if not states or any(state not in {"proposed", "accepted", "adopted", "dismissed", "expired", "retired"} for state in states):
                raise ValueError("Unsupported insight state filter")
            placeholders = ",".join("?" for _ in states)
            rows = self.db.execute("SELECT id FROM insights WHERE state IN (" + placeholders + ") ORDER BY created_at DESC LIMIT 250", states)
        ids = [row[0] for row in rows]
        result = []
        query_words = words(query)
        for ident in ids:
            item = self.get(ident)
            if item["state"] in TERMINAL:
                continue
            overlap = len(query_words & words(item["title"] + " " + item["scope"] + " " + item["guidance"]))
            if query_words and not overlap:
                continue
            item["query_overlap"] = overlap
            result.append(item)
        return sorted(result, key=lambda i: (i["query_overlap"], i["score"], i["created_at"]), reverse=True)[:limit]

    def artifact(self, ident, path, checks, roots):
        item = self.get(ident)
        if item["state"] in TERMINAL:
            raise ValueError("Expired/dismissed insight cannot become ready")
        target = Path(path).expanduser().resolve(strict=True)
        if not roots or not any(target.is_relative_to(Path(root).expanduser().resolve()) for root in roots):
            raise ValueError("Artifact is outside configured preparation roots")
        if not target.is_file() or target.stat().st_size > 1024 * 1024:
            raise ValueError("Require a regular UTF-8 artifact no larger than 1 MiB")
        raw = target.read_bytes()
        text = raw.decode("utf-8")
        if not checks or len(checks) > 12 or any(not str(c).strip() or str(c) not in text for c in checks):
            raise ValueError("Every declared content check must match actual artifact text")
        # Ensure the artifact carries retrievable source receipts, not just a title.
        if not all(sid in text for sid in item["source_ids"]):
            raise ValueError("Artifact must include each linked source receipt ID")
        item["artifact"] = {"path": str(target), "sha256": digest(raw), "bytes": len(raw), "checked_at": now(),
                            "checks": list(checks), "proof": "file exists, hash, required text and source IDs checked; quality not independently judged"}
        self.save(item)
        self.event(ident, "artifact_checked", item["artifact"])
        self.db.commit()
        return item["artifact"]

    def artifact_current(self, item):
        value = item.get("artifact")
        if not value:
            return False
        path = Path(value["path"])
        return path.is_file() and path.stat().st_size <= 1024 * 1024 and digest(path.read_bytes()) == value["sha256"]

    def policy(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='attention'").fetchone()
        value = {"muted": False, "muted_topics": [], "urgent_within_hours": 2}
        if row:
            value.update({k:v for k,v in json.loads(row[0]).items() if k in value})
        return value

    def set_policy(self, values, current, quote):
        if not quote or quote not in current["message"]:
            raise ValueError("Attention preference change requires an exact current admitted native quote")
        if {"quiet_start", "quiet_end", "timezone"} & values.keys():
            raise ValueError(f"{zeus_settings.AGENT_NAME} follows the owner's {QUIET_LABEL} quiet hours")
        value = self.policy()
        if not isinstance(values.get("muted", value["muted"]), bool):
            raise ValueError("muted must be boolean")
        topics = values.get("muted_topics", value["muted_topics"])
        if not isinstance(topics, list) or len(topics) > 32 or any(not isinstance(topic, str) or not topic.strip() or len(topic) > 100 for topic in topics):
            raise ValueError("muted_topics must contain at most 32 exact topic labels")
        urgent = values.get("urgent_within_hours", value["urgent_within_hours"])
        if type(urgent) is not int or not 0 <= urgent <= 12:
            raise ValueError("urgent_within_hours must be an integer 0..12; zero disables urgency marking")
        value.update({key: values[key] for key in value if key in values})
        value["muted_topics"] = sorted(set(topic.strip().lower() for topic in topics))
        self.db.execute("INSERT OR REPLACE INTO settings VALUES ('attention',?)", (json.dumps(value),))
        self.event(None, "attention_policy", {"policy": value, "source_id": current["source_id"]})
        self.db.commit()
        return {"policy": value, "work_stop_changed": False}

    def attention(self, ident, current, requested=False):
        item = self.get(ident)
        policy = self.policy()
        instant = datetime.now(timezone.utc)
        quiet = owner_clock().alert_open(instant) > instant
        offered = self.db.execute("SELECT 1 FROM events WHERE insight_id=? AND kind='return_prepared' LIMIT 1", (ident,)).fetchone()
        urgency = item.get("urgency")
        hours_left = (timestamp(urgency["deadline"]) - datetime.now(timezone.utc)).total_seconds() / 3600 if urgency else None
        urgent = bool(urgency and policy["urgent_within_hours"] > 0 and 0 < hours_left <= policy["urgent_within_hours"])
        reason = None
        if item["state"] in TERMINAL:
            reason = item["state"]
        elif not self.artifact_current(item):
            reason = "no checked current artifact"
        elif not requested and policy["muted"]:
            reason = "proactive attention muted"
        elif not requested and item.get("topic", item["scope"].lower()) in policy["muted_topics"]:
            reason = "topic muted"
        elif quiet:
            reason = "quiet window"
        elif not requested and offered:
            reason = "already prepared a return; do not repeat after silence"
        return {"eligible": reason is None, "reason": reason, "requested_current_reply": requested,
                "item": item if reason is None else {"id": ident, "title": item["title"]},
                "delivery_state": "not_sent", "work_stop_changed": False,
                "urgency": {"time_sensitive": urgent, "hours_left": hours_left, "policy_hours": policy["urgent_within_hours"],
                            "bypasses": "none"},
                "note": "Eligibility is not authorization or delivery. Use the native current reply; no sender exists here."}

    def queue_quiet_review(self, current, response, timezone_name=None):
        """Record one source-driven intent. Recording is not performed consolidation."""
        parent = current.get("parent_work") or {}
        sources = list(dict.fromkeys(current.get("observed_sources", [current["source_id"]])))[:12]
        substantive = len(current["message"]) >= 400 or len(str(response)) >= 800 or len(sources) >= 3
        if not substantive or not parent.get("id") or not parent.get("revision"):
            return None
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        prior = self.db.execute("SELECT * FROM quiet_reviews WHERE parent_id=? AND parent_revision=?", (parent["id"], parent["revision"])).fetchone()
        if prior:
            return dict(prior)
        day_start = datetime.now(ZoneInfo(timezone_name or zeus_settings.OWNER_TZ)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        if self.db.execute("SELECT count(*) FROM quiet_reviews WHERE created_at>=?", (day_start,)).fetchone()[0] >= 3:
            return None
        fingerprint = digest(json.dumps([parent["id"], parent["revision"], sorted(sources)], sort_keys=True))
        ident = "quiet_" + fingerprint[:20]
        self.db.execute("INSERT INTO quiet_reviews VALUES (?,?,?,?,?,?,?,?)",
                        (ident, parent["id"], parent["revision"], fingerprint, json.dumps(sources), "pending", now(), "{}"))
        self.event(None, "quiet_review_intent", {"id": ident, "parent_id": parent["id"], "source_ids": sources,
                                                "substantial_signal": True, "performed": False})
        self.db.commit()
        return dict(self.db.execute("SELECT * FROM quiet_reviews WHERE id=?", (ident,)).fetchone())

    def quiet_reviews(self, ident=None):
        rows = self.db.execute("SELECT * FROM quiet_reviews WHERE id=?", (ident,)) if ident else self.db.execute(
            "SELECT * FROM quiet_reviews ORDER BY created_at DESC LIMIT 12")
        return [{**dict(row), "source_ids": json.loads(row["source_ids"]), "result": json.loads(row["result"])} for row in rows]

    def quiet_update(self, ident, state, result):
        self.db.execute("UPDATE quiet_reviews SET state=?,result=? WHERE id=?", (state, json.dumps(result, sort_keys=True), ident))
        self.event(None, "quiet_review_" + state, {"id": ident, **result})
        self.db.commit()

    def learning_event(self, ident, action, work, source_id=None):
        item = self.get(ident)
        if item["kind"] not in {"lesson", "procedure"} or item["state"] != "adopted":
            raise ValueError("Use/outcome requires an adopted procedural lesson")
        work_id = work.get("id")
        if not work_id:
            raise ValueError("Need a native zeus_work status receipt")
        if action == "use":
            previous = [json.loads(row[0]) for row in self.db.execute(
                "SELECT detail FROM events WHERE insight_id=? AND kind='use'", (ident,))]
            if any(row["work_id"] == work_id and row.get("revision") == work.get("revision") for row in previous):
                return {"duplicate": True, "work_id": work_id}
            if work.get("state") in {"completed", "verified_complete", "stopped"}:
                raise ValueError("Record lesson application while work is active, not after seeing its result")
            if work_id not in item["work_ids"]:
                item["work_ids"].append(work_id)
            detail = {"work_id": work_id, "generation": work.get("generation"), "revision": work.get("revision"),
                      "meaning": "application recorded, not an improved outcome"}
        else:
            if work_id not in item["work_ids"]:
                raise ValueError("Outcome requires a prior use event on that work")
            if work.get("state") not in {"completed", "verified_complete"}:
                raise ValueError("Successful outcome requires verified completed native work")
            if work.get("completion_current") is False:
                raise ValueError("Native work reports stale completion evidence")
            source = self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone() if source_id else None
            if not source or source["kind"] != "native_tool" or source_id in item["source_ids"]:
                raise ValueError("Outcome also needs observed independent evidence source")
            uses = [(json.loads(row[0]), row[1]) for row in self.db.execute(
                "SELECT detail,created_at FROM events WHERE insight_id=? AND kind='use' ORDER BY created_at DESC", (ident,))]
            matched = next((row for row in uses if row[0]["work_id"] == work_id and row[0].get("revision") == work.get("revision")), None)
            if not matched or timestamp(source["observed_at"]) < timestamp(matched[1]):
                raise ValueError("Outcome evidence must follow lesson use on this exact work revision")
            evidence = work.get("evidence")
            if not isinstance(evidence, dict) or not evidence:
                raise ValueError("Completed work needs actual verified artifact evidence")
            for check in evidence.values():
                path = Path(check.get("path", ""))
                if not check.get("passed") or check.get("revision") != work.get("revision") or not path.is_file() or path.stat().st_size > 4_000_000 or digest(path.read_bytes()) != check.get("sha256"):
                    raise ValueError("Native completed-work evidence is missing, stale, or its artifact changed")
            detail = {"work_id": work_id, "generation": work.get("generation"), "revision": work.get("revision"),
                      "source_id": source_id, "work_state": work["state"],
                      "artifact_hashes": sorted(check["sha256"] for check in evidence.values()),
                      "meaning": "work completion plus evidence; causal improvement still requires comparison"}
        self.save(item)
        self.event(ident, action, detail)
        self.db.commit()
        return detail

    def promote(self, ident):
        item = self.get(ident)
        events = [json.loads(row[0]) for row in self.db.execute(
            "SELECT detail FROM events WHERE insight_id=? AND kind='outcome'", (ident,))]
        unique = {event["work_id"]: event for event in events}
        if item["kind"] != "lesson" or len(unique) < 2:
            raise ValueError("A reusable procedure requires two distinct completed work outcomes from an adopted lesson")
        item["kind"] = "procedure"
        item["promotion_evidence"] = list(unique.values())
        item["procedure_version"] = 1
        self.save(item)
        self.event(ident, "procedure_promoted", {"work_ids": sorted(unique), "version": 1})
        self.db.commit()
        return self.get(ident)

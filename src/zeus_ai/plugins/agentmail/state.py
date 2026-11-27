"""Durable receive and outbound-intent fences, isolated inside each profile."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


class ClosingConnection(sqlite3.Connection):
    """Keep transaction behavior and release each short-lived queue connection."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def admission_ready(status, profile=None):
    """Wait for active conversation work, not an indefinitely running background server.

    When the gateway reports work per profile, wait only for THIS profile's own chat turns and cron
    jobs, so one profile's long turn never holds another profile's mail. A gateway without
    ``by_profile`` falls back to the gateway-wide rule below.
    """
    if not isinstance(status, dict) or status.get("admission_version") != 2:
        return False
    by_profile = status.get("by_profile")
    if profile and isinstance(by_profile, dict):
        # Per-profile view wins, even when a slow gateway-wide probe left "idle" unknown.
        mine = by_profile.get(profile) or {}
        return all(type(mine.get(k, 0)) is int and mine.get(k, 0) == 0 for k in ("chat", "cron"))
    if status.get("idle") is None:
        return False
    counts = status.get("counts") or {}
    foreground = ("chat", "cron", "api", "api_workers", "delegation", "deferred", "tui", "mailbox_handoff")
    return all(type(counts.get(key)) is int and counts[key] == 0 for key in foreground)


OWNER_COMMANDS = ("/stop", "/new", "/reset")


def owner_command(message):
    """The gateway control a verified-owner email carries ("/stop", "/new", "/reset"), else None.

    /stop and /new work on every lane, email included: only mail owner_mail() marked
    ``_email_owner`` whose first non-empty body line (or, with no body, its subject) is exactly the
    command, so a signature below it still counts and prose mentioning it never does. Background mail
    never carries a command.
    """
    if not isinstance(message, dict) or message.get("_email_owner") is not True:
        return None
    body = str(message.get("extracted_text") or message.get("text") or "")
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    text = (lines[0] if lines else str(message.get("subject") or "").strip()).lower()
    return text if text in OWNER_COMMANDS else None


class Spool:
    def __init__(self, path, inbox):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.inbox = inbox
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                  id TEXT PRIMARY KEY,thread_id TEXT NOT NULL,source TEXT NOT NULL,
                  source_digest TEXT NOT NULL,state TEXT NOT NULL,received REAL NOT NULL,
                  dispatched REAL,completed REAL,outcome TEXT,error TEXT);
                CREATE TABLE IF NOT EXISTS replies(
                  incoming_id TEXT PRIMARY KEY,text TEXT NOT NULL,text_digest TEXT NOT NULL,
                  state TEXT NOT NULL,attempted REAL NOT NULL,receipt TEXT);
                CREATE TABLE IF NOT EXISTS health(
                  key TEXT PRIMARY KEY,value TEXT NOT NULL,updated REAL NOT NULL);
            """)
            for key, value in [("inbox", inbox), ("started_at", str(time.time()))]:
                db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, value))
            actual = db.execute("SELECT value FROM settings WHERE key='inbox'").fetchone()[0]
            if actual != inbox:
                raise ValueError("Saved email spool belongs to another inbox")
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @property
    def started_at(self):
        with self.connect() as db:
            return float(db.execute("SELECT value FROM settings WHERE key='started_at'").fetchone()[0])

    def admit(self, message):
        if message.get("inbox_id") != self.inbox:
            raise ValueError("Received email belongs to another inbox")
        immutable = {k: message.get(k) for k in (
            "message_id", "inbox_id", "thread_id", "from", "to", "reply_to",
            "subject", "text", "html", "in_reply_to", "references", "created_at",
            "attachments")}
        sha = digest(immutable)
        with self.connect() as db:
            old = db.execute("SELECT source_digest FROM messages WHERE id=?", (message["message_id"],)).fetchone()
            if old:
                if old[0] != sha:
                    raise ValueError("Immutable received email changed")
                return False
            db.execute("INSERT INTO messages(id,thread_id,source,source_digest,state,received) VALUES(?,?,?,?,?,?)",
                       (message["message_id"], message["thread_id"],
                        json.dumps(message, ensure_ascii=False), sha, "queued", time.time()))
        return True

    def get(self, incoming_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (incoming_id,)).fetchone()
            return dict(row) if row else None

    def next_queued(self):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM messages WHERE state='dispatching' LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT * FROM messages WHERE state='queued' ORDER BY received,id LIMIT 1").fetchone()
            return dict(row) if row else None

    def next_owner_mail(self):
        """The oldest queued verified-owner email. It never waits for the agent's own turn or another
        email in flight: the owner steers or stops a busy agent by email as on every other lane."""
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE state='queued' ORDER BY received,id LIMIT 200").fetchall()
        for row in rows:
            try:
                if json.loads(row["source"]).get("_email_owner") is True:
                    return dict(row)
            except (TypeError, ValueError):
                continue
        return None

    def reserve(self, incoming_id):
        with self.connect() as db:
            return db.execute("UPDATE messages SET state='dispatching',dispatched=? WHERE id=? AND state='queued'",
                              (time.time(), incoming_id)).rowcount == 1

    def finish(self, incoming_id, state, outcome, error=None):
        with self.connect() as db:
            db.execute("UPDATE messages SET state=?,outcome=?,error=?,completed=? WHERE id=?",
                       (state, outcome, error, time.time(), incoming_id))

    def reply(self, incoming_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM replies WHERE incoming_id=?", (incoming_id,)).fetchone()
            return dict(row) if row else None

    def reserve_reply(self, incoming_id, text):
        """Only the caller receiving True may POST. Existing/unknown intents never replay."""
        with self.connect() as db:
            old = db.execute("SELECT * FROM replies WHERE incoming_id=?", (incoming_id,)).fetchone()
            if old:
                if old["text_digest"] != digest(text):
                    raise ValueError("Email reply content changed after reservation")
                return False
            db.execute("INSERT INTO replies VALUES(?,?,?,?,?,NULL)",
                       (incoming_id, text, digest(text), "attempting", time.time()))
            return True

    def save_reply(self, incoming_id, state, receipt=None):
        with self.connect() as db:
            db.execute("UPDATE replies SET state=?,receipt=? WHERE incoming_id=?",
                       (state, json.dumps(receipt) if receipt else None, incoming_id))

    def health(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO health VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated",
                       (key, json.dumps(value), time.time()))

    def recover(self):
        """Never rerun a dispatch whose native/tool outcome is uncertain after a restart."""
        with self.connect() as db:
            db.execute("""UPDATE messages SET state='needs_review',error='interrupted_native_dispatch'
                          WHERE state='dispatching'""")
            db.execute("UPDATE replies SET state='outcome_unknown' WHERE state='attempting'")

    def summary(self):
        with self.connect() as db:
            return {row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM messages GROUP BY state")}

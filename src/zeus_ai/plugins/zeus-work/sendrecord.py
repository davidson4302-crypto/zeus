"""Zeus's own durable send record for timed returns. Plain Python: no Hermes import, no transport.

Public Hermes has no per-send journal a plugin can read back, so Zeus keeps its own, one row per operation id,
in the zeus-work data folder of the profile (``plugin-data/zeus-work/sends.sqlite3``):

  intent    written and committed (fsync'd, ``synchronous=FULL``) BEFORE the transport is called.
  sent      the platform confirmed the send; its message id(s) are kept.
  failed    the platform proved nothing reached the chat (Hermes's ``delivery_state`` == none). May be tried again.
  uncertain the send may have reached the chat without a receipt. Final: never sent again on its own.

An ``intent`` row whose sender is gone (crash, kill, timeout after the send started) reads as uncertain. The
sender holds an exclusive lock on a per-operation lock file for the whole send, and the operating system drops that
lock when the process dies, so "intent and nobody holds the lock" is exactly "the send started and its outcome is
unknown". "intent and the lock is held" is a send still in flight in another process.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import time

FINAL = frozenset({"sent", "uncertain"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS sends(operation_id TEXT PRIMARY KEY, status TEXT NOT NULL, platform TEXT NOT NULL,
  chat_id TEXT NOT NULL, sha256 TEXT NOT NULL, attempts INTEGER NOT NULL, intent_at REAL NOT NULL,
  finished_at REAL, message_id TEXT, message_ids TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS log(seq INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, at REAL NOT NULL,
  status TEXT NOT NULL, detail TEXT NOT NULL);
"""
_OP = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class SendRecord:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "sends.sqlite3"
        self.locks = self.root / "send-locks"
        self.locks.mkdir(exist_ok=True)
        for path in (self.path, Path(str(self.path) + "-journal")):
            if path.is_symlink():
                raise ValueError("The send record cannot be a symlink.")
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(SCHEMA)        # commits on its own
        finally:
            db.close()
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")       # every commit is on disk before the call returns
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextmanager
    def _lock(self, operation_id, wait):
        """The per-operation lock: yields True when this process holds it."""
        fd = os.open(self.locks / (operation_id + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @staticmethod
    def _row(db, operation_id):
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM sends WHERE operation_id=?", (operation_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _log(db, operation_id, status, detail=None):
        db.execute("INSERT INTO log(operation_id,at,status,detail) VALUES(?,?,?,?)",
                   (operation_id, time.time(), status, json.dumps(detail or {}, sort_keys=True)))

    @staticmethod
    def _view(row, state=None):
        if row is None:
            return {"state": "none"}
        return {"state": state or row["status"], "operation_id": row["operation_id"], "attempts": row["attempts"],
                "platform": row["platform"], "chat_id": row["chat_id"], "message_id": row["message_id"],
                "message_ids": json.loads(row["message_ids"] or "[]"), "error": row["error"],
                "intent_at": row["intent_at"], "finished_at": row["finished_at"]}

    def lookup(self, operation_id):
        """none, in_flight, sent, failed or uncertain. Never changes what was recorded except to settle a dead
        sender's intent as uncertain."""
        _check(operation_id)
        with self._lock(operation_id, wait=False) as mine:
            with self._db() as db:
                row = self._row(db, operation_id)
                if row is None or row["status"] != "intent":
                    return self._view(row)
                if not mine:
                    return self._view(row, "in_flight")
                self._settle_dead_intent(db, row)
                return self._view(row)

    def _settle_dead_intent(self, db, row):
        db.execute("UPDATE sends SET status='uncertain', finished_at=?, error=? WHERE operation_id=?",
                   (time.time(), "The send started but its result was never recorded.", row["operation_id"]))
        self._log(db, row["operation_id"], "uncertain", {"reason": "intent_without_result"})
        row.update(status="uncertain", error="The send started but its result was never recorded.")

    def send_once(self, operation_id, platform, chat_id, text, send):
        """Send ``text`` at most once for ``operation_id``. ``send()`` returns a Hermes send-result dict.

        A recorded sent or uncertain result is returned as it is, without calling ``send``. A failed one (nothing
        reached the chat) may be tried again. An exception from ``send`` is uncertain: it may have been written."""
        _check(operation_id)
        body = sha256(text.encode()).hexdigest()
        with self._lock(operation_id, wait=False) as mine:
            if not mine:
                with self._db() as db:
                    return self._view(self._row(db, operation_id), "in_flight")
            with self._db() as db:
                row = self._row(db, operation_id)
                if row and row["status"] == "intent":
                    self._settle_dead_intent(db, row)
                if row and row["status"] in FINAL:
                    return dict(self._view(row), recorded=True)
                if row and (row["platform"], row["chat_id"], row["sha256"]) != (platform, chat_id, body):
                    return dict(self._view(row), refused="This operation was recorded for another message or chat.")
                attempts = (row["attempts"] if row else 0) + 1
                db.execute("""INSERT INTO sends(operation_id,status,platform,chat_id,sha256,attempts,intent_at)
                              VALUES(?,?,?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET status='intent',
                              attempts=excluded.attempts, intent_at=excluded.intent_at, finished_at=NULL,
                              message_id=NULL, message_ids=NULL, error=NULL""",
                           (operation_id, "intent", platform, chat_id, body, attempts, time.time()))
                self._log(db, operation_id, "intent", {"attempt": attempts})
            # Committed. From here on a crash leaves "intent", which reads as uncertain.
            try:
                result = send()
            except Exception as exc:
                result = {"error": "Send raised: " + type(exc).__name__ + ": " + str(exc)[:160], "delivery": "uncertain"}
            status, ids, error = classify(result)
            with self._db() as db:
                db.execute("UPDATE sends SET status=?, finished_at=?, message_id=?, message_ids=?, error=? "
                           "WHERE operation_id=?",
                           (status, time.time(), ids[-1] if ids else None, json.dumps(ids), error, operation_id))
                self._log(db, operation_id, status, {"message_ids": ids, "error": error})
                return self._view(self._row(db, operation_id))


def classify(result):
    """(status, message ids, error) for a Hermes send result, with Hermes's own reading of what reached the chat
    (``gateway.platforms._shared.delivery_state``: only ``none`` proves nothing was sent)."""
    try:
        from gateway.platforms._shared import NONE, delivery_state, delivered_message_ids
        state = delivery_state(result)
        landed = delivered_message_ids(result)
    except Exception:
        # Without Hermes's reader, use the same envelope rule it documents.
        if not isinstance(result, dict) or not result.get("error"):
            state = "delivered"
        else:
            state = result.get("delivery") or ("none" if result.get("retryable") is True else "uncertain")
        NONE = "none"
        landed = [str(i) for i in (result.get("delivered_message_ids") if isinstance(result, dict) else None) or ()]
    if not isinstance(result, dict):
        return "uncertain", [], "The sender returned no result."
    error = str(result.get("error") or "")[:300] or None
    if state == "delivered" and result.get("success") is True:
        ids = landed or ([str(result["message_id"])] if result.get("message_id") is not None else [])
        return "sent", ids, None
    if state == NONE:
        return "failed", [], error or "Nothing was sent."
    return "uncertain", landed, error or "The sender's result did not confirm the send."


def _check(operation_id):
    if not isinstance(operation_id, str) or not _OP.match(operation_id):
        raise ValueError("A plain operation id is required.")

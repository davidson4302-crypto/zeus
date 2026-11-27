"""Durable iMessage approval requests. Plain code decides; no model reads or grants approvals.

A request is one exact question shown to the owner. They answer in plain words ("yes", "skip it", "skip 2",
"yes to all"), no codes needed. Each open request has a small number (stored in ``code``) used only when several
are open. Only the owner's verified handle, in the owner's own chat, on the agent's own line, can answer it, once. The owning flow re-checks its own binding (payload
hash, nonce, revision) before anything happens.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# No 0/O, 1/I/L, 2/Z, 5/S, 6/G, 8/B: nothing that reads two ways on a phone.
ALPHABET = "ACDEFHJKMNPQRTUVWXY3479"
CODE_LENGTH = 5
VERBS = ("YES", "NO", "EDIT", "NEXT", "READY", "RETRY", "SKIP")
OPEN = ("sending", "sent", "unknown", "deciding")
ANSWERABLE = ("sent", "unknown")
_REPLY = re.compile(r"^\s*([A-Za-z]+)[\s:,.\-]+([A-Za-z0-9]{%d})(?![A-Za-z0-9])(.*)$" % CODE_LENGTH, re.S)
_TRAILING_OK = re.compile(r"^[\s.!,]*$")

# Plain-word answers. A flow can pass its own ``words`` ({"delete it": "YES"}) so a casual "yes" meant for
# something else can never trigger it; otherwise these defaults apply to the request's choices.
WORDS = {
    # "ok"/"k" are left out on purpose: they usually mean "noted", not "approved".
    "YES": ("yes", "y", "yep", "yeah", "yup", "ya", "sure", "approve", "approved", "go ahead", "do it",
            "sounds good", "yes please", "looks good", "good to go"),
    "NO": ("no", "n", "nope", "nah", "dont", "don't", "no thanks", "not now", "cancel", "pass"),
    "SKIP": ("skip", "skip it", "skip that", "skip this", "skip this one", "skip that one", "let it go",
             "leave it", "ignore it", "ignore", "drop it", "forget it"),
    "RETRY": ("retry", "retry it", "try again", "try it again", "run it", "run it again", "run again", "rerun",
              "rerun it", "redo it", "redo"),
    "NEXT": ("next", "next one"),
    "READY": ("ready", "im ready", "i'm ready"),
}
_POLITE = {"please", "pls", "thanks", "thank", "you", "thx", "ty"}
_FILLER = {"to", "on", "for", "number", "num", "no.", "and", "&", "the", "of", "them", "#"}
_ALL = {"all", "both", "everything", "every"}
_EDIT = re.compile(r"^\s*(?:edit|change)\s*(?:#?(\d{1,2}))?\s*[:\-,]?\s+(.+)$", re.S | re.I)


def _norm(text):
    text = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    text = re.sub(r"#\s*(\d)", r" \1", text)
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return " ".join(text.split())


def parse_plain(text):
    """Plain-language answer, or None when this is ordinary conversation.

    Returns {"phrase", "numbers", "all", "extra"}. Only short messages count; the phrase must later match
    an open request's words exactly, otherwise the message goes to the agent untouched.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        return None
    edit = _EDIT.match(text)
    if edit and edit.group(2).strip():
        return {"phrase": "edit", "numbers": [edit.group(1)] if edit.group(1) else [], "all": False,
                "extra": edit.group(2).strip()}
    tokens = _norm(text).split()
    if not tokens or len(tokens) > 10:
        return None
    while tokens and tokens[-1] in _POLITE:
        tokens.pop()
    numbers = [t for t in tokens if t.isdigit() and len(t) <= 2]
    every = any(t in _ALL for t in tokens)
    rest = [t for t in tokens if not (t.isdigit() and len(t) <= 2) and t not in _ALL]
    if numbers or every:
        rest = [t for t in rest if t not in _FILLER]
    phrase = " ".join(rest)
    if not phrase:
        return None
    return {"phrase": phrase, "numbers": list(dict.fromkeys(numbers)), "all": every and not numbers, "extra": ""}


def words_for(request):
    """{phrase: VERB} accepted for this request."""
    custom = request.get("words")
    if isinstance(custom, str):
        try:
            custom = json.loads(custom)
        except ValueError:
            custom = None
    choices = request.get("choices") or []
    if isinstance(choices, str):
        choices = json.loads(choices)
    if custom:
        return {_norm(k): str(v).upper() for k, v in custom.items() if str(v).upper() in choices}
    out = {}
    for verb in choices:
        for word in WORDS.get(verb, ()):
            out[_norm(word)] = verb
    return out


def label_for(request):
    if request.get("label"):
        return str(request["label"])
    first = (request.get("text") or "").strip().splitlines()[0:1]
    text = first[0] if first else request.get("flow", "request")
    return text if len(text) <= 70 else text[:69].rstrip() + "\u2026"


def sha256(value):
    if not isinstance(value, (bytes, str)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def new_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def parse_reply(text):
    """Return None when the text is ordinary conversation, else a parsed approval attempt.

    ``ok`` is False for a recognised verb with a malformed code or unexpected extra words. Those are
    still consumed (never forwarded to a model as permission) and answered with "nothing was done".
    """
    if not isinstance(text, str):
        return None
    match = _REPLY.match(text)
    if not match:
        return None
    verb = match.group(1).upper()
    if verb not in VERBS:
        return None
    code = match.group(2).upper()
    extra = match.group(3) or ""
    ok = all(ch in ALPHABET for ch in code)
    if verb == "EDIT":
        extra = extra.strip(" \t\r\n:-,")
        ok = ok and bool(extra)
    else:
        ok = ok and bool(_TRAILING_OK.match(extra))
        extra = ""
    return {"verb": verb, "code": code, "extra": extra, "ok": ok}


class ApprovalError(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ApprovalError("An absolute approval database path is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS requests(
              id TEXT PRIMARY KEY, batch TEXT NOT NULL, code TEXT NOT NULL, flow TEXT NOT NULL,
              subject TEXT NOT NULL, binding TEXT NOT NULL, binding_sha256 TEXT NOT NULL,
              choices TEXT NOT NULL, text TEXT NOT NULL, text_sha256 TEXT NOT NULL,
              status TEXT NOT NULL, handle TEXT NOT NULL, chat TEXT NOT NULL, route TEXT NOT NULL,
              message_id TEXT, send_error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
              decision TEXT, decided_at REAL, result TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS requests_open_code ON requests(code)
              WHERE status IN ('sending','sent','unknown','deciding');
            CREATE INDEX IF NOT EXISTS requests_flow_subject ON requests(flow, subject, status);
            CREATE TABLE IF NOT EXISTS inbound(
              message_id TEXT PRIMARY KEY, handle TEXT NOT NULL, chat TEXT NOT NULL,
              text_sha256 TEXT NOT NULL, request_id TEXT, outcome TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
            """)
            have = {r[1] for r in db.execute("PRAGMA table_info(requests)")}
            for column in ("words", "label"):
                if column not in have:
                    db.execute("ALTER TABLE requests ADD COLUMN " + column + " TEXT")

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _row(row):
        if row is None:
            return None
        value = dict(row)
        for key in ("binding", "choices", "decision", "words"):
            if value.get(key):
                value[key] = json.loads(value[key])
        return value

    @staticmethod
    def _audit(db, kind, payload):
        db.execute("INSERT INTO audit(kind,payload,created) VALUES (?,?,?)",
                   (kind, json.dumps(payload, sort_keys=True, default=str), time.time()))

    def create_batch(self, items, render, *, handle, chat, route):
        """Durable claim before any network call. ``items``: [{flow, subject, binding, choices, words?, label?}].

        ``render(numbers)`` returns the one exact message text. ``numbers`` are short strings ("1", "2"), the
        lowest not open or used in the last day, so "skip 2" is unambiguous. Show them only when several things are asked.
        """
        if not items:
            raise ApprovalError("Nothing to ask")
        for item in items:
            choices = [str(c).upper() for c in item["choices"]]
            if not choices or any(c not in VERBS for c in choices):
                raise ApprovalError("Unsupported reply choice")
        now = time.time()
        batch = uuid.uuid4().hex
        with self.tx() as db:
            # Open numbers, plus any used in the last day, so a late answer to an old text can't land on a new one.
            used = {r[0] for r in db.execute("SELECT code FROM requests WHERE status IN ('sending','sent','unknown','deciding') "
                                             "OR updated>?", (now - 86400,))}
            codes = []
            number = 0
            for _ in items:
                number += 1
                while str(number) in used:
                    number += 1
                codes.append(str(number))
            text = render(codes)
            if not isinstance(text, str) or not text.strip():
                raise ApprovalError("Approval text is empty")
            rows = []
            for item, code in zip(items, codes):
                rid = uuid.uuid4().hex
                binding = item["binding"]
                db.execute("INSERT INTO requests(id,batch,code,flow,subject,binding,binding_sha256,choices,text,"
                           "text_sha256,status,handle,chat,route,created,updated,words,label) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (rid, batch, code, item["flow"], str(item["subject"]),
                            json.dumps(binding, sort_keys=True), sha256(binding),
                            json.dumps([str(c).upper() for c in item["choices"]]), text, sha256(text),
                            "sending", handle, chat, route, now, now,
                            json.dumps(item["words"], sort_keys=True) if item.get("words") else None,
                            str(item["label"])[:120] if item.get("label") else None))
                rows.append(rid)
            self._audit(db, "claimed", {"batch": batch, "requests": rows, "text_sha256": sha256(text)})
        return {"batch": batch, "ids": rows, "codes": codes, "text": text}

    def settle_batch(self, batch, status, *, message_id=None, error=None):
        if status not in ("sent", "unknown", "failed"):
            raise ApprovalError("Unknown send outcome")
        with self.tx() as db:
            changed = db.execute("UPDATE requests SET status=?,message_id=?,send_error=?,updated=? "
                                 "WHERE batch=? AND status='sending'",
                                 (status, message_id, (str(error)[:400] if error else None), time.time(), batch)).rowcount
            self._audit(db, "send_" + status, {"batch": batch, "message_id": message_id, "error": str(error or "")[:200]})
            return changed

    def recover_interrupted(self, older_than=180):
        """A crash between claim and receipt leaves 'sending': treat it as unknown, never resend.
        A crash inside a flow decision leaves 'deciding': close it. The flow's own state is the truth."""
        cutoff = time.time() - older_than
        with self.tx() as db:
            changed = db.execute("UPDATE requests SET status='unknown',send_error=?,updated=? "
                                 "WHERE status='sending' AND updated<?",
                                 ("Send outcome was interrupted; not resent.", time.time(), cutoff)).rowcount
            stuck = db.execute("UPDATE requests SET status='closed',result=?,updated=? "
                               "WHERE status='deciding' AND updated<?",
                               ("Decision was interrupted. Check the flow's saved state.", time.time(), time.time() - 600)).rowcount
            if changed or stuck:
                self._audit(db, "recovered", {"unknown": changed, "closed": stuck})
            return changed + stuck

    def get(self, rid):
        with self.tx() as db:
            return self._row(db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())

    def batch(self, batch):
        with self.tx() as db:
            return [self._row(r) for r in db.execute("SELECT * FROM requests WHERE batch=? ORDER BY rowid", (batch,))]

    def open_requests(self, flow=None):
        with self.tx() as db:
            if flow:
                rows = db.execute("SELECT * FROM requests WHERE flow=? AND status IN ('sending','sent','unknown','deciding') "
                                  "ORDER BY created", (flow,))
            else:
                rows = db.execute("SELECT * FROM requests WHERE status IN ('sending','sent','unknown','deciding') ORDER BY created")
            return [self._row(r) for r in rows]

    def for_binding(self, flow, subject, binding):
        """Every request ever made for this exact flow binding (payload hash, nonce, state...)."""
        with self.tx() as db:
            return [self._row(r) for r in db.execute(
                "SELECT * FROM requests WHERE flow=? AND subject=? AND binding_sha256=? ORDER BY created",
                (flow, str(subject), sha256(binding)))]

    def latest_for(self, flow, subject):
        with self.tx() as db:
            return self._row(db.execute("SELECT * FROM requests WHERE flow=? AND subject=? ORDER BY created DESC, rowid DESC LIMIT 1",
                                        (flow, str(subject))).fetchone())

    def close(self, rid, reason):
        with self.tx() as db:
            changed = db.execute("UPDATE requests SET status='closed',result=?,updated=? "
                                 "WHERE id=? AND status IN ('sending','sent','unknown','failed')",
                                 (str(reason)[:400], time.time(), rid)).rowcount
            if changed:
                self._audit(db, "closed", {"id": rid, "reason": str(reason)[:200]})
            return bool(changed)

    def record_inbound(self, message_id, handle, chat, text):
        """False when this exact inbound message was already handled (provider redelivery)."""
        with self.tx() as db:
            try:
                db.execute("INSERT INTO inbound VALUES (?,?,?,?,NULL,'received',?)",
                           (str(message_id), handle, chat, sha256(text or ""), time.time()))
                return True
            except sqlite3.IntegrityError:
                return False

    def seen_inbound(self, message_id):
        with self.tx() as db:
            return db.execute("SELECT 1 FROM inbound WHERE message_id=?", (str(message_id),)).fetchone() is not None

    def recent_handled(self, handle, chat, within=6 * 3600):
        """Requests answered or closed lately, newest first, so a late "skip" hears "already handled"."""
        with self.tx() as db:
            return [self._row(r) for r in db.execute(
                "SELECT * FROM requests WHERE handle=? AND chat=? AND status IN ('decided','closed') AND updated>? "
                "ORDER BY updated DESC", (handle, chat, time.time() - within))]

    def inbound_outcome(self, message_id, outcome, request_id=None):
        with self.tx() as db:
            db.execute("UPDATE inbound SET outcome=?,request_id=? WHERE message_id=?",
                       (str(outcome)[:400], request_id, str(message_id)))

    def claim(self, code, verb, *, handle, chat, inbound_id, extra=""):
        """Atomically consume one open request. Returns (request, None) or (None, plain reason)."""
        code, verb = str(code).upper(), str(verb).upper()
        with self.tx() as db:
            row = db.execute("SELECT * FROM requests WHERE code=? AND status IN ('sending','sent','unknown','deciding')",
                             (code,)).fetchone()
            if row is None:
                prior = db.execute("SELECT * FROM requests WHERE code=? ORDER BY updated DESC LIMIT 1", (code,)).fetchone()
                if prior is not None and prior["status"] in ("decided", "closed"):
                    return None, "That one was already handled" + (": " + prior["result"] if prior["result"] else ".")
                return None, "I don't have anything open for that."
            if row["handle"] != handle or row["chat"] != chat:
                return None, "That one belongs to a different conversation."
            if row["status"] == "deciding":
                return None, "I'm already working on that one."
            if row["status"] not in ANSWERABLE:
                return None, "That request is still being sent. Try again in a minute."
            choices = json.loads(row["choices"])
            if verb not in choices:
                words = words_for(self._row(row))
                options = [next(w for w, v in words.items() if v == c) for c in choices if c in words.values()]
                return None, "For that one, say " + " or ".join('"' + w + '"' for w in options) + "."
            decision = {"verb": verb, "extra": extra, "handle": handle, "chat": chat,
                        "inbound_message_id": str(inbound_id), "at": time.time(),
                        "binding_sha256": row["binding_sha256"], "text_sha256": row["text_sha256"]}
            decision["previous_status"] = row["status"]
            changed = db.execute("UPDATE requests SET status='deciding',decision=?,decided_at=?,updated=? "
                                 "WHERE id=? AND status IN ('sent','unknown')",
                                 (json.dumps(decision, sort_keys=True), decision["at"], decision["at"], row["id"])).rowcount
            if not changed:
                return None, "That one was already handled."
            self._audit(db, "deciding", {"id": row["id"], "code": code, "verb": verb, "inbound": str(inbound_id)})
            value = self._row(row)
            value["decision"] = decision
            value["status"] = "deciding"
            return value, None

    def finish(self, rid, result, *, final=True):
        """Final: the code is spent. Not final (the flow changed nothing): reopen the code."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if row is None or row["status"] != "deciding":
                return False
            if final:
                db.execute("UPDATE requests SET status='decided',result=?,updated=? WHERE id=?",
                           (str(result)[:400], time.time(), rid))
            else:
                previous = (json.loads(row["decision"] or "{}").get("previous_status") or "sent")
                db.execute("UPDATE requests SET status=?,result=?,updated=? WHERE id=?",
                           (previous if previous in ANSWERABLE else "sent", str(result)[:400], time.time(), rid))
            self._audit(db, "finished" if final else "reopened", {"id": rid, "result": str(result)[:200]})
            return True

    def proven(self):
        """True once a real coded reply from the verified handle has been processed on this line."""
        with self.tx() as db:
            for row in db.execute("SELECT decision FROM requests WHERE status='decided' AND decision IS NOT NULL"):
                decision = json.loads(row[0])
                if decision.get("inbound_message_id") and decision.get("handle"):
                    return True
            return False

    def summary(self):
        with self.tx() as db:
            states = dict(db.execute("SELECT status, count(*) FROM requests GROUP BY status").fetchall())
            last = db.execute("SELECT kind, created FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            return {"requests": states, "last_audit": dict(last) if last else None}

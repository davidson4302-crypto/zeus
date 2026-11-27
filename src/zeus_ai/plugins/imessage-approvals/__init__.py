"""iMessage approvals: ask the owner over the agent's own iMessage line; they answer in plain words.

The owner replies "yes", "skip it", "skip 2", "yes to all" (no codes needed). Routing is plain code, never a
model. A reply is acted on only when it is a short message whose words match an open request, from the
configured owner handle, in the owner's private chat, on this profile's own receiving line, once. A word that matches nothing open is ordinary chat for the agent.
Coded ``VERB CODE`` replies are also accepted. Everything else falls through to the agent untouched. Flows (retry review, inbox desk) keep
their own state and re-check their own binding before anything happens.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import sys
import types
from pathlib import Path

from . import core, transport

log = logging.getLogger(__name__)
REGISTRY = "hermes_imessage_approvals_registry"


def registry():
    """Process-wide hub and flow registry, shared by the plugins of every profile in this gateway."""
    module = sys.modules.get(REGISTRY)
    if module is None:
        module = types.ModuleType(REGISTRY)
        module.HUBS = {}
        module.FLOWS = {}
        sys.modules[REGISTRY] = module
    return module


def hub_for(profile):
    return registry().HUBS.get(profile)


def register_flow(profile, name, handler):
    registry().FLOWS[(profile, name)] = handler


def _platform(source):
    value = getattr(source, "platform", None)
    return getattr(value, "value", value)


class Hub:
    def __init__(self, profile, settings, store):
        self.profile = profile
        self.handle = settings["handle"]
        self.chats = set(settings["chats"])
        self.transport_profile = settings["transport_profile"]
        self.route = settings["route"]
        self.target = settings["target"]
        self.store = store
        self.tasks = set()
        self.closed = False

    # ---- outbound -------------------------------------------------------------------------
    async def ask(self, items, render):
        """Claim, send once, settle. Returns the saved request rows (one per item)."""
        claim = await asyncio.to_thread(self.store.create_batch, items, render,
                                        handle=self.handle, chat=self.home_chat(), route=self.route)
        outcome = await transport.send(self.route, self.profile, self.target, claim["text"])
        status = outcome["kind"]
        await asyncio.to_thread(self.store.settle_batch, claim["batch"], status,
                                message_id=outcome.get("message_id"), error=outcome.get("error"))
        if status != "sent":
            log.warning("imessage-approvals: request %s send %s; not resent", claim["batch"][:8], status)
        return await asyncio.to_thread(self.store.batch, claim["batch"])

    async def notify(self, text):
        """One plain status text (no code, approves nothing). Sent once; never resent."""
        outcome = await transport.send(self.route, self.profile, self.target, text)
        def note():
            with self.store.tx() as db:
                self.store._audit(db, "notice_" + outcome["kind"], {"text_sha256": core.sha256(text),
                                  "message_id": outcome.get("message_id"), "error": str(outcome.get("error") or "")[:200]})
        await asyncio.to_thread(note)
        return outcome

    def home_chat(self):
        # The canonical inbound chat ID (any;-;+1...) when configured, else the first.
        for chat in sorted(self.chats):
            if chat.startswith("any;-;"):
                return chat
        return sorted(self.chats)[0]

    # ---- inbound --------------------------------------------------------------------------
    def _verified(self, event, gateway):
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False) is not False:
            return None
        if _platform(source) != "photon":
            return None
        if str(getattr(source, "user_id", "") or "") != self.handle:
            return None
        if str(getattr(source, "chat_id", "") or "") not in self.chats:
            return None
        if str(getattr(source, "chat_type", "dm") or "dm") != "dm":
            return None
        try:
            from gateway.session_identity import identity_of
            identity = identity_of(source)
        except Exception:
            identity = None
        if identity is None:
            log.warning("imessage-approvals: reply without gateway identity ignored")
            return None
        if identity.runtime_profile != self.profile or identity.transport_profile != self.transport_profile:
            log.warning("imessage-approvals: reply on wrong line/profile ignored (runtime=%s transport=%s)",
                        identity.runtime_profile, identity.transport_profile)
            return None
        # Live provenance only: the receiving adapter must be known, never inferred or restored.
        if getattr(source, "is_bot", True) is not False or getattr(identity, "transport_inferred", True):
            return None
        try:
            if identity.adapter() is None:
                return None
        except Exception:
            return None
        # Zeus's one owner test must also pass: the owner's own direct chat on the owner platform.
        if not _zeus_hermes().is_owner_event(event):
            log.warning("imessage-approvals: owner check refused a coded reply")
            return None
        return source

    async def admit(self, event=None, gateway=None, **kwargs):
        if self.closed or event is None:
            return None
        text = getattr(event, "text", None)
        legacy = core.parse_reply(text)
        plain = core.parse_plain(text)
        if legacy is None and plain is None:
            return None
        source = self._verified(event, gateway)
        if source is None:
            return None
        chat = str(source.chat_id)
        home = self.home_chat()
        inbound_id = str(getattr(event, "message_id", None) or getattr(source, "message_id", None) or "")
        if inbound_id and await asyncio.to_thread(self.store.seen_inbound, inbound_id):
            return self._consumed(event)  # provider redelivery of a reply already handled
        open_rows = [r for r in await asyncio.to_thread(self.store.open_requests)
                     if r["handle"] == self.handle and r["chat"] == home]
        if legacy is not None and legacy["ok"] and any(r["code"] == legacy["code"] for r in open_rows):
            picks = [(next(r for r in open_rows if r["code"] == legacy["code"]), legacy["verb"], legacy["extra"])]
        elif plain is not None:
            picks, question = self._match(plain, open_rows)
            if picks is None and question is None:
                late = [r for r in await asyncio.to_thread(self.store.recent_handled, self.handle, home)
                        if core.words_for(r).get(plain["phrase"])
                        and (not plain["numbers"] or r["code"] in plain["numbers"])]
                if not late:
                    return None  # ordinary conversation: nothing open uses these words
                question = "That one was already handled" + (": " + late[0]["result"] if late[0].get("result") else ".")
        else:
            return None
        if not inbound_id:
            self._reply(gateway, source, "I couldn't verify that message, so nothing was done. Please send it again.")
            return self._consumed(event)
        if not await asyncio.to_thread(self.store.record_inbound, inbound_id, self.handle, chat, text):
            return self._consumed(event)  # provider redelivery of a reply already handled
        if picks is None:
            await asyncio.to_thread(self.store.inbound_outcome, inbound_id, "asked which one")
            self._reply(gateway, source, question)
            return self._consumed(event)
        task = asyncio.ensure_future(self._run(gateway, source, picks, inbound_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self._consumed(event)

    @staticmethod
    def _match(plain, open_rows):
        """(picks, None) to act, (None, question) to ask which one, (None, None) when nothing matches."""
        answerable = [r for r in open_rows if r["status"] in core.ANSWERABLE]
        matches = []
        for r in answerable:
            verb = "EDIT" if plain["phrase"] == "edit" and "EDIT" in (r.get("choices") or []) else \
                core.words_for(r).get(plain["phrase"])
            if verb:
                matches.append((r, verb, plain["extra"]))
        if not matches:
            return None, None
        if plain["numbers"]:
            picks = [m for m in matches if m[0]["code"] in plain["numbers"]]
            missing = [n for n in plain["numbers"] if n not in {m[0]["code"] for m in picks}]
            if missing or not picks:
                return None, ("I don't have number " + ", ".join(missing or plain["numbers"]) + " open for that. "
                              + Hub._which(matches, plain["phrase"]))
            return picks, None
        if plain["all"] or len(matches) == 1:
            return matches, None
        return None, "Which one? " + Hub._which(matches, plain["phrase"])

    @staticmethod
    def _which(matches, phrase):
        lines = [m[0]["code"] + ") " + core.label_for(m[0]) for m in matches[:8]]
        example = (phrase if phrase != "edit" else "edit") + " " + matches[0][0]["code"]
        return "\n" + "\n".join(lines) + '\nSay something like "' + example + '" or "' + phrase + ' all".'

    async def _run(self, gateway, source, picks, inbound_id):
        texts = []
        for row, verb, extra in picks:
            if (self.profile, row["flow"]) not in registry().FLOWS:
                await asyncio.to_thread(self.store.inbound_outcome, inbound_id, "flow_unavailable", row["id"])
                texts.append("I can't process that one right now, so nothing was done. Try again in a few minutes.")
                continue
            request, reason = await asyncio.to_thread(self.store.claim, row["code"], verb, handle=self.handle,
                                                      chat=self.home_chat(), inbound_id=inbound_id, extra=extra)
            if request is None:
                await asyncio.to_thread(self.store.inbound_outcome, inbound_id, "refused: " + reason)
                texts.append(reason + (" Nothing was done." if "already handled" not in reason else ""))
                continue
            await asyncio.to_thread(self.store.inbound_outcome, inbound_id, "claimed", request["id"])
            texts.append(await self._decide(request, verb, extra, inbound_id))
        if len(texts) > 1:
            texts = [str(i) + ") " + t for i, t in enumerate(texts, 1)]
        self._reply(gateway, source, "\n".join(texts) or "Nothing was done.")

    @staticmethod
    def _consumed(event):
        try:
            event.metadata["suppress_completion_reaction"] = True
        except Exception:
            pass
        return {"action": "skip", "reason": "imessage_approval_reply"}

    def provenance(self, request, inbound_id):
        return {"platform": "photon", "actor_id": self.handle, "chat_id": request["chat"],
                "team_id": self.handle, "message_id": "imsg:" + request["id"],
                "photon_message_id": request.get("message_id") or "",
                "interaction_id": "imessage:" + str(inbound_id), "code": request["code"],
                "text_sha256": request["text_sha256"], "binding_sha256": request["binding_sha256"],
                "transport_profile": self.transport_profile, "runtime_profile": self.profile}

    async def _decide(self, request, verb, extra, inbound_id):
        handler = registry().FLOWS.get((self.profile, request["flow"]))
        text, final = "That approval can't be processed right now. Nothing was done.", False
        try:
            if handler is None:
                raise core.ApprovalError("flow unavailable")
            result = handler(request, verb, extra, self.provenance(request, inbound_id))
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                text, final = str(result.get("text") or "Done."), bool(result.get("final", True))
            else:
                text, final = str(result or "Done."), True
        except Exception as error:
            log.exception("imessage-approvals: %s decision failed", request["flow"])
            text, final = "Not done: " + (str(error)[:200] or type(error).__name__), True
        await asyncio.to_thread(self.store.finish, request["id"], text, final=final)
        return text

    def _reply(self, gateway, source, text):
        async def send():
            adapter = None
            try:
                adapter = gateway._intake_adapter_for(source) if gateway is not None else None
            except Exception:
                adapter = None
            try:
                if adapter is not None:
                    await adapter.send(source.chat_id, text)
                else:
                    await transport.send(self.route, self.profile, self.target, text, reply=True)
            except Exception:
                log.warning("imessage-approvals: confirmation reply could not be sent; decision is saved")
        task = asyncio.ensure_future(send())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ---- built-in no-op flow for live proof ---------------------------------------------
    @staticmethod
    def dryrun(request, verb, extra, provenance):
        label = (request.get("binding") or {}).get("label") or "test"
        if verb == "YES":
            return {"text": "Got it: YES on the " + label + ". It was a dry run, so nothing else happens.", "final": True}
        return {"text": "Got it: NO on the " + label + ". Nothing happens.", "final": True}


def _settings(ctx):
    raw = {key: ctx.get_config(key, default) for key, default in
           (("enabled", False), ("handle", transport.DEFAULT_HANDLE), ("chats", []), ("transport_profile", ""),
            ("route", ""), ("target", ""), ("db_path", ""))}
    if raw["enabled"] is not True:
        return None
    handle = str(raw["handle"] or "")
    if not re.fullmatch(r"\+\d{8,15}", handle):
        raise ValueError("imessage-approvals needs the owner's exact E.164 handle")
    if transport.DEFAULT_HANDLE and handle != transport.DEFAULT_HANDLE:
        raise ValueError("imessage-approvals accepts only the owner's handle (owner.phone)")
    chats = [str(c) for c in (raw["chats"] or []) if str(c).strip()]
    if not chats or any(handle not in c for c in chats):
        raise ValueError("imessage-approvals needs the exact private chat IDs for that handle")
    if raw["route"] not in transport.ROUTES or not raw["transport_profile"]:
        raise ValueError("imessage-approvals needs a verified route and receiving line")
    if str(raw["target"]) != handle:
        raise ValueError("imessage-approvals sends only to the verified handle")
    db = raw["db_path"] or str(transport.ROOT / "plugin-data" / "imessage-approvals" / (ctx.profile_name + ".sqlite3"))
    if not Path(db).is_absolute():
        raise ValueError("imessage-approvals needs an absolute database path")
    return {"handle": handle, "chats": chats, "transport_profile": str(raw["transport_profile"]),
            "route": raw["route"], "target": handle, "db_path": db}


def register(ctx):
    settings = _settings(ctx)
    if settings is None:
        return
    profile = ctx.profile_name
    store = core.Store(settings["db_path"])
    store.recover_interrupted()
    hub = Hub(profile, settings, store)
    registry().HUBS[profile] = hub
    register_flow(profile, "dryrun", Hub.dryrun)
    # pre_gateway_dispatch: a coded or plain approval reply is handled here and skipped from the agent turn.
    ctx.register_hook("pre_gateway_dispatch", hub.admit)

    def unload():
        hub.closed = True
        if registry().HUBS.get(profile) is hub:
            registry().HUBS.pop(profile, None)
    ctx.on_unload(unload)
    log.info("imessage-approvals ready for %s (%s line, handle ...%s)", profile, settings["transport_profile"],
             settings["handle"][-4:])


def _zeus_hermes():
    """Zeus's one owner test (zeus_ai/engine/zeus_hermes.py)."""
    engine = str(Path(__file__).resolve().parents[2] / "engine")
    if engine not in sys.path:
        sys.path.insert(0, engine)
    import zeus_hermes
    return zeus_hermes

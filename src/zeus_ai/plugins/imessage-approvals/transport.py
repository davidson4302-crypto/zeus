"""Send one text over the agent's own existing, verified iMessage route.

Uses the standard ``hermes send`` command. Never reads secrets,
never retries: a missing receipt is 'unknown', and unknown is never resent.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))
import settings  # noqa: E402

HERMES_BIN = settings.HERMES_BIN
ROOT = Path(settings.HERMES_HOME)
ROUTES = ("own", "shared-default")
_TARGET = re.compile(r"^\+\d{8,15}$")
DEFAULT_HANDLE = settings.OWNER_PHONE


def _quiet_hold(reply=False):
    """Quiet hours (settings.QUIET_START to QUIET_END on the owner's clock): nothing reaches the owner, except a
    direct answer to a message they sent within the last hour. Uses the engine's own gate (ownertime.contact_hold),
    so this line follows the same rule as every other message Zeus sends. Returns a 'failed' outcome (nothing went
    out, so the caller may ask again after the window) or None to send now. If the gate cannot load, send as before;
    if it loads but cannot tell the time, hold."""
    try:
        import ownertime
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning("imessage-approvals: quiet-hours gate unavailable (%s); sending now", type(error).__name__)
        return None
    try:
        until = ownertime.contact_hold(reply=reply, life=settings.LIFE_HOME)
    except Exception:
        until = ownertime.UNKNOWN
    if until is None:
        return None
    when = "later" if until == ownertime.UNKNOWN else "after quiet hours"
    return {"kind": "failed", "error": "held for quiet hours; nothing was sent, ask again " + when}


def command(route, profile, target):
    """Return (argv, env) for one send. ``own``: the profile's own Photon adapter.
    ``shared-default``: a shared adapter pinned to this profile by gateway.profile_routes."""
    if route not in ROUTES:
        raise ValueError("Unknown iMessage route")
    if not _TARGET.match(str(target or "")):
        raise ValueError("iMessage target must be an E.164 handle")
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "USER", "LOGNAME") if k in os.environ}
    env.setdefault("HOME", str(settings.HOME))
    env["PATH"] = str(Path(HERMES_BIN).parent) + ":" + env.get("PATH", "/usr/bin:/bin")
    if route == "own":
        env["HERMES_HOME"] = str(ROOT / "profiles" / profile)
        env["HERMES_PROFILE"] = profile
        sender = profile
    else:
        env["HERMES_HOME"] = str(ROOT)
        sender = "default"
    argv = [HERMES_BIN, "--profile", sender, "send", "--to", "photon:" + target, "--json", "--file", "-"]
    return argv, env


def classify(returncode, stdout, stderr, timed_out=False):
    if timed_out:
        return {"kind": "unknown", "error": "send timed out"}
    value = None
    try:
        value = json.loads(stdout) if (stdout or "").strip() else None
    except ValueError:
        value = None
    message_id = ""
    error = ""
    if isinstance(value, dict):
        if isinstance(value.get("message_id"), str) and value["message_id"].strip() and value.get("success") is not False:
            message_id = value["message_id"].strip()
        if value.get("error"):
            error = " ".join(str(value["error"]).split())[:300]
    if not error:
        error = " ".join(str(stderr or "").split())[:300]
    if message_id:
        return {"kind": "sent", "message_id": message_id}
    if returncode in (1, 2) and error:
        return {"kind": "failed", "error": error}
    return {"kind": "unknown", "error": error or "no receipt"}


async def send(route, profile, target, text, *, timeout=90, reply=False):
    argv, env = command(route, profile, target)
    held = await asyncio.to_thread(_quiet_hold, reply)
    if held:
        return held
    try:
        process = await asyncio.create_subprocess_exec(*argv, env=env, cwd=str(ROOT),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as error:  # Never started: nothing went out.
        return {"kind": "failed", "error": "could not start sender: " + type(error).__name__}
    try:
        out, err = await asyncio.wait_for(process.communicate(text.encode("utf-8")), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return classify(None, "", "", timed_out=True)
    return classify(process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))

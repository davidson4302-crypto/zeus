---
name: background-watcher-autostart
description: "Use when a background script must wake me or auto-restart."
version: 1.0.0
metadata:
  hermes:
    tags: [Watcher, Gateway, Hooks, Autostart]
    related_skills: [agent-owned-mail, scheduled-checks-and-followups]
---

# Background watcher: wake the agent and survive restarts

For a looping Python checker (for example an agent-owned inbox watcher) that the user wants to (a) wake the agent and text them on arrival, and (b) come back after a reboot. Prefer the user's stated mechanism: a Python script, not a cron or launchd job they would have to undo.

## Always-on

- A loop that only writes to a log wakes nobody. Say so plainly when asked "will it wake you?"
- The gateway may serve several Hermes profiles on this machine. Any change to its config, routes or a restart takes all of them offline for about a minute. Name that downside and get the user's yes first.
- Never repeat an external send because a wake or ack looked missing. Recheck the destination first.
- Do not tell the user a restart path works until it has fired for real. Say what was tested (hook loads, handler start/skip) and what was not (live gateway restart).

## A. Wake and notify on arrival

1. Preferred: on a new item, the loop runs one one-shot agent turn to write the heads-up, then sends it with `hermes send --to <platform>` using the shared `HERMES_HOME`. No cron, no gateway edit.
2. Alternative: a gateway webhook route. It needs a shared-config edit plus gateway restart. A profile that owns its own Slack/Discord bot fails closed when the route tries to deliver on another profile's channel, so use `hermes send` from the script in that case.
3. Give the checker a self-test flag (e.g. `--test-ring`) that exercises arrival -> wake -> text. Only the user's phone confirms the final hop, so ask them.

## B. Auto-restart through a gateway startup hook

The gateway is kept alive by launchd and starts at login, so hang the watcher off it.

1. Create `~/.hermes/hooks/<name>/HOOK.yaml` with `events: [gateway:startup]` and a `description`.
2. Create `handler.py` with `async def handle(event_type, context)`.
3. In the handler: read the watcher's pid file and `os.kill(pid, 0)`. If alive, do nothing. If dead, `subprocess.Popen([system_python, "-I", script, "--loop"], cwd=profile_dir, start_new_session=True, stdin/stdout/stderr=DEVNULL)` with `HERMES_*` and `PYTHON*` env removed. Use a system python so the watcher survives Hermes updates.
4. Wrap the whole handler in try/except and log. Hooks run in-process with no sandbox; an exception must never reach the gateway.
5. Make the watcher itself refuse a second copy (pid file) so a double fire is harmless.

## C. Verify without restarting the gateway

1. Load the registry with the Hermes venv python (`venv/bin/python` under the hermes-agent dir, with `HERMES_HOME` set; the system python lacks ruamel): `HookRegistry().discover_and_load()` should log `Loaded hook '<name>' for events: ['gateway:startup']`.
2. Call the handler's start function with the watcher alive: expect "already running".
3. Kill the watcher by pid, confirm none running, call it again: expect "started pid=N". Then read the pid file and a fresh log line from the loop.
4. Record in memory that the live-restart path is unproven until a real gateway start fires it.

## Limits to state

- The hook fires only on gateway start. If the watcher dies alone while the gateway stays up, nothing relaunches it; the loop's own error handling has to carry that case.

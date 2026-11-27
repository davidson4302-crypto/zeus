---
name: local-terminal-session-inspection
description: Use when asked what terminal or AI sessions are open.
---

# Local terminal and AI-CLI session inspection

Covers: "what terminal sessions are open", "what is that AI CLI session doing", "close the old ones". Two separate places to look: Hermes background jobs (process_manage list) and shell sessions on the machine itself (ps with a TTY).

## Listing sessions
1. Hermes jobs first: process_manage list. Usually empty, say so in one line.
2. Local shells: `ps -axo pid,ppid,tty,etime,%cpu,stat,command` filtered to rows with a real tty (ttysNNN on macOS, pts/N on Linux) and the AI CLI names in use (for example codex, claude, opencode). The wrapper node process and the native binary are one session, not two. Desktop-app helpers have tty `??` and are not terminal sessions; do not count them.
3. For each session get its working folder: `lsof -a -d cwd -p PID`. Age and CPU alone do not let the user recognize anything.

## Name every session by topic before proposing to close it
- The user recognizes sessions by their thread title or task, not by pid or age. A list of ages is not enough to ask for approval on. Resolve each tty to its saved title/first request/last-active time from the tool's own store, and show that list. If a title cannot be matched to a process, say "cannot match" rather than guessing.
- Codex: threads are indexed under ~/.codex (session index plus a state database). One terminal can host several threads over its life, so a process-to-thread match is uncertain; say so. Closed conversations are saved and reopen with `codex resume` (opens a picker). Only unsent typed input is lost.
- Other CLIs keep their own session store under a dot-folder in the home directory (an active-sessions map from session to pid and working folder, plus per-session summaries and chat history). Injected instructions often arrive as user turns (for example starting with `<system-reminder>`); filter those out before concluding what the user asked it. If nothing remains, say the record holds no typed requests.

## Is it busy or idle
- Idle = ~0 CPU, no child doing work, last_active long ago. State the last-active timestamp in the owner's timezone.
- A listening localhost port is normal for these CLIs. An established outbound HTTPS link (`lsof -nP -a -i -p PID`) is just a kept-open API/MCP connection; it does not mean the session is working or still in use. Never state it as evidence of activity, and reverse DNS on CDN addresses is useless, so do not pretend to identify the service.
- Check child processes (`ps -axo pid,ppid,command | awk '$2==PID'`) for MCP helpers and what each is wired to.

## Flag risk, do not act on it
- Launch flags like `--always-approve` (acts without asking).
- chrome-devtools-mcp with `--autoConnect` plus Chrome's DevToolsActivePort file and a listener on 127.0.0.1:9222: any local program can drive the logged-in browser. Report it and leave it; closing the port is the user's call.

## Closing
- Get the yes against the named list (topic, folder, age), not against a vague count. If the earlier ask was only pids/ages, re-show names and re-ask before killing.
- Recheck right before closing: same pid, same start time, still idle, nothing attached. Then SIGTERM by pid, verify each is gone, report what closed and what was held.
- Tell the user closing ends the process only; saved conversations stay on disk and how to reopen them.
- Hold anything with unclear status (live connection, recent activity) and say why.

## Reply style (iMessage)
Short, answer first: what it is doing (or not), last-active time, the one or two risks, one yes/no question. Own corrections plainly, e.g. when a first claim about a connection was an inference, say so. No tables or code fences.

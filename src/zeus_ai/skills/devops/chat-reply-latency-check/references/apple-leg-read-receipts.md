# Timing the Apple leg with read receipts

The phone-to-Mac and Mac-to-phone hops can't be pinged. The one real signal is the user's phone reporting a read receipt for a text we sent. Photon's sidecar sees those as inbound events with an outbound direction.

## What to set up (once)

1. In the Photon sidecar (`~/.hermes/hermes-agent/plugins/platforms/photon/sidecar/index.mjs`), log read events before the direction filter in the inbound loop: target message id, sent_at, read_at, sent_to_read seconds, observed_at. Add a startup line (e.g. `read-receipt logging on`) after `listening on` so you can prove the new code is the running code.
2. `node --check` the file and run a small offline function test (feed a read event, a non-read event, null input, a throwing input) before relying on it. Keep a backup of the original beside the scratch dir.
3. This is shared Hermes code: a `hermes update` can overwrite it. Record the change and the backup path in a workspace note so it can be re-applied.
4. The user's phone must have Send Read Receipts on for that chat, or nothing is ever logged. Ask them to check; do not assume.

## Making it live

- A sidecar edit only takes effect when the sidecar process restarts. Check the listener's start time (`lsof -nP -ti tcp:<port> -sTCP:LISTEN` then `ps -o lstart=`) against the file's modified time; if the process is older, the edit is not live.
- The terminal guard refuses any command that restarts or kills the gateway or its sidecar from inside the gateway process. Don't try to route around it with a different kill. Have the user text `/restart` (drains active runs and restarts the gateway, which affects every profile it serves), or run `hermes gateway restart` from a separate shell. Otherwise it goes live at the next natural restart.
- Verify by grepping the profile gateway log for the startup marker with a timestamp after the restart. A read-only poll script in the scratch dir is fine for waiting; keep each wait under the tool timeout.

## Reporting

Say plainly whether the marker has been seen. Don't quote an Apple-leg figure until real `read receipt target=` lines exist for texts we sent.

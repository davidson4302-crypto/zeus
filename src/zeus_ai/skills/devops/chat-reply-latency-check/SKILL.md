---
name: chat-reply-latency-check
description: Use when asked for a ping or latency test of my replies.
---

# Chat reply latency check

A "ping test" of me means: how long from the user's text reaching the Mac to my reply leaving it, plus whether the network is healthy. Report real numbers from logs; never estimate.

## Procedure

1. Network leg (read-only): ping the local gateway/router and the model host (for Anthropic, api.anthropic.com) with a small count. Report average ms and packet loss.
2. Reply leg: run `scripts/replytimes.py` (via `python3 -I`, or copy to the scratch dir). It reads the gateway logs for the day and, per inbound Photon text, prints:
   - seconds until the response was ready (the `response ready: ... time=` line), and the model call count
   - seconds from response-ready to the `[Photon] Sending response` hand-off
   Pass `--since 'YYYY-MM-DD HH:MM'` and `--sender '<masked number as it appears in the log>'` for the window and chat being measured.
3. Connection stability (read-only): grep both gateway logs for `received SIGTERM` (the Photon sidecar being stopped) and `photon reconnected` lines for the window. Count the drops and the seconds between each stop and reconnect. Drops are the usual cause of a text that seems lost or a reply that never arrives, so report them even when latency looks fine. The sidecar is stopped from outside the adapter (the adapter logs `sidecar exited unexpectedly (code 0)` afterwards), so don't name a cause unless you found it; a `ps` listing of the sidecar node processes (start times) helps spot a stale duplicate.
4. Split the results: simple replies (api_calls=1) vs replies that did lookups (api_calls>1). Quote medians and ranges for each. Mention that the turn running the test itself is slow because of the test.
5. State what cannot be measured: phone to Mac and Mac to phone through Apple/Photon. Say so in one sentence; do not invent a figure for it. If the user wants that leg timed, see `references/apple-leg-read-receipts.md` (read-receipt logging in the Photon sidecar, how to make it live, and what to verify).

## Pitfalls

- `[Photon] Sending response` lines are written to either the main gateway log (`~/.hermes/logs/gateway.log`) or the profile's own log (`$HERMES_HOME/logs/gateway.log`), depending on which gateway process served the turn. Read both and merge, or the hand-off column pairs with the wrong send and shows absurd values (hundreds to thousands of seconds).
- A hand-off of more than a few seconds is that pairing bug first, not a real delay. Sanity-check any outlier before reporting it.
- Match the sender by the masked number in the log, so other chats' sends stay out of the pairing.
- When a drop is found right before a message that the user re-sent or that went unanswered, check the log for an inbound line for it and a `Sending response` line after it. No inbound line means the message was lost in the drop, so answer it fresh and say so briefly.
- Sidecar SIGTERMs seen in the log are not always yours to explain: when the cause isn't found, report the count and gap times only, and don't try to stop or restart the sidecar from inside a turn (the terminal guard blocks it).
- Use read-only commands only. A latency test must not send test texts to the user unless they ask.
- In chat, lead with the answer (replies take about N seconds; network is fine; the wait is model thinking time). Keep log lines and file paths out of the text.

# Sender: this profile's own Slack bot (`hermes send`)

Use when the message goes to Slack as the agent itself (the profile's own bot), e.g. after the user says "send as yourself" instead of another account.

## Send

```bash
export HERMES_HOME="$HOME/.hermes/profiles/<profile>"
hermes send --to slack:<CHANNEL_ID> --json "<exact text>"
# thread reply instead: --to slack:<CHANNEL_ID>:<thread_ts>
```

- Reuses the profile's own bot token from `.env`; no gateway or LLM needed.
- `--json` returns `success`, `chat_id`, `message_id` (the Slack ts). Keep the ts for readback.
- Omit the thread part to post top level. A chat reply always lands in the thread it answers, so use this route when the post must be top level.
- Use this instead of a chat reply when the message must contain only the requested text (e.g. "only the picked time"). A chat reply has to carry the proof line too, which breaks the "only" rule.

## Readback (Slack is the source of truth, not the send result)

```bash
cd "$HERMES_HOME"; set -a; . ./.env >/dev/null 2>&1; set +a
curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  "https://slack.com/api/conversations.history?channel=<CHANNEL_ID>&latest=<ts>&inclusive=true&limit=1" > "$TMPDIR/slack_rb.json"
```

Then parse the saved file (not `curl | python3`, which the security scan flags) and compare: `ok` is true, `ts` equals the send's message_id, `bot_id` is present, and `text` is exactly what was approved. Never print the token.

Report line: channel id, ts, text read, time read (local time).

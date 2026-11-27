# AgentMail REST

Base: `https://api.agentmail.to/v0`
Header: `Authorization: Bearer $AGENTMAIL_API_KEY`
Never print the key. Never put it on a query string.

## Create

`POST /inboxes`

```json
{"display_name": "Name", "client_id": "stable-id"}
```

Read `email` and `inbox_id` from the 200. Same `client_id` retries the same create.

## List and read

`GET /inboxes/{inbox_id}/messages?limit=20`

`GET /inboxes/{inbox_id}/messages/{urlencoded_message_id}`

Prefer `extracted_text`, then `text`. Message ids look like `<...@...>`. Encode the whole id.

## Reply

`POST /inboxes/{inbox_id}/messages/{urlencoded_message_id}/reply`

```json
{"text": "Plain text body."}
```

A 200 returns `message_id` and `thread_id`. Same `thread_id` means you stayed in the conversation.

## Watch

Svix headers: `svix-id`, `svix-timestamp`, `svix-signature`. Do not treat a Hermes subscription secret as that verifier.

A silent watch script prints only new inbound lines. Empty stdout means no delivery. Store seen ids beside the profile cache, capped, and skip the inbox's own from address.

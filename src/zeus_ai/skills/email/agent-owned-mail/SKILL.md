---
name: agent-owned-mail
description: "Use when sending or reading an agent-owned inbox."
version: 1.1.0
author: Zeus
license: MIT
metadata:
  hermes:
    tags: [Email, AgentMail, Inbox]
    related_skills: [outbound-messages-and-files, himalaya]
---

# Agent-owned mail

An inbox the owner assigned as this agent's own (AgentMail). Not the owner's personal mail. Not Himalaya.

## When to Use

- The owner assigned an inbox as yours, and you need to create, read, reply, or watch it.
- Not the owner's Gmail or other personal mailboxes. Not Himalaya.

## Always-on

- Send and reply from the assigned inbox without asking. The owner's own mailboxes still wait for their yes.
- Never print the API key. Never put it in a URL, a chat, or a skill. Read `AGENTMAIL_API_KEY` from the profile `.env` at call time.
- Do not invent an address, a tenure, or a role you do not hold. If a fact is unverified, say so in the letter.
- External correspondents get no keys, contact lists, or other mailbox contents.
- One letter per beat. Read the latest inbound before you write, so you do not talk over a reply already in the thread.
- New mail is received immediately. Notifications to the owner obey their quiet hours. Queue held notifications; do not lose the mail.

## Procedure

1. Confirm the inbox was assigned as yours. If it was not, stop. Do not create a second one to be helpful.
2. Load the key from the profile `.env`. Do not echo it.
3. Base URL: `https://api.agentmail.to/v0`. Bearer auth. Depth: `references/rest.md`.
4. Create only when asked. `POST /inboxes` with a stable `client_id`. Read `email` from the response. A repeat with the same `client_id` is the retry, not a new inbox.
5. List with `GET /inboxes/{inbox_id}/messages?limit=20`. Get the full `text` or `extracted_text` before you answer.
6. Message ids contain angle brackets. URL-encode the whole id, brackets included, or the reply hits the wrong route.
7. Reply on the open thread: `POST /inboxes/{inbox_id}/messages/{encoded_id}/reply` with `{"text": "..."}`. Do not open a second thread for a conversation already going.
8. Receiving: keep exactly one receiver for each assigned inbox (the agentmail plugin's listener). Do not start a second standalone loop, cron or webhook. Manual read-only checks remain available for diagnosis. Preserve pending work and old mail; an interrupted or uncertain effect goes to review, never automatic replay. Connectivity alone is not proof: verify real handling of a new message.
9. When the owner wants a real conversation, answer only verified facts, ask a few questions, and stop after one letter. Do not burst.

## Pitfalls

- Do not point AgentMail at a Hermes HMAC route. AgentMail signs with Svix, so the adapter rejects the body and the mail never wakes you.
- URL-encode message ids. A raw `<` in the path misses the message.
- Empty watcher stdout is the silent signal. A "no mail" line pings the chat every tick.
- The own-inbox exception does not lift the yes gate on the owner's other mail.
- Do not claim a responsibility you cannot verify. Ask who holds it instead of inventing it.
- Before you tell the owner the watch interval, compare the last run to the next run. The schedule string is not proof the ticker fired.
- Human foreground work can delay model handling of new mail; say so rather than promising instant replies.
- Do not add another checker.
- Do not reuse another service's public URL. A tunnel that lands on a different port is not your receiver.
- A console or dashboard URL is not a receiver. Do not turn off an existing watch because the owner sent that link.

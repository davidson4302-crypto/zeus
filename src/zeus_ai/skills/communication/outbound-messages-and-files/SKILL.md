---
name: outbound-messages-and-files
description: Use when asked to send a file or message to someone.
version: 1.0.0
author: Zeus
license: MIT
metadata:
  hermes:
    tags: [communication, email, messaging, files, delivery, outbound]
---

# Outbound messages and files

## When to Use

- The user asks to send, email, forward, or share a file or message with another person, including vague references like "send Alex that file".
- Covers text-only messages and emails too. The file steps are skipped when nothing is attached.
- Not for ordinary replies in the current chat. It does cover a clean post to the current channel when the message must contain only the requested text, or must land top level rather than in the thread.

A send is external and cannot be recalled, so the order below matters. Confirm the file, the address and the sender before any send, and never report "sent" without a readback.

Sender specifics (AgentMail key check, size limits, drafts, readback) are in `references/sender-agentmail.md`. Posting to Slack as this profile's own bot (send + Slack readback) is in `references/sender-slack-own-bot.md`.

## Procedure

1. Pin down the exact file or text.
   - Search in this order: the current thread, session history (session_search), memory, then the workspace and recent local files.
   - A history query that reports zero sessions searched proves nothing. Browse recent sessions before concluding the context is missing.
   - Before asking the user to paste text again, search session history for the original request (session_search with a distinctive word or channel ID from it). Making the user repaste something already on record costs them a turn.
   - A chat thread can reset. If earlier context for a file reference is gone and history has no copy, ask the user to restate it. Assuming it exists sends whatever file you last saw.
   - Accept a hit only if it names both the file and the recipient. A keyword match can come from an unrelated note.
   - If nothing matches, ask which file. Never choose the nearest one: a wrong attachment cannot be recalled once sent.
   - Note the file's size. A sender can refuse an attachment over its limit, so check it now, not after the send fails.

2. Get a full recipient address.
   - A first name is not an address. Use an address the user gave, or a contact the user has confirmed.
   - If the address came from memory or history, confirm it in full with the user before sending.
   - An address the user typed in this conversation, or one the thread being answered carries, needs no extra confirmation.
   - A contact list or roster the user gave directly counts as confirmed. Don't ask again for an address on it: the user wants one ask, not two.
   - If a name matches more than one entry, send to the person's own address, not a shared role inbox. For example, a person's own inbox rather than a shared team alias. Name the address you used in the report so a wrong pick is caught at once.

3. Confirm a sender is connected for this session.
   - Run tool search with the service name, a send action, and the object, e.g. "<service> send email". If nothing matches, retry once with different wording, then conclude. Search matches on words, so wording decides the result.
   - A REST sender counts as connected once its key is in the profile's .env. Check by variable name only: list the names with `cut -d= -f1 <profile>/.env`, and test the process environment with `test -n "$NAME" && echo set`. Never print a value. The file is the source of truth; a missing process variable alone proves nothing.
   - If no sender is connected, say so in one line and offer the two real options: the user forwards it, or the user connects a sender. Put forwarding first when the user can't finish a setup step from where they are. Stop there.
   - If the request names an account or tool you can't reach (another workspace's Slack account, a named send tool), still do the parts that need no send. Pick the time, draft the exact text, and show both. Then ask one question offering the closest real route, such as your own bot or your own inbox. That way one yes finishes the job.
   - A missing named Slack tool doesn't mean there's no Slack route. This profile's own bot can post through `hermes send` (see references/sender-slack-own-bot.md). When the user says to send as yourself, that is the route. Send right away, without asking again.
   - Check that the destination is consistent before you ask. A channel link and a typed channel ID can name different channels. Point out the mismatch rather than picking one, because a post in the wrong channel can't be taken back.
   - Never ask for, accept, or echo a key or password in chat. Chat is logged, so a pasted key stays exposed and must be rotated. Name the variable and the file, and let the user add the line themselves.
   - Never improvise a send through another account, an ad-hoc shell command, or another channel. Use the connected sender's documented route (its reference file). An unauthorized send has no record to read back, so it can never be verified.

4. Record the request before multistep work.
   - Check for an existing work item first (`zeus_work` `status`). Update it instead of creating a second one: `zeus_work` `checkpoint` with the id and the new next_action.
   - Read the work item back with `status` and confirm each field you changed. If a field you set is still empty, repeat that wording in next_action.
   - Calls and data shapes: see the company-records skill, goals-tool reference.
   - If a work action fails twice with the same arguments, stop repeating it. Read the record's current shape with `status`, then change the call.
   - Otherwise create one (`zeus_work` `create`). Its title and criteria hold the exact original request, the recipient, the draft text (text-only) or the file (attachment), and the expected output.
   - For an attachment, record the file and its sha256 as a criterion check, so the readback compares the same bytes.

5. Ask one question that shows exactly what will be sent: the full recipient address, the sender, and either the file name and size (attachment) or the subject and full body (text-only). Wait for the yes. One yes approves exactly the text shown; any change to the wording needs a fresh yes.
   - When the user's request points at text you already showed, like "send this message to X", the request is the yes. Send it unchanged. Asking again makes the user say yes twice.

6. Send once, through the connected sender only.
   - A 2xx response carries the message id. Keep it; the readback needs it.
   - A 4xx means the sender rejected the request. Check that no sent record exists, fix the cause, then send once.
   - A timeout, dropped connection, or 5xx may still have sent. Read the sent record first. A second send doubles the external effect.

7. Read back before saying "sent".
   - Check the sender's sent record, not the chat: recipient, attachment name, sha256 if exposed, timestamp, message ID. Quote those values.
   - AgentMail's readback fields, matching rules, and the list-lag caveat are in references/sender-agentmail.md.
   - The send call returning only proves the action ran. On first use with a new sender, confirm which record it exposes for sent messages before relying on this step.
   - Then record the readback on the work item: save the message id, recipient, subject, created_at and time read in a checkpoint's next_action (or a small evidence artifact), and report verified from the readback. Details are in company-records, references/goals-tool.md. A closed task alone does not complete the work.

## Replying to a third party from a voice note or dictated brief

- Transcribe first, then write the reply as a draft and show it in full: sender account, recipient address, thread, and body. Do not send until the user says yes.
- Include only what the user said. If you add a line they did not dictate (a call offer, a sign-off ask), say so under the draft and offer to cut it.
- To quote or summarise an inbound email for the user, find it in the mailbox it was sent to. If several mail accounts are connected (for example Gmail accounts through Composio; list them via the toolkit's connection status), search each one with the sender's name and `newer_than:`. The reply goes from the account the thread lives in.

## Re-checking before a approved send goes out

- A yes covers the text and file the user saw. Before sending a reissue or follow-up of something already distributed, read the inbox and the relevant threads for replies that arrived after the draft was made (reviewer corrections, objections, open questions). Unread feedback can make the approved copy wrong.
- If a reply asks for fixes, make the fixes that need no one else, request the missing input from its owner (a message to the person who holds the source), and hold the send. Tell the user in one short message: not sent yet, why, what is pending, and that they can say to send as-is. Do not send a known-flawed copy because the yes was given earlier.
- Keep a hold note on disk (what was approved, what is pending, the exact send steps and recipients) so the send can finish in one pass when the input arrives.

## Relaying the user's instructions to several people

- Reply on each person's own latest thread in your inbox (AgentMail `/reply`), with only that person's items; use `/messages/send` for someone with no thread from you.
- Write the sends as one script file and run it once. Inline interpreters trigger a security prompt, and a rerun would double-send.
- Relay only what the user said. If "both" or "everyone" is ambiguous, name your reading in the report.
- Readback: list the inbox and confirm each recipient shows with label `sent`. Report who was told what, in one short line each.

## Keep it fast while the user waits

- If the answer is already known (no file yet, no sender, no address), reply with it. Don't chain more lookups before the reply.
- Budget record calls to one write and one view readback per turn. Repeats of a failing call add minutes the user feels on a phone.
- Report the real send state. Don't say a goal or record was saved until a readback shows it.

## Report shape

- First line: sent (with the local time) or not sent, plus the one thing that's missing.
- Then the exact text, the recipient address, and one proof line: message id, the sent label, and the time read.
- No process narration, headers or sign-offs.

# Own AgentMail tools

This plugin adds four tools in `own_agentmail` to an existing email-enabled Hermes profile:

- `agentmail_inbox_list`: GET messages in its exact assigned own inbox, paginated.
- `agentmail_message_read`: GET one full message, preserving unread state.
- `agentmail_send`: send a new plain-text email, or reply to an exact own-inbox message.
- `agentmail_send_status`: read-only destination reconciliation using the same action ID.

It installs no watcher, scheduler, mailbox, account, credential or reporting route. Root owns live profile enablement and native receiver verification separately.

## Assignment and keys

The assigned mailbox is read from `platforms.agentmail.extra.inbox_id`. The existing credential comes from this exact profile's private `.env`, or the existing configured `platforms.agentmail.extra.credentials_file` JSON. The private JSON must contain matching `inbox_id` and `api_key`, be owner-controlled and mode 0600 (or narrower), and not be a symbolic link. No environment fallback borrows another agent's key. The binding is checked again on every operation. STOP, ESTOP, PAUSED and gateway.parked markers on the home and, for local named profiles, Hermes root prevent operations.

Profile name `default` and a standalone home are supported. The owner's team (internal recipients) comes from `team.members` in the Zeus config (`settings.TEAM`, each member with an `emails` list). It is non-secret configuration and empty by default, so with no team every send outside the owner's own request needs approval.

## Sending and approvals

The model provides only `action_id`, `to`, `subject`, `text`, and optionally `reply_to_message_id`. It cannot choose the sender/mailbox, add headers, supply approval booleans, or expose credentials.

All recipients must be plain email addresses. When every recipient is on the configured team list, no extra approval is requested. A current native owner request can authorize its exact email without a second prompt when recipients, subject and full body are present literally in that authenticated request. The source is captured from native turn/owner metadata, never from tool arguments or email contents.

Other sends use the existing own-line iMessage approval Hub. Its exact saved action, payload hash, nonce, request status and verified actor/runtime/transport binding must match before the single send. A missing Hub returns `approval_path_missing` without sending. After that route is restored, retrying the same action can prepare its first approval; uncertain approval deliveries are never automatically repeated.

A previous conversational approval with no exact current payload provenance, file-sourced wording, automatic continuation or an edited/paraphrased body may use the existing Hub instead. The plugin does not infer prior authority from conversation prose or model-supplied flags.

## Durability and verification

A mode-0600 profile-local SQLite ledger is stored in `plugin-data/own-agentmail/sends.sqlite3`. It binds one action ID to one exact email and nonce, commits `attempted` before POST, and never repeats POST after interruption, uncertainty, submitted state or completion. The exact same payload also remains bound to its original action if supplied under a different ID. This deliberately prevents ID changes from replaying an uncertain email; intentionally repeating an identical message needs a separately reviewed policy rather than silently bypassing the existing binding.

The provider's Sent message must match own sender, recipients, body, subject, mailbox/message/thread IDs, sent label, no unexpected CC/BCC/attachments, and in-reply-to for replies. Successful verification is `sent_verified`, not recipient delivery or reading. Status may retry GET only. An unknown POST without a returned provider ID remains `outcome_unknown` and requires destination reconciliation, never resend.

No redirect is followed. Provider responses are capped at 2 MB and 25 seconds. Email contents are marked untrusted data. Keys and raw provider exceptions are never returned.

## Tests

The test suite uses fixture homes, keys, requests, approvals and transport responses only; it changes no live profile and sends no real mail.

Tests cover wrong-profile/inbox/message refusal, scoped private credentials, GET-only reads, unread preservation, URL confinement, bounds/no redirects, team sends, exact current owner authority, untrusted-source rejection, outside approval yes/no and forged approval refusal, one exact action binding, committed-before-POST state, concurrent double calls, changed-ID duplicates, unknown effects, GET-only reconciliation, STOPs, assignment changes, correct reply binding and exactly four registered tools.

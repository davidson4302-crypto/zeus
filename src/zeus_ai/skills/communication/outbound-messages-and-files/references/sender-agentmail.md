# Sender: AgentMail

Use with `outbound-messages-and-files` when AgentMail is the connected sender. The sending address is the agent's own AgentMail inbox (`agent.email` in the Zeus config). Sending from it grants no access to any other mailbox.

## Credential and pre-send checks

- The key is `AGENTMAIL_API_KEY` in the agent profile's `.env`. Check it by name only (SKILL.md step 3). Never print or paste the value.
- Before the first send, run two read-only checks with `Authorization: Bearer <key>`:
  - `GET https://api.agentmail.to/v0/auth/me` returns the credential's identity and scope. Expect `scope_type` = inbox and an `inbox_id` equal to the sending address.
  - `GET https://api.agentmail.to/v0/inboxes` must list that address.
- Read the key at call time. Don't put it in a URL, a log, or a chat message.

## Send

- `POST https://api.agentmail.to/v0/inboxes/{inbox_id}/messages/send` with a JSON body. Fields include `to`, `cc`, `bcc`, `subject`, `text`, `html`, `labels`, `attachments`.
- Send both `text` and `html` when there's a body. Plain text alone hurts readability and deliverability.
- Minimal call for a text-only message: `POST /inboxes/{inbox_id}/messages/send` with a JSON body of `to`, `subject`, `text`, `html`. One call per approved message.
- The response carries `message_id` and `thread_id`. Keep the `message_id` for the readback.
- `attachments` is a list. Each item has `content` (base64) or `url`, plus optional `filename` and `content_type`.
- Size limits:
  - Inline `content`: 6 MB for the whole request, body and metadata included. Over that, the API returns 413. Send a `url` instead.
  - Remote `url`: about 30 MB combined per message. The URL must download without auth headers or cookies and must return a 2xx. Redirects and pre-signed URLs work.
- Before the first real send, read the send page's response section for the field names: `api-reference/inboxes/messages/send.md`.

## Drafts for approval

- A draft is an unsent message in the sending inbox. It can hold recipients, subject, body, and attachments.
- To show the exact content before a send: create the draft, show the user the recipient, subject, file name, and size, and send the draft only after an explicit yes. Pages: `api-reference/inboxes/drafts/create.md` and `api-reference/inboxes/drafts/send.md`.

## Readback after send

The by-id record is the authority. The inbox list is corroboration only.

- Read by id: `GET /inboxes/{inbox_id}/messages/{urlencoded_message_id}`. The id contains angle brackets; URL-encode all of it, or the request hits the wrong route. Doc page: `api-reference/inboxes/messages/get.md`.
- Match the fields the approval named:
  - `to` comes back as a list even when you sent one string. Check membership.
  - `subject` equals the approved subject.
  - `labels` includes `sent`.
  - `created_at` is the sent time to quote (UTC).
  - `text` can end with a platform footer (a `--` line and a Sent via line). Compare the leading text.
- If the read is not 200 or a field does not match, retry up to three times with a short pause. Retries are reads: never resend to get a fresh result.
- If it still fails, list the inbox (`GET /inboxes/{inbox_id}/messages?limit=20`) and look for the same subject and recipient. The list can lag the send by several seconds, so wait a few seconds and list again before calling it a discrepancy.
- Quote the message id, the values read, and the time read.

Recipe (read-only; fill the constants from the approved send):

```python
import json, time, urllib.request, urllib.error, urllib.parse

import os
ENV = os.path.join(os.environ['HERMES_HOME'], '.env')  # the sender profile's .env
INBOX = '<agent inbox address>'  # the agent's own inbox, e.g. name@agentmail.to
RECIPIENT = 'recipient@example.test'  # the approved address
SUBJECT = 'approved subject'  # the approved subject
MESSAGE_ID = '<message_id from the send response>'

key = None
with open(ENV) as f:
    for line in f:
        if line.startswith('AGENTMAIL_API_KEY='):
            key = line.split('=', 1)[1].strip()  # strip surrounding quotes too if the value has them

BASE = 'https://api.agentmail.to/v0'
INBOX_ENC = urllib.parse.quote(INBOX, safe='')

def call(method, path, body=None):
    headers = {'Authorization': 'Bearer ' + key, 'User-Agent': 'Mozilla/5.0'}
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            raw = r.read().decode('utf-8', 'replace')
            return r.status, (json.loads(raw) if raw.strip() else {}), None
    except urllib.error.HTTPError as e:
        return e.code, None, e.read().decode('utf-8', 'replace')[:300]
    except Exception as e:
        return None, None, type(e).__name__

def is_match(m):
    to = m.get('to')
    to = to if isinstance(to, list) else [to]
    return (RECIPIENT in [str(t) for t in to]
            and m.get('subject') == SUBJECT
            and 'sent' in (m.get('labels') or []))

record = None
for attempt in range(4):  # one read, then up to three retries
    if attempt:
        time.sleep(4)
    status, m, err = call('GET', '/inboxes/' + INBOX_ENC + '/messages/' + urllib.parse.quote(MESSAGE_ID, safe=''))
    if status == 200 and isinstance(m, dict):
        record = m
        if is_match(m):
            break
print('by-id match:', bool(record and is_match(record)), '| created_at:', (record or {}).get('created_at'))

status, lst, err = call('GET', '/inboxes/' + INBOX_ENC + '/messages?limit=20')
hits = [i for i in (lst or {}).get('messages', []) if i.get('subject') == SUBJECT] if status == 200 else []
print('list hits:', len(hits), '| list status:', status, err or '')
```

## Docs access

- Index: `https://docs.agentmail.to/llms.txt`. Append `.md` to any page URL for clean Markdown.
- If a web extract call fails with HTTP 429, fetch the `.md` URL with a plain HTTP GET. It returns the same page text.

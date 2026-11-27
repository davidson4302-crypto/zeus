"""Exact send: after the owner's yes, code sends the stored email reply word for word.

The chat Zeus never retypes an approved email from its own notes: ordinary code controls the approval and the
execution, a yes is bound to the exact draft (duplicate and no-replay checks), and before retrying any uncertain send
the destination is checked first.

Narrow on purpose: a reply inside the email thread the item came from, to people already on that thread. Anything else
(a brand-new email, a text, a task change) stays with the chat Zeus's own tools.

Nothing here decides WHETHER to send. `life approve` calls it only after the owner's yes is on record.
LIFE_SEND_DRY=<file>: nothing leaves; the request is appended to that file (tests and rehearsals). Outside the live
life-engine folder a dry file is REQUIRED: a sandbox can never send.

- thread_state(): like in_thread(), and it also says when someone ELSE wrote in the thread after the email this reply
  answers ("moved"): the reply was prepared without that message, so it is redone, not sent.
- LIFE_FAKE_MAIL=<program>: a pretend mail service for fault tests. Honoured ONLY outside the live life-engine folder;
  in the live folder it is ignored, so the real engine can never be pointed at a pretend one.
- A readback compares the words after undoing web escapes (a mailbox preview shows "can't" as "can&#39;t").
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import time
from pathlib import Path

import settings
import sources as S

R = S.R                                    # the read-only mail helpers: COMPOSIO, ACCOUNTS, _env, read_message
# The live life-engine folder. Read from the config file, never from the LIFE_HOME environment variable, so a sandbox
# that sets LIFE_HOME is never mistaken for the live folder.
LIVE_HOME = Path(settings.get('paths.life_home', str(settings.PROFILE_DIR / 'workspace' / 'life-engine')))
OWNER_TURN_FILE = 'last-owner-turn.json'    # written by the chat plugin on every real message from the owner
OWNER_FRESH = 15 * 60
SEND_TIMEOUT = 90                           # seconds the mail service gets to answer; after that the outcome is unknown
FAKE_MAIL = 'LIFE_FAKE_MAIL'                # a pretend mail service, sandboxes only (see send)
MIN_BODY, MAX_BODY, MAX_PEOPLE = 20, 6000, 6
_ADDRESS = re.compile(r"[a-z0-9][a-z0-9._%+'\-]*@[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", re.I)
# A blank someone still has to fill in: [name], {{first}}, TBD, XXX, <date>.
BLANK = re.compile(r"\[[^\]\n]{1,40}\]|\{\{[^}\n]*\}\}|\bTBD\b|\bXX+\b|<[A-Za-z][^>\n]{0,40}>")


class SendFailed(RuntimeError):
    """The mail service said no. Nothing was sent."""


class SendUncertain(RuntimeError):
    """No clear answer came back. It may or may not have gone: read the thread before anything else."""


def _addresses(value) -> list:
    items = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    seen, out = set(), []
    for item in items:
        for a in _ADDRESS.findall(str(item)):
            box = S.same_mailbox(a)
            if box and box not in seen:
                seen.add(box)
                out.append(a.lower())
    return out


def _by_owner(tail_row: dict) -> bool:
    """Did the owner's own mailbox send this message in the thread? (sources.thread_tail marks it with a from_* flag.)"""
    if 'from_owner' in tail_row:
        return bool(tail_row['from_owner'])
    return any(v is True for k, v in tail_row.items() if str(k).startswith('from_'))


def _his(address: str) -> bool:
    return S.same_mailbox(address) in {S.same_mailbox(a) for a in S.OWN_ADDRESSES}


def from_job(send, opp: dict):
    """The background job's "send" object, completed from the item's own email. The job supplies the people and the
    words; which mailbox and which thread come from the item itself, never from the job. -> draft, or None when
    there is nothing code can send (then the step stays with the chat Zeus, exactly as before)."""
    if not isinstance(send, dict):
        return None
    mail, thread = opp.get('latest_email') or {}, opp.get('thread') or {}
    mailbox = mail.get('mailbox') or thread.get('mailbox')
    thread_id = thread.get('thread_id') or mail.get('thread_id')
    body = re.sub(r'\s*—\s*|\s+–\s+', ', ', str(send.get('body') or '').replace('\r\n', '\n')).strip()   # house style: no em dashes
    to = [a for a in _addresses(send.get('to')) if not _his(a)]
    cc = [a for a in _addresses(send.get('cc')) if not _his(a) and S.same_mailbox(a) not in {S.same_mailbox(x) for x in to}]
    if not (mailbox and thread_id and mail.get('message_id') and to and body):
        return None
    return {'kind': 'email_reply', 'mailbox': str(mailbox), 'thread_id': str(thread_id), 'reply_to': str(mail['message_id']),
            'to': to, 'cc': cc, 'body': body}


def fingerprint(draft: dict) -> str:
    """The exact thing the owner's yes is bound to."""
    canon = {k: draft.get(k) for k in ('kind', 'mailbox', 'thread_id', 'reply_to', 'to', 'cc', 'body')}
    return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def thread_people(draft: dict, read=None):
    """Who is on the email this draft answers (From, Reply-To, To, Cc), the owner's own addresses left out.
    -> set of mailboxes, or None when the email could not be read (never treated as "anyone")."""
    account = R.ACCOUNTS.get(draft.get('mailbox'))
    if not account:
        return None
    try:
        m = (read or R.read_message)(account, draft['reply_to'])
    except Exception:
        return None
    headers = ((m.get('payload') or {}).get('headers') or []) if isinstance(m, dict) else []
    people = set()
    for h in headers:
        if str(h.get('name') or '').lower() in ('from', 'reply-to', 'to', 'cc'):
            people |= {S.same_mailbox(a) for a in _ADDRESS.findall(str(h.get('value') or ''))}
    people = {a for a in people if a and not _his(a)}
    return people or None


def problems(draft: dict, people) -> list:
    """Why this draft cannot be sent as it stands, in plain words. Empty = it can."""
    out = []
    if draft.get('kind') != 'email_reply' or not R.ACCOUNTS.get(draft.get('mailbox')):
        return ['it is not a reply from one of the owner\'s mailboxes']
    body = str(draft.get('body') or '')
    if not MIN_BODY <= len(body) <= MAX_BODY:
        out.append('the reply is %s' % ('almost empty' if len(body) < MIN_BODY else 'far too long to be one email'))
    blank = BLANK.search(body)
    if blank:
        out.append('the reply still has a blank to fill in: "%s"' % blank.group(0))
    everyone = list(draft.get('to') or []) + list(draft.get('cc') or [])
    if not draft.get('to'):
        out.append('nobody is named to send it to')
    if len(everyone) > MAX_PEOPLE:
        out.append('it goes to %d people, more than a reply should' % len(everyone))
    if people is None:
        out.append('the email it answers could not be read, so who is on that thread is unknown')
    else:
        strangers = [a for a in everyone if S.same_mailbox(a) not in people]
        if strangers:
            out.append('%s %s not on that email thread' % (', '.join(strangers), 'is' if len(strangers) == 1 else 'are'))
    return out


def _start(text: str, n: int = 60) -> str:
    return re.sub(r'\s+', ' ', html.unescape(str(text or ''))).strip().lower()[:n]


def in_thread(draft: dict, tail=None):
    """What already sits in the thread after the email this draft answers.
    -> 'ours' (this very reply is there), 'other' (something else went out from the owner's mailbox), 'nothing', or None
    when the thread could not be read (never treated as 'nothing')."""
    if tail is None:
        tail = S.thread_tail(R.ACCOUNTS.get(draft.get('mailbox')), draft['thread_id'], draft['reply_to'])
    if tail is None:
        return None
    mine = [t for t in tail if _by_owner(t)]
    if any(_start(t.get('preview')) and _start(t.get('preview')) == _start(draft['body']) for t in mine):
        return 'ours'
    return 'other' if mine else 'nothing'


def thread_state(draft: dict, tail=None):
    """What sits in the thread after the email this draft answers, for the last look before a send and for settling
    an unknown one. -> 'ours' (this very reply is there) | 'other' (something else went out from the owner's mailbox) |
    'moved' (someone else wrote after it: the reply was prepared without that) | 'nothing' | None when the thread
    could not be read (never treated as 'nothing')."""
    if tail is None:
        tail = S.thread_tail(R.ACCOUNTS.get(draft.get('mailbox')), draft['thread_id'], draft['reply_to'])
    if tail is None:
        return None
    seen = in_thread(draft, tail=tail)
    if seen != 'nothing':
        return seen
    return 'moved' if any(not _by_owner(t) for t in tail) else 'nothing'


def owner_spoke(life_home, now=None):
    """Did a real message from the owner reach Zeus in the last 15 minutes? The chat plugin writes the time of each one.
    -> True / False, or None when there is no record at all (a chat version that does not write it yet)."""
    try:
        at = float(json.loads((Path(life_home) / OWNER_TURN_FILE).read_text())['at'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (time.time() if now is None else now) - at <= OWNER_FRESH


def send(draft: dict, life_home, *, run=None) -> dict:
    """Send the reply exactly as stored. -> receipt. Raises SendFailed (nothing went) or SendUncertain (unknown)."""
    account = R.ACCOUNTS.get(draft.get('mailbox'))
    payload = {'user_id': 'me', 'thread_id': draft['thread_id'], 'message_body': draft['body'], 'recipient_email': draft['to'][0],
               'extra_recipients': draft['to'][1:], 'cc': draft.get('cc') or [], 'is_html': False}
    dry = os.environ.get('LIFE_SEND_DRY')
    live = Path(life_home).resolve() == LIVE_HOME.resolve()
    pretend = None if live else os.environ.get(FAKE_MAIL)      # never honoured in the live folder
    if not dry and not live and not pretend:
        raise SendFailed('this is not the live life-engine folder, and a sandbox never sends')
    if dry:
        with open(dry, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'at': time.time(), 'tool': 'GMAIL_REPLY_TO_THREAD', 'account': account, 'payload': payload}, ensure_ascii=False) + '\n')
        return {'dry': True, 'at': time.time(), 'fingerprint': fingerprint(draft)}
    try:
        p = (run or subprocess.run)([R.COMPOSIO if live else pretend, 'execute', 'GMAIL_REPLY_TO_THREAD', '-d', json.dumps(payload), '--account', account],
                                    capture_output=True, text=True, timeout=SEND_TIMEOUT, env=R._env(), cwd=str(R.HOME), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise SendUncertain('the mail service gave no answer within %d seconds' % SEND_TIMEOUT) from None
    except OSError as e:
        raise SendFailed('the mail tool could not be started (%s)' % type(e).__name__) from None
    try:
        data = json.loads(p.stdout)
        if isinstance(data, dict) and data.get('storedInFile') is True:
            path = Path(str(data.get('outputFilePath') or '')).resolve()
            try:
                data = json.loads(path.read_text()) if path.is_relative_to(R.COMPOSIO_TMP.resolve()) else None
            finally:
                if path.is_relative_to(R.COMPOSIO_TMP.resolve()):
                    path.unlink(missing_ok=True)
    except (ValueError, OSError):
        data = None
    if isinstance(data, dict) and data.get('successful') is True and not p.returncode:
        inner = data.get('data') if isinstance(data.get('data'), dict) else {}
        inner = inner.get('response_data') if isinstance(inner.get('response_data'), dict) else inner
        return {'at': time.time(), 'fingerprint': fingerprint(draft), 'message_id': inner.get('id'), 'thread_id': inner.get('threadId')}
    if isinstance(data, dict) and data.get('successful') is False:
        raise SendFailed(str(data.get('error') or 'the mail service refused it')[:200])
    raise SendUncertain('the mail service answered in a way that could not be read')


# ---- the ONE email that stands in for an urgent text that did not go (urgent-fallback-on) ----
# This is NOT the reply path above (that one answers a thread from one of the owner's mailboxes after their yes). This
# one goes from Zeus's OWN inbox to the owner's alert inbox, with words written by code. urgentfallback.py decides
# whether and keeps it to one; nothing here decides anything. The same sandbox rules as send(): outside the live
# life-engine folder a dry file or a pretend mail program is REQUIRED, so a sandbox can never send.
# No agent inbox configured (agent.email) or no owner address: the stand-in email is off, and alert() says so.
ALERT_FROM = settings.AGENT_EMAIL                    # Zeus's own AgentMail inbox (agent.email); empty = no stand-in email
ALERT_TO = (settings.get('mail.alert_to') or settings.OWNER_EMAILS.get('personal')
            or next(iter(settings.OWNER_EMAILS.values()), ''))   # the owner's inbox for alerts
ALERT_API = 'https://api.agentmail.to/v0'
ALERT_PROFILE = _ALERT_REAL_PROFILE = settings.PROFILE_DIR     # Zeus's own Hermes profile folder
ALERT_KEY_FILE = ALERT_PROFILE / '.env'              # the profile's own key file (AGENTMAIL_API_KEY); read only in the live folder
ALERT_STOP_FILES = ('STOP', 'ESTOP', 'PAUSED', 'gateway.parked')      # the same four the own-agentmail plugin refuses to send under


def alert_parked(life_home):
    """Is Zeus parked? -> the name of the stop file found in its profile (or in the Hermes home above it), else None.
    The same check the own-agentmail plugin makes before any send. A sandbox has no profile of its own, so outside the
    live folder the real profile is not looked at (a test points ALERT_PROFILE at a pretend one)."""
    if Path(life_home).resolve() != LIVE_HOME.resolve() and ALERT_PROFILE == _ALERT_REAL_PROFILE:
        return None
    roots = [Path(ALERT_PROFILE)] + ([Path(ALERT_PROFILE).parent.parent] if Path(ALERT_PROFILE).parent.name == 'profiles' else [])
    for root in roots:
        for name in ALERT_STOP_FILES:
            try:
                if (root / name).exists():
                    return name
            except OSError:
                return name                               # the stop file could not be looked for: treated as parked
    return None


def _alert_key() -> str:
    for line in ALERT_KEY_FILE.read_text().splitlines():
        name, _, value = line.partition('=')
        if name.strip() == 'AGENTMAIL_API_KEY' and value.strip():
            return value.strip().strip('"').strip("'")
    raise SendFailed("Zeus's own mail key could not be found")


class AlertHeld(SendFailed):
    """No mail was handed off because quiet hours began."""


def _check_alert_hours(life):
    from ownertime import alert_open, now
    at = now()
    if alert_open(at, life=life) > at:
        raise AlertHeld('quiet hours on the owner clock')


def _alert_post(payload: dict) -> dict:
    """The live send: one POST to Zeus's own mail service. -> its answer. Raises SendFailed (a clear no: nothing went)
    or SendUncertain (no clear answer)."""
    import urllib.error
    import urllib.parse
    import urllib.request
    try:
        key = _alert_key()
    except OSError:
        raise SendFailed("Zeus's own mail key file could not be read") from None
    body = json.dumps({k: payload[k] for k in ('to', 'subject', 'text')}).encode()
    req = urllib.request.Request(f"{ALERT_API}/inboxes/{urllib.parse.quote(ALERT_FROM)}/messages/send", data=body, method='POST',
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept': 'application/json'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):      # the key never follows a redirect to another address
        def redirect_request(self, *_a, **_k):
            return None
    _check_alert_hours(LIVE_HOME)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=SEND_TIMEOUT) as resp:   # no proxy settings, no redirects (as the own-agentmail plugin)
            data = json.loads(resp.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 500:                           # a redirect that was not followed, or a clear no: nothing went
            raise SendFailed(f'the mail service refused it ({e.code})') from None
        raise SendUncertain(f'the mail service answered with an error ({e.code})') from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SendUncertain(f'the mail service gave no clear answer ({type(e).__name__})') from None
    if isinstance(data, dict) and data.get('message_id'):
        return data
    raise SendUncertain('the mail service answered in a way that could not be read')


def alert(subject: str, body: str, life_home, *, post=None, run=None) -> dict:
    """Send the stand-in email exactly as given, from Zeus's own inbox to the owner's alert inbox. -> receipt.
    Raises SendFailed (nothing went) or SendUncertain (unknown: read that inbox before anything else, alert_arrived)."""
    parked = alert_parked(life_home)
    if parked:
        raise SendFailed(f'Zeus is parked ({parked}): nothing is sent')
    if not (ALERT_FROM and ALERT_TO):
        raise SendFailed('no agent inbox or owner alert address is configured: the stand-in email is off')
    payload = {'from': ALERT_FROM, 'to': [ALERT_TO], 'subject': str(subject), 'text': str(body)}
    mark = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    dry = os.environ.get('LIFE_SEND_DRY')
    live = Path(life_home).resolve() == LIVE_HOME.resolve()
    pretend = None if live else os.environ.get(FAKE_MAIL)      # never honoured in the live folder
    if not dry and not live and not pretend:
        raise SendFailed('this is not the live life-engine folder, and a sandbox never sends')
    _check_alert_hours(life_home)
    if dry:
        with open(dry, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'at': time.time(), 'tool': 'ZEUS_ALERT_EMAIL', 'account': ALERT_FROM, 'payload': payload}, ensure_ascii=False) + '\n')
        return {'dry': True, 'at': time.time(), 'fingerprint': mark}
    if live:
        data = (post or _alert_post)(payload)
        return {'at': time.time(), 'fingerprint': mark, 'message_id': data.get('message_id'), 'thread_id': data.get('thread_id')}
    try:
        p = (run or subprocess.run)([pretend, 'execute', 'ZEUS_ALERT_EMAIL', '-d', json.dumps(payload)], capture_output=True, text=True,
                                    timeout=SEND_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise SendUncertain('the mail service gave no answer within %d seconds' % SEND_TIMEOUT) from None
    except OSError as e:
        raise SendFailed('the mail tool could not be started (%s)' % type(e).__name__) from None
    try:
        data = json.loads(p.stdout)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get('successful') is True and not p.returncode:
        return {'at': time.time(), 'fingerprint': mark, 'message_id': data.get('message_id'), 'thread_id': data.get('thread_id')}
    if isinstance(data, dict) and data.get('successful') is False:
        raise SendFailed(str(data.get('error') or 'the mail service refused it')[:200])
    raise SendUncertain('the mail service answered in a way that could not be read')


def _alert_box() -> str:
    """The mailbox label of the alert inbox in settings.OWNER_EMAILS (default 'personal')."""
    for label, address in settings.OWNER_EMAILS.items():
        if address and ALERT_TO and address.lower() == ALERT_TO.lower():
            return label
    return 'personal'


def alert_arrived(token: str, life_home=None, *, search=None):
    """The destination check: is an email from Zeus's own inbox carrying this reference in the owner's alert inbox?
    One read-only search of that mailbox (the same read the engine already uses for the owner's sent mail).
    -> True (it is there) | False (the inbox was read and it is not there) | None (the inbox could not be read:
    never treated as "not there"). Outside the live life-engine folder the real inbox is never searched: without a
    stand-in `search` the answer is None."""
    token = re.sub(r'[^A-Za-z0-9]', '', str(token or ''))
    if not token:
        return None
    if search is None and (life_home is None or Path(life_home).resolve() != LIVE_HOME.resolve()):
        return None
    if not ALERT_FROM:
        return None
    query = {'user_id': 'me', 'query': f'from:{ALERT_FROM} subject:{token} newer_than:3d', 'max_results': 5, 'verbose': False}
    try:
        if search is not None:
            msgs = search(R.ACCOUNTS.get(_alert_box()), query)
        else:
            msgs = __import__('loops')._composio(R.ACCOUNTS.get(_alert_box()), 'GMAIL_FETCH_EMAILS', query).get('messages')
    except Exception:
        return None
    return bool(msgs) if isinstance(msgs, list) else None


# ---- the off-Mac deadline backup's scheduled drafts (rides urgent-fallback-on) ----
# Hard deadlines must reach the owner even if the Mac is off. An ESCALATION PATH, NOT AN EXECUTOR: a draft scheduled on
# AgentMail's servers (Zeus's own inbox, to the owner's alert inbox) sends itself at its send_at if the Mac has not
# deleted it first. Nothing here decides anything; urgentfallback.deadlines does.
# Service shapes: POST /v0/inboxes/{inbox}/drafts {to, subject, text, send_at} -> 200 with draft_id, send_status
# "scheduled", labels [draft, scheduled], send_at echoed as "YYYY-MM-DDTHH:MM:SSZ"; GET /drafts/{id} -> the draft;
# GET /drafts?labels=scheduled -> {count, drafts: [...]}; DELETE /drafts/{id} -> 204; GET after the delete -> 404
# not_found. A sent draft is deleted by the service, so a sent one also reads 404.
# Only those fields are sent (no custom labels): a draft is found again by the reference in its subject.
# A sandbox still needs a pretend service (BACKUP_API); without one it refuses.
BACKUP_SUBJECT = 'Zeus deadline:'                    # every backup draft's subject starts with this and ends with [its reference]
BACKUP_API = None                                    # a pretend drafts service (tests); never honoured in the live folder
LIVE_DRAFTS_OK = str(settings.get('mail.scheduled_drafts', True)).strip().lower() not in ('0', 'false', 'no', 'off')   # mail.scheduled_drafts; False blocks every live call


class _LiveDrafts:
    """Zeus's own inbox's drafts on AgentMail. Every call: a clear no raises SendFailed, no clear answer SendUncertain."""

    def _call(self, method: str, path: str, body=None):
        import urllib.error
        import urllib.parse
        import urllib.request
        if not LIVE_DRAFTS_OK or not ALERT_FROM:
            raise SendFailed('the scheduled-draft path is switched off or no agent inbox is configured: nothing is scheduled')
        try:
            key = _alert_key()
        except OSError:
            raise SendFailed("Zeus's own mail key file could not be read") from None
        url = f"{ALERT_API}/inboxes/{urllib.parse.quote(ALERT_FROM)}/drafts{path}"
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept': 'application/json'})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_a, **_k):
                return None
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=SEND_TIMEOUT) as resp:
                raw = resp.read().decode('utf-8', 'replace')
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                               # no such draft (deleted, or already sent)
            if 300 <= e.code < 500:
                raise SendFailed(f'the mail service refused it ({e.code})') from None
            raise SendUncertain(f'the mail service answered with an error ({e.code})') from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SendUncertain(f'the mail service gave no clear answer ({type(e).__name__})') from None

    def create(self, draft: dict) -> dict:
        got = self._call('POST', '', draft)
        if isinstance(got, dict) and got.get('draft_id'):
            return got
        raise SendUncertain('the mail service answered in a way that could not be read')

    def get(self, draft_id: str):
        import urllib.parse
        return self._call('GET', '/' + urllib.parse.quote(str(draft_id)))

    def delete(self, draft_id: str) -> str:
        import urllib.parse
        return 'gone' if self._call('DELETE', '/' + urllib.parse.quote(str(draft_id))) is None else 'deleted'

    def list(self, label: str) -> list:
        import urllib.parse
        got = self._call('GET', '?labels=' + urllib.parse.quote(label))     # the real query: labels=scheduled
        if isinstance(got, dict) and isinstance(got.get('drafts'), list):
            return got['drafts']
        raise SendUncertain('the draft list could not be read')


def drafts(life_home):
    """-> the drafts service for this folder: the real one only in the live life-engine folder, a pretend one in a
    sandbox (BACKUP_API). A sandbox without a pretend one gets SendFailed: it can never schedule anything."""
    if Path(life_home).resolve() == LIVE_HOME.resolve():
        return _LiveDrafts()
    if BACKUP_API is not None:
        return BACKUP_API
    raise SendFailed('this is not the live life-engine folder, and a sandbox never schedules anything')

"""Sources -> observations. Read-only.

email:    the owner's mailboxes (settings.OWNER_EMAILS), listed into a local mail list by intake/run_shadow.py and read
          through read-only Composio Gmail actions (accounts in `mail.composio_accounts`); bodies are fetched on demand.
          Obvious bulk is still passed through: the noticer decides. No mailbox configured = mail is off.
calendar: a calendar snapshot file (`calendar.snapshot_file`, JSON; shape in calendar_observations). An event is a new
          observation when it first appears or when its time/status/title changes. No file configured = no calendar.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import settings

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'intake'))
import run_shadow as R  # noqa: E402  (read_message, to_email, ACCOUNTS, the mail list: read-only helpers)
import life_intake as L  # noqa: E402

_CAL_FILE = settings.get('calendar.snapshot_file')
CALENDAR_CONFIGURED = bool(_CAL_FILE)
CAL_SNAPSHOT = Path(_CAL_FILE).expanduser() if _CAL_FILE else settings.LIFE_HOME / 'calendar-snapshot.json'
# The owner's own mailboxes (label -> address) and every address that is theirs. Used only to tell "addressed to the
# owner" from "the owner is only copied".
MAILBOX_ADDRESS = dict(settings.OWNER_EMAILS)
OWN_ADDRESSES = {a.lower() for a in MAILBOX_ADDRESS.values()} | set(settings.OWN_ADDRESSES)
MAIL_CONFIGURED = R.mail_configured()


def _owner_names():
    """The names the owner is greeted by, lower case: every part of the full name plus configured nicknames."""
    names = set()
    if settings.OWNER_NAME and settings.OWNER_NAME != 'the owner':
        names |= set(settings.OWNER_NAME.lower().split())
    if settings.OWNER_FIRST and settings.OWNER_FIRST != 'the owner':
        names.add(settings.OWNER_FIRST.lower())
    names |= {str(n).lower() for n in (settings.get('owner.nicknames', []) or []) if str(n).strip()}
    return frozenset(n.strip(".'-") for n in names if n.strip(".'-"))


HIS_NAMES = _owner_names()           # the owner's names (kept under this name for older callers)
_ADDRESS = re.compile(r"[a-z0-9][a-z0-9._%+'\-]*@[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}", re.I)
_SALUTE = r"(?:hi|hello|hey|dear|morning|good\s+(?:morning|afternoon|evening)|thanks|thank\s+you|ok|okay|great|perfect)"
_NAME = r"[a-z][a-z'.\-]*(?:\s+[a-z][a-z'.\-]*)?"
_GREETING_LINE = re.compile(rf"^\W*(?:{_SALUTE}\b[\s,!.]*)?(?P<names>{_NAME}(?:\s*(?:,|&|/|\+|\band\b)\s*{_NAME}){{0,3}})\s*[,:;!.\-\u2013\u2014]*\s*$", re.I)
_LEADS_WITH_HIM = re.compile(rf"^\W*(?:{_SALUTE}\b[\s,]*)?@?(?:{'|'.join(map(re.escape, sorted(HIS_NAMES))) or '(?!)'})\b\s*[,:\-\u2013\u2014]", re.I)


def same_mailbox(address: str) -> str:
    """One spelling per mailbox: lower case, no "+tag", and for Gmail no dots (Gmail delivers alex.rivera and
    alexrivera to the same person)."""
    local, _, domain = str(address or '').strip().lower().partition('@')
    local = local.split('+', 1)[0]
    if domain in ('gmail.com', 'googlemail.com'):
        local, domain = local.replace('.', ''), 'gmail.com'
    return f'{local}@{domain}' if local and domain else ''


def greets_him(text: str) -> bool:
    """Does the newest message open by speaking to the owner by name? ("Hi Alex,", "Alex -", "Jordan and Alex,",
    "Alex, can you sign this today?" for an owner named Alex). Looks at the first two lines only; a greeting to somebody
    else ends the look."""
    for line in [ln.strip() for ln in str(text or '').splitlines() if ln.strip()][:2]:
        m = _GREETING_LINE.match(line) if len(line) <= 60 else None
        if m:
            greeted = re.split(r"\s*(?:,|&|/|\+|\band\b)\s*", m.group('names').lower())
            return any(word.strip(".'-") in HIS_NAMES for name in greeted for word in name.split())
        if _LEADS_WITH_HIM.match(line[:80]):
            return True
    return False


def addressed(mailbox: str, to: str, cc: str, text: str = '') -> str:
    """Is this email addressed to the owner? 'to' (yes), 'cc' (the owner is only copied), or 'not_named' (a list, an
    alias or a blind copy). A thread between two other people that merely copies the owner is not theirs to act on.

    'to' is the answer whenever there is a sign it is theirs: one of their addresses is on the To line (compared as
    mailboxes, so the dotted spelling of a Gmail address counts), OR the newest message greets them by name (a reply-all
    often leaves the owner on the Cc line while the text says "Hi Alex, here are the answers to your questions"), OR
    neither line could be read. Not knowing is never a reason to drop the owner's mail."""
    own = {same_mailbox(a) for a in OWN_ADDRESSES | {MAILBOX_ADDRESS.get(mailbox, '')} if a}
    on = lambda line: any(same_mailbox(a) in own for a in _ADDRESS.findall(str(line or '')))
    if not str(to or '').strip() and not str(cc or '').strip():
        return 'to'
    return 'to' if on(to) or greets_him(text) else 'cc' if on(cc) else 'not_named'


def _fp(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


def email_candidates(days=2):
    """Recent mail from the mail list, oldest first (mailboxes are listed again first, at most once a minute).
    Mail not configured -> []."""
    if not MAIL_CONFIGURED:
        return []
    R.refresh_mail_list(days)
    return list(R.listed(days))


def inbox_feed():
    """Is the mail feed alive? -> {mailbox: time of its last successful listing} for the mailboxes this engine reads,
    from the mail list's own bookkeeping (its cursor table, read-only). A mailbox with no row comes back as None.
    Without this check a stopped listing would look like "no new mail": clean, empty ticks.
    Mail not configured -> {} (check MAIL_CONFIGURED: nothing to read is not a stale feed).
    Raises when the feed itself cannot be read; the caller records that as not readable, never as fresh."""
    if not MAIL_CONFIGURED:
        return {}
    checked = R.list_cursors()
    boxes = R.ACCOUNTS or checked
    return {box: checked.get(box) for box in boxes}


def sender_history(mailbox, sender, days=90, limit=30, *, exclude=None, errors=None):
    """Earlier mail from the same sender address in the same mailbox, oldest first, for the reflection step
    (reflect.py): [{'id', 'from', 'at', 'subject'}], 'at' as plain time on the owner's clock. Subjects and times only,
    no bodies. Read-only, from the mail list (the same database email_candidates reads). The list only reaches back as
    far as mail has been listed, so on a new install this covers less than `days`.
    exclude: a message id to leave out (the arrival itself). A failed read returns [] and, when `errors` is a list,
    adds one plain line to it. It never raises."""
    try:
        found = _ADDRESS.findall(str(sender or ''))
        address = same_mailbox(found[-1]) if found else ''
        if not address or not mailbox or not MAIL_CONFIGURED:
            return []
        from datetime import datetime
        rows = list(R.listed(days, mailbox))[::-1]          # newest first
        # "Earlier" means listed no later than the arrival itself (two mails from one sender can land in the same run).
        upto = next((float(r['at']) for r in rows if exclude is not None and str(r['message_id']) == str(exclude)), None)
        out = []
        for r in rows:
            theirs = _ADDRESS.findall(str(r['sender'] or ''))
            if not theirs or same_mailbox(theirs[-1]) != address or (exclude is not None and str(r['message_id']) == str(exclude)):
                continue
            if upto is not None and float(r['at']) > upto:
                continue
            at = datetime.fromtimestamp(float(r['at']), L.OWNER_TZ).strftime('%Y-%m-%d %I:%M%p').lower().replace(' 0', ' ')
            out.append({'id': str(r['message_id']), 'from': str(r['sender'] or '')[:120], 'at': at, 'subject': str(r['subject'] or '')[:200]})
            if len(out) >= int(limit):
                break
        return out[::-1]
    except Exception as e:
        if isinstance(errors, list):
            errors.append(f'earlier mail from this sender could not be read ({type(e).__name__}); the reflection went on without it')
        return []


# ---- the whole email: how much of the body is shown, and what its attachments say -------------------------------------------
# Advice must not rest on half an email: a body longer than BODY_SHOWN is flagged and kept whole in `full_text`, and
# every attachment is named, with the text of the readable ones. An attachment is read from the same read-only message
# fetch (format "raw", parsed here), never through a download link. Attachment files are untrusted: written under a fresh temp folder with a name of our own, converted to
# text by a fixed program with a time limit, never opened any other way, and removed.

BODY_SHOWN = 9000                    # the observation's `text`; the whole body is in `full_text` when it is longer
BODY_KEPT = 200000                   # the most of a body that is kept at all
ATTACH_MAX = 4                       # attachments looked at per email
ATTACH_BYTES_MAX = 10 * 1024 * 1024  # all attachments together; larger mail is listed and marked not read
ATTACH_TEXT_MAX = 20000              # characters of text kept per attachment
PDFTOTEXT = ('/opt/homebrew/bin/pdftotext', '/usr/local/bin/pdftotext', '/usr/bin/pdftotext')
TEXTUTIL = '/usr/bin/textutil'
_TEXT_NAMES = ('.txt', '.csv', '.ics', '.md', '.log', '.json', '.xml', '.vcf')
_WORD_NAMES = ('.doc', '.docx', '.rtf')


def _kind(name: str, mime: str) -> str:
    """'pdf' | 'text' | 'html' | 'word' | '' (a kind this engine does not read)."""
    name, mime = str(name or '').lower(), str(mime or '').lower()
    if mime == 'application/pdf' or name.endswith('.pdf'):
        return 'pdf'
    if mime == 'text/html' or name.endswith(('.html', '.htm')):
        return 'html'
    if mime.startswith('text/') or mime in ('application/ics', 'application/json', 'application/xml') or name.endswith(_TEXT_NAMES):
        return 'text'
    if mime in ('application/msword', 'application/rtf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') or name.endswith(_WORD_NAMES):
        return 'word'
    return ''


def attachment_list(m) -> list:
    """The real attachments of a fetched message: [{'name', 'type', 'size'}]. Pictures placed inside the body (a logo,
    a signature, a delivery photo: an image with a Content-ID) are not attachments in the sense that matters here."""
    out = []

    def walk(part):
        if not isinstance(part, dict):
            return
        name = str(part.get('filename') or '').strip()
        if name:
            head = {str(h.get('name') or '').lower(): str(h.get('value') or '') for h in part.get('headers') or []}
            mime = str(part.get('mimeType') or '').lower()
            inline_picture = mime.startswith('image/') and (head.get('content-id') or head.get('content-disposition', '').lower().startswith('inline'))
            if not inline_picture and not any(a['name'] == name for a in out):      # a calendar invite arrives twice (text/calendar and application/ics): one entry
                out.append({'name': name[:120], 'type': mime, 'size': int((part.get('body') or {}).get('size') or 0)})
        for q in part.get('parts') or []:
            walk(q)

    walk((m or {}).get('payload') if isinstance(m, dict) else None)
    return out


def raw_message(account, message_id):
    """The whole message as it arrived (bytes), from the same read-only fetch the body comes from. None = could not."""
    import base64
    try:
        p = subprocess.run([R.COMPOSIO, 'execute', 'GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID', '-d', json.dumps({'user_id': 'me', 'message_id': message_id, 'format': 'raw'}),
                            '--account', account], capture_output=True, text=True, timeout=90, env=R._env(), cwd=str(R.HOME), stdin=subprocess.DEVNULL)
        if p.returncode:
            return None
        data = json.loads(p.stdout)
        if isinstance(data, dict) and data.get('storedInFile') is True:
            path = Path(str(data.get('outputFilePath') or '')).resolve()
            if not path.is_relative_to(R.COMPOSIO_TMP.resolve()) or not path.is_file():
                return None
            try:
                data = json.loads(path.read_text())
            finally:
                path.unlink(missing_ok=True)   # never leave mail lying in a temp folder
        if not isinstance(data, dict) or data.get('successful') is False:
            return None
        raw = (data.get('data') or data).get('raw')
        return base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)) if raw else None
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return None


def _to_text(kind: str, blob: bytes) -> tuple:
    """One attachment's bytes -> (text, '') or ('', why it was not read). Never raises."""
    import shutil
    import tempfile
    if kind in ('text', 'html'):
        for enc in ('utf-8', 'utf-16', 'latin-1'):
            try:
                t = blob.decode(enc)
                break
            except UnicodeError:
                continue
        return (L.html_to_text(t) if kind == 'html' else t), ''
    tool = next((x for x in PDFTOTEXT if Path(x).exists()), None) if kind == 'pdf' else (TEXTUTIL if Path(TEXTUTIL).exists() else None)
    if tool is None:
        return '', 'the program that reads this kind of file is not on this computer'
    box = tempfile.mkdtemp(prefix='zeus-attach-')
    try:
        f = Path(box) / ('a.pdf' if kind == 'pdf' else 'a.doc')      # our own name: the sender's file name never reaches a command line
        f.write_bytes(blob)
        cmd = [tool, '-q', '-layout', str(f), '-'] if kind == 'pdf' else [tool, '-convert', 'txt', '-stdout', str(f)]
        p = subprocess.run(cmd, capture_output=True, timeout=25, cwd=box, env={'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}, stdin=subprocess.DEVNULL)
        if p.returncode:
            return '', 'it could not be turned into text (it may be damaged or locked with a password)'
        text = p.stdout.decode('utf-8', 'replace')
        return (text, '') if text.strip() else ('', 'it has no text in it (probably a scan or a picture)')
    except (OSError, subprocess.TimeoutExpired):
        return '', 'reading it took too long'
    finally:
        shutil.rmtree(box, ignore_errors=True)


def read_attachments(listed: list, raw) -> list:
    """`listed` (attachment_list) with what each one says: every entry gains 'read' (bool) and either 'text' or 'why'.
    `raw` is the whole message as bytes, or None when it could not be fetched. Never raises."""
    import email
    from email import policy
    out = [dict(a) for a in listed[:ATTACH_MAX]]
    for a in listed[ATTACH_MAX:]:
        out.append(dict(a, read=False, why=f'only the first {ATTACH_MAX} attachments of an email are read'))
    todo = []
    for a in out[:ATTACH_MAX]:
        if _kind(a['name'], a['type']):
            todo.append(a)
        else:
            a.update(read=False, why='this kind of file is not read (pictures, spreadsheets, archives and the like)')
    if not todo:
        return out
    if sum(a['size'] for a in listed) > ATTACH_BYTES_MAX:
        for a in todo:
            a.update(read=False, why='the attachments are too large to read here')
        return out
    if raw is None:
        for a in todo:
            a.update(read=False, why='the email could not be fetched again to read it')
        return out
    try:
        found = {}
        for part in email.message_from_bytes(raw, policy=policy.default).walk():
            name = part.get_filename()
            if name and name not in found:
                found[name] = (part.get_content_type(), part.get_payload(decode=True) or b'')
    except Exception:
        found = None
    for a in todo:
        if found is None or a['name'] not in found:
            a.update(read=False, why='it could not be found inside the email')
            continue
        mime, blob = found[a['name']]
        kind = _kind(a['name'], a['type']) or _kind(a['name'], mime)
        try:
            text, why = _to_text(kind, blob)
        except Exception:
            text, why = '', 'reading it failed'
        text = re.sub(r'[ \t]+\n', '\n', text).strip()
        if text:
            a.update(read=True, text=text[:ATTACH_TEXT_MAX], text_chars=len(text))
        else:
            a.update(read=False, why=why or 'it has no text in it')
    return out


def email_observation(row, attachments=False, account_override=None):
    """Full read of one listed email -> observation, or None if the read failed (retried next tick).
    With attachments=True the observation also says what its attachments are and what the readable ones say."""
    account = account_override or R.ACCOUNTS.get(row['mailbox'])
    if not account:
        return None
    m = R.read_message(account, row['message_id'])
    actual_id = (m.get('messageId') or m.get('id')) if isinstance(m,dict) else None
    if actual_id and str(actual_id)!=str(row['message_id']):
        return None
    e = R.to_email(row['mailbox'], row, m)
    body = L.html_to_text(e.get('body_text') or '')
    obs = {'source': 'email', 'id': f"email:{row['mailbox']}:{row['message_id']}", 'fp': _fp(row['message_id']),
           'when': e.get('date'), 'mailbox': row['mailbox'], 'from': e.get('from'), 'subject': e.get('subject'),
           'labels': e.get('labels'), 'text': body[:BODY_SHOWN], 'headers': e.get('headers'), 'body_chars': len(body)}
    if len(body) > BODY_SHOWN:
        obs['full_text'] = body[:BODY_KEPT]                # the part after the cut is never silently gone
    if attachments:
        try:
            listed = attachment_list(m)
            worth_fetching = any(_kind(a['name'], a['type']) for a in listed[:ATTACH_MAX]) and sum(a['size'] for a in listed) <= ATTACH_BYTES_MAX
            obs['attachments'] = read_attachments(listed, raw_message(account, row['message_id']) if worth_fetching else None) if listed else []
        except Exception:                                  # the email itself was read: a broken attachment step never loses it
            obs['attachments_unknown'] = True
    __import__('fresh').raw_links(obs['id'], e.get('body_text'))   # fresh.py (switch links-by-code): where this mail's own links point; never part of obs
    lines = {str(h.get('name') or '').lower(): str(h.get('value') or '') for h in ((m.get('payload') or {}).get('headers') or [])} if isinstance(m, dict) else {}
    to_line, cc_line = lines.get('to') or e.get('to') or '', lines.get('cc') or ''
    obs['to'], obs['cc'] = to_line[:400], cc_line[:400]
    obs['addressed'] = addressed(row['mailbox'], to_line, cc_line, body)     # from the whole lines, before the cut
    tid = m.get('threadId') if isinstance(m, dict) else None
    if tid:  # has the thread already moved on, and did the owner answer it themselves?
        obs['thread_id'] = tid
        tail = thread_tail(account, tid, row['message_id'])
        if tail is not None:
            obs['later_in_thread'] = [{k: v for k, v in t.items() if k != 'id'} for t in tail[-6:]]   # the noticer is never shown message ids
            obs['owner_already_replied'] = any(t.get('from_owner') for t in tail)
    return obs


def thread_tail(account, thread_id, message_id):
    """Messages in the same thread AFTER this one (read-only). None = could not tell (never treated as 'nothing')."""
    try:
        p = subprocess.run([R.COMPOSIO, 'execute', 'GMAIL_FETCH_MESSAGE_BY_THREAD_ID', '-d',
                            json.dumps({'user_id': 'me', 'thread_id': thread_id}), '--account', account],
                           capture_output=True, text=True, timeout=50, env=R._env(), cwd=str(R.HOME), stdin=subprocess.DEVNULL)
        if p.returncode:
            return None
        data = json.loads(p.stdout)
        if isinstance(data, dict) and data.get('storedInFile') is True:
            path = Path(str(data.get('outputFilePath') or '')).resolve()
            if not path.is_relative_to(R.COMPOSIO_TMP.resolve()) or not path.is_file():
                return None
            try:
                data = json.loads(path.read_text())
            finally:
                path.unlink(missing_ok=True)  # never leave email text lying in a temp folder
        if not isinstance(data, dict) or data.get('successful') is False:
            return None
        msgs = ((data.get('data') or {}).get('messages')) or []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    ids = [m.get('messageId') for m in msgs]
    if message_id not in ids:
        return None
    out = []
    for m in msgs[ids.index(message_id) + 1:]:
        pv = m.get('preview')
        pv = pv.get('body') if isinstance(pv, dict) else pv
        out.append({'from': str(m.get('sender') or '')[:80], 'at': m.get('messageTimestamp'),
                    'from_owner': 'SENT' in (m.get('labelIds') or []), 'preview': str(pv or '')[:240]})
        out[-1]['id'] = m.get('messageId')   # each later message's own id: fresh.py tells messages apart by it (never shown to a model)
    return out


def owner_replied_since(mailbox, message_id, thread_id=None):
    """True if the owner themselves sent a message in this thread after `message_id`. None = could not tell."""
    account = R.ACCOUNTS.get(mailbox)
    if not account:
        return None
    if not thread_id:
        try:
            m = R.read_message(account, message_id)
        except Exception:
            return None
        thread_id = m.get('threadId') if isinstance(m, dict) else None
    if not thread_id:
        return None
    tail = thread_tail(account, thread_id, message_id)
    return None if tail is None else any(t.get('from_owner') for t in tail)


def calendar_observations(now_iso=None):
    """The calendar snapshot -> (observations, meta). No snapshot file configured -> ([], ok with `configured` False).

    Snapshot shape (one JSON object, written by whatever reads the owner's calendars):
      {"complete": bool, "checked_at": iso, "window_start": iso, "window_end": iso, "errors": [..],
       "events": [{"key": str, "series_key": str|null, "instances": [..], "title": str, "start": iso, "end": iso,
                   "all_day": bool, "busy": bool, "personal": bool, "calendars": [str], "status": str,
                   "attendees": [..], "organizer": str|null, "location": str|null, "description": str|null}]}"""
    if not CALENDAR_CONFIGURED:
        return [], {'ok': True, 'configured': False, 'why': 'no calendar configured', 'checked_at': None, 'errors': None, 'window': [None, None]}
    try:
        snap = json.loads(CAL_SNAPSHOT.read_text())
    except (OSError, ValueError):
        return [], {'ok': False, 'why': 'calendar snapshot unreadable'}
    out = []
    for e in snap.get('events') or []:
        obs = {'source': 'calendar', 'id': f"calendar:{e.get('series_key') or e.get('key')}:{(e.get('start') or '')[:10]}",
               'title': e.get('title'), 'series_key':e.get('series_key'), 'instances':e.get('instances',[]), 'start': e.get('start'), 'end': e.get('end'), 'all_day': e.get('all_day'),
               'busy': e.get('busy'), 'personal': e.get('personal'), 'calendars': e.get('calendars'),
               'status': e.get('status'), 'attendees': e.get('attendees') or [], 'organizer': e.get('organizer'),
               'location': e.get('location'), 'description': (e.get('description') or '')[:1500]}
        obs['fp'] = _fp(obs['title'], obs['start'], obs['end'], obs['status'])
        if obs['all_day']:
            # An all-day entry is its dates: holiday entries can come back at a different UTC hour on each copy of the snapshot
            # and must not read as changed every time. The time-of-day print stays as fp_old so a copy stored with the
            # older print still reads as the same entry.
            obs['fp_old'], obs['fp'] = obs['fp'], _fp(obs['title'], str(obs['start'] or '')[:10], str(obs['end'] or '')[:10], obs['status'])
        out.append(obs)
    meta = {'ok': bool(snap.get('complete')), 'checked_at': snap.get('checked_at'), 'errors': snap.get('errors'),
            'window': [snap.get('window_start'), snap.get('window_end')]}
    return out, meta


def nearby(cal_obs, around_iso, hours=36):
    """Events near a time, for context (clashes, travel days, what else is going on)."""
    from datetime import datetime, timedelta, timezone

    def instant(value):
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # All-day dates and timezone-free values use the owner's time zone, as intake does.
        # Compare and sort UTC instants while retaining original timestamps in the context.
        return (t if t.tzinfo else t.replace(tzinfo=L.OWNER_TZ)).astimezone(timezone.utc)

    try:
        t = instant(around_iso)
    except (TypeError, ValueError):
        return []
    lo, hi = t - timedelta(hours=hours), t + timedelta(hours=hours)
    res = []
    for o in cal_obs:
        try:
            s = instant(o.get('start'))
        except (TypeError, ValueError):
            continue
        if lo <= s <= hi:
            res.append((s, {k: o.get(k) for k in ('title', 'start', 'end', 'all_day', 'busy', 'calendars', 'attendees')}))
    return [o for _, o in sorted(res, key=lambda x: x[0])[:25]]


# ---- the owner's Granola meeting notes, read-only (optional) ------------------------------------------------------------------
# Lets the engine catch things the owner promised in meetings. Off unless `meetings.granola` is true (the owner opts in).
# Route: a Composio Granola connection (the same command and sign-in the mail reads use; `meetings.granola_account`
# names the account when there is more than one). Only the two read actions below can be run from here; nothing in
# Granola is ever written.
GRANOLA_ON = str(settings.get('meetings.granola', False)).lower() in ('1', 'true', 'yes')
GRANOLA_ACCOUNT = settings.get('meetings.granola_account') or ''
GRANOLA_READS = ('GRANOLA_MCP_SEARCH_MEETINGS', 'GRANOLA_MCP_GET_MEETINGS')
_MEETING = re.compile(r'<meeting\s+id="([0-9a-f-]{36})"([^>]*)(?<!/)>(.*?)</meeting>', re.S)


def granola_read(op: str, args: dict, timeout: float = 30) -> str:
    """One read-only Granola action -> the service's answer as text. Raises when it could not be read (the caller says
    so in the bundle's coverage note; a failed read is never "no meetings")."""
    if not GRANOLA_ON:
        raise OSError('meeting notes are not turned on')
    if op not in GRANOLA_READS:
        raise ValueError('only the two read actions are allowed')
    p = subprocess.run([R.COMPOSIO, 'execute', op, '-d', json.dumps(args)] + (['--account', GRANOLA_ACCOUNT] if GRANOLA_ACCOUNT else []), capture_output=True, text=True, timeout=max(1, timeout),
                       env=R._env(), cwd=str(R.HOME), stdin=subprocess.DEVNULL)
    if p.returncode:
        raise OSError('the meeting notes service did not answer')
    data = json.loads(p.stdout)
    if isinstance(data, dict) and data.get('storedInFile') is True:
        path = Path(str(data.get('outputFilePath') or '')).resolve()
        if not path.is_relative_to(R.COMPOSIO_TMP.resolve()) or not path.is_file():
            raise ValueError('unexpected answer')
        try:
            data = json.loads(path.read_text())
        finally:
            path.unlink(missing_ok=True)       # never leave meeting notes lying in a temp folder
    text = (data.get('data') or {}).get('data') if isinstance(data, dict) and data.get('successful') is True else None
    if not isinstance(text, str):
        raise ValueError('the meeting notes service refused the read')
    return text


def _xml_attr(tag: str, name: str) -> str:
    import html
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return html.unescape(m.group(1)).strip() if m else ''


def _xml_inner(block: str, tag: str) -> str:
    import html
    m = re.search(rf'<{tag}(?:\s[^>]*)?>(.*?)</{tag}>', block, re.S)
    return html.unescape(m.group(1)).strip() if m else ''


def meetings_listed(since_day: str, until_day: str, timeout: float = 30) -> list:
    """The meetings the owner was in between two dates (their clock), newest first: [{'id', 'title', 'at'}]. Only meetings
    they attended or recorded are asked for (`attended_only`), so a note someone merely shared with them is never read as
    theirs. Meeting notes not turned on -> []."""
    if not GRANOLA_ON:
        return []
    text = granola_read('GRANOLA_MCP_SEARCH_MEETINGS', {'from_date': since_day, 'to_date': until_day, 'attended_only': True, 'limit': 50, 'max_participants': 8}, timeout)
    if '<meetings_data' not in text:
        raise ValueError('unexpected answer')              # an answer with no meetings still has this block, with count 0
    body = text.split('<meetings_data', 1)[1].split('</meetings_data>', 1)[0]
    out = [{'id': mid, 'title': _xml_attr(tag, 'title'), 'at': _xml_attr(tag, 'meeting_at')} for mid, tag, _ in _MEETING.findall(body)]
    return sorted(out, key=lambda m: m['at'], reverse=True)


def meetings_notes(ids: list, timeout: float = 30) -> dict:
    """The notes of up to ten meetings -> {id: {'title', 'at', 'people', 'his_notes', 'summary'}}. `his_notes` is what the
    owner typed themselves; `summary` is the note-taker's condensed write-up (not a transcript, and not the owner's exact
    words). Meeting notes not turned on -> {}."""
    if not GRANOLA_ON:
        return {}
    text = granola_read('GRANOLA_MCP_GET_MEETINGS', {'meeting_ids': list(ids)[:10]}, timeout)      # this read takes the ids only
    return {mid: {'title': _xml_attr(tag, 'title'), 'at': _xml_attr(tag, 'meeting_at'), 'people': ' '.join(_xml_inner(inner, 'known_participants').split()),
                  'his_notes': _xml_inner(inner, 'private_notes'), 'summary': _xml_inner(inner, 'summary')} for mid, tag, inner in _MEETING.findall(text)}

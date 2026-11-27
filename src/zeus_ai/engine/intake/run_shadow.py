"""Mail helpers for the life intake, and its shadow mode. Read-only. Never sends, never acts.

Mailboxes: the owner's mailboxes (labels from settings.OWNER_EMAILS), each read through a Composio Gmail connection
named in the config (`mail.composio_accounts`, label -> Composio account id). Only read actions are ever run.

Mail list: a small local database (MAIL_DB) of recent messages per mailbox (who, subject, when) plus, per mailbox, the
time of its last successful listing (`cursor`). refresh_mail_list() fills it; sources.py reads it for new mail, the
feed-alive check and sender history. With `mail.external_list = true` the database is written by some other tool
(same two tables, see LIST_SCHEMA) and this module only reads it.

Shadow mode (run as a script): only candidates that look actionable-automated by sender or subject get a full read.
Output: the life-event store + a JSON report.

Usage: python3 -I run_shadow.py [--days N] [--max N] [--dry]
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import life_intake as L  # noqa: E402  (also puts engine/ on sys.path)
import settings  # noqa: E402
from store import Store  # noqa: E402

HOME = settings.HOME
OUT = Path(os.environ.get('LIFE_INTAKE_HOME') or settings.get('paths.life_intake_home') or settings.PROFILE_DIR / 'workspace' / 'life')
MAIL_DB = Path(settings.get('mail.list_db') or settings.LIFE_HOME / 'mail-list.sqlite3')
EXTERNAL_LIST = str(settings.get('mail.external_list', False)).lower() in ('1', 'true', 'yes')
LIST_EVERY = float(settings.get('mail.list_every_seconds', 60))      # a mailbox is listed again at most this often
LIST_MAX = int(settings.get('mail.list_max', 100))                   # messages asked for per mailbox per listing
COMPOSIO = settings.COMPOSIO_BIN
COMPOSIO_TMP = Path(settings.get('composio.tmp_dir', '/private/tmp/composio'))   # where the Composio CLI parks large answers
OFF = OUT / 'intake-off'
# Mailbox label -> Composio account id, always by account id (never by nickname). Read-only use only.
ACCOUNTS = {str(k): str(v) for k, v in dict(settings.get('mail.composio_accounts', {}) or {}).items() if v}
CANDIDATE = re.compile(
    r'southwest|delta|united|alaskaair|jetblue|\baa\.com|american ?airlines|spirit|frontier|aeromexico|volaris|'
    r'cvs|walgreens|riteaid|pharmacy|prescription|\brx\b|express-scripts|optum|'
    r'calendar|invitation|invite|event|'
    r'marriott|hilton|hyatt|ihg|booking\.com|expedia|airbnb|mgm|caesars|wynn|hotel|reservation|'
    r'itinerary|confirmation|check[- ]?in|boarding|flight|trip|'
    r'payment|declined|past due|overdue|bill|invoice|due|'
    r'signature|delivery attempt|appointment', re.I)
LIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen(mailbox TEXT NOT NULL, message_id TEXT NOT NULL, sender TEXT, subject TEXT, verdict TEXT,
  reason TEXT, at REAL NOT NULL, PRIMARY KEY(mailbox, message_id));
CREATE TABLE IF NOT EXISTS cursor(mailbox TEXT PRIMARY KEY, ok_at REAL);
"""


class ReadFailed(Exception):
    pass


def mail_configured() -> bool:
    """Is there any mail to read? False = mail is off (no Composio accounts and no external list)."""
    return bool(ACCOUNTS) or EXTERNAL_LIST


def _env():
    env = {'HOME': str(HOME), 'PATH': f'{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}
    if os.environ.get('COMPOSIO_API_KEY'):
        env['COMPOSIO_API_KEY'] = os.environ['COMPOSIO_API_KEY']
    return env


def _composio(slug, args, account, timeout=50):
    """One read-only Composio Gmail action -> its data. Raises ReadFailed."""
    p = subprocess.run([COMPOSIO, 'execute', slug, '-d', json.dumps(args), '--account', account], capture_output=True, text=True,
                       timeout=timeout, env=_env(), cwd=str(HOME), stdin=subprocess.DEVNULL)
    if p.returncode:
        raise ReadFailed(f'composio rc {p.returncode}')
    data = json.loads(p.stdout)
    if isinstance(data, dict) and data.get('storedInFile') is True:
        path = Path(str(data.get('outputFilePath') or '')).resolve()
        if not path.is_relative_to(COMPOSIO_TMP.resolve()) or not path.is_file():
            raise ReadFailed('unexpected stored output')
        try:
            data = json.loads(path.read_text())
        finally:
            path.unlink(missing_ok=True)  # never leave email text lying in a temp folder
    if isinstance(data, dict) and data.get('successful') is False:
        raise ReadFailed(str(data.get('error'))[:120])
    return data.get('data', data) if isinstance(data, dict) else data


def read_message(account, message_id):
    return _composio('GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID', {'user_id': 'me', 'message_id': message_id, 'format': 'full'}, account)


# ---- the mail list ------------------------------------------------------------------------------------------------------------
def _list_db(write=False):
    if write:
        MAIL_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(MAIL_DB, timeout=10)
        con.executescript(LIST_SCHEMA)
        return con
    con = sqlite3.connect(MAIL_DB.as_uri() + '?mode=ro', uri=True, timeout=5)
    con.execute('PRAGMA query_only=ON')
    return con


def _epoch(m) -> float:
    """When a listed message arrived, as epoch seconds (its own timestamp; the listing time when it has none)."""
    for k in ('messageTimestamp', 'date'):
        v = m.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
    try:
        return int(m.get('internalDate')) / 1000.0
    except (TypeError, ValueError):
        return time.time()


def _listed_row(mailbox, m):
    heads = {str(h.get('name') or '').lower(): str(h.get('value') or '') for h in ((m.get('payload') or {}).get('headers') or [])}
    mid = m.get('messageId') or m.get('id')
    if not mid:
        return None
    return (mailbox, str(mid), str(m.get('sender') or heads.get('from') or '')[:300], str(m.get('subject') or heads.get('subject') or '')[:500],
            None, None, _epoch(m))


def refresh_mail_list(days=2, force=False) -> dict:
    """List recent mail in every configured mailbox into MAIL_DB -> {mailbox: 'ok' | 'skipped' | error text}.
    A mailbox is listed at most once per LIST_EVERY seconds unless `force`. A failed listing leaves the mailbox's
    cursor where it was, so the feed check sees it go stale instead of reading it as "no new mail". Never raises."""
    if EXTERNAL_LIST or not ACCOUNTS:
        return {}
    out = {}
    try:
        with closing(_list_db(write=True)) as con:
            cursors = {str(b): ok for b, ok in con.execute('SELECT mailbox, ok_at FROM cursor')}
    except sqlite3.Error as e:
        return {box: f'mail list not writable ({type(e).__name__})' for box in ACCOUNTS}
    for box, account in ACCOUNTS.items():
        if not force and cursors.get(box) and time.time() - float(cursors[box]) < LIST_EVERY:
            out[box] = 'skipped'
            continue
        try:
            data = _composio('GMAIL_FETCH_EMAILS', {'user_id': 'me', 'query': f'newer_than:{max(1, math.ceil(float(days)))}d',
                                                    'max_results': LIST_MAX}, account, timeout=60)
            msgs = (data.get('messages') if isinstance(data, dict) else data) or []
            rows = [r for r in (_listed_row(box, m) for m in msgs if isinstance(m, dict)) if r]
            with closing(_list_db(write=True)) as con:
                con.executemany('INSERT OR IGNORE INTO seen VALUES(?,?,?,?,?,?,?)', rows)
                con.execute('INSERT OR REPLACE INTO cursor VALUES(?,?)', (box, time.time()))
                con.commit()
            out[box] = 'ok'
        except (ReadFailed, ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as e:
            out[box] = f'listing failed ({type(e).__name__})'
    return out


def listed(days, mailbox=None):
    """Rows of the mail list since `days` ago (oldest first): mailbox, message_id, sender, subject, verdict, reason, at.
    Raises when the list cannot be read."""
    since = time.time() - float(days) * 86400
    with closing(_list_db()) as con:
        con.row_factory = sqlite3.Row
        q = 'SELECT mailbox,message_id,sender,subject,verdict,reason,at FROM seen WHERE at>=?' + (' AND mailbox=?' if mailbox else '') + ' ORDER BY at'
        return con.execute(q, (since, mailbox) if mailbox else (since,)).fetchall()


def list_cursors() -> dict:
    """{mailbox: time of its last successful listing}. Raises when the list cannot be read."""
    with closing(_list_db()) as con:
        return {str(box): ok_at for box, ok_at in con.execute('SELECT mailbox, ok_at FROM cursor')}


# ---- one message ---------------------------------------------------------------------------------------------------------------
def to_email(mailbox, row, m):
    headers = {}
    for h in ((m.get('payload') or {}).get('headers') or []):
        if h.get('name'):
            headers[h['name']] = h.get('value') or ''
    labels = m.get('labelIds') or []
    if isinstance(labels, str):
        labels = re.findall(r'[A-Z_a-z0-9]+', labels)
    return {'message_id': row['message_id'], 'mailbox': mailbox, 'from': headers.get('From') or row['sender'] or '',
            'to': headers.get('To') or '', 'subject': headers.get('Subject') or row['subject'] or '',
            'date': headers.get('Date') and _rfc(headers['Date']), 'body_text': str(m.get('messageText') or ''),
            'labels': labels, 'headers': {k: v for k, v in headers.items() if k in ('List-Unsubscribe', 'List-Id', 'Content-Type', 'Precedence')}}


def _rfc(v):
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(v).isoformat()
    except (TypeError, ValueError):
        return None


def candidates(days):
    refresh_mail_list(days)
    return [r for r in listed(days) if CANDIDATE.search(f"{r['sender'] or ''} {r['subject'] or ''}")]


def main():
    args = sys.argv[1:]
    days = float(args[args.index('--days') + 1]) if '--days' in args else 3
    cap = int(args[args.index('--max') + 1]) if '--max' in args else 40
    dry = '--dry' in args
    OUT.mkdir(parents=True, exist_ok=True)
    if OFF.exists():
        print(json.dumps({'status': 'off', 'switch': str(OFF)}))
        return
    if not mail_configured():
        print(json.dumps({'status': 'off', 'why': 'no mailbox is configured (mail.composio_accounts)'}))
        return
    st = Store(OUT / 'events.sqlite3')
    report = {'started': time.time(), 'mode': 'shadow', 'sends': 0, 'actions': 0, 'read': 0, 'skipped_seen': 0,
              'read_failed': 0, 'results': []}
    for row in candidates(days):
        mailbox = row['mailbox']
        if st.seen(mailbox, row['message_id']):
            report['skipped_seen'] += 1
            continue
        if report['read'] >= cap:
            break
        account = ACCOUNTS.get(mailbox)
        if not account:
            continue
        if dry:
            report['results'].append({'mailbox': mailbox, 'subject': row['subject'], 'would_read': True})
            continue
        try:
            m = read_message(account, row['message_id'])
        except (ReadFailed, ValueError, subprocess.TimeoutExpired) as e:
            report['read_failed'] += 1
            report['results'].append({'mailbox': mailbox, 'message_id': row['message_id'], 'error': str(e)[:120]})
            continue  # not marked: a failed read is never "nothing there"; retried next run
        report['read'] += 1
        email = to_email(mailbox, row, m)
        r = L.process(email)
        change = st.upsert(r.get('key'), r['event'], row['message_id'], mailbox) if r['class'] == 'actionable_automated' else None
        st.mark(mailbox, row['message_id'], r)
        report['results'].append({'mailbox': mailbox, 'subject': (row['subject'] or '')[:90], 'list_verdict': row['verdict'],
                                  'class': r['class'], 'type': r.get('event_type'), 'key': r.get('key'), 'change': change,
                                  'why': r['why']})
    report['events'] = [{'key': e['_key'], 'type': e['type'], 'status': e.get('_status'), 'flags': e['_flags']} for e in st.all()]
    report['finished'] = time.time()
    (OUT / 'last-run.json').write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: report[k] for k in ('mode', 'read', 'skipped_seen', 'read_failed', 'sends', 'actions')} |
                     {'events': len(report['events'])}))


if __name__ == '__main__':
    main()

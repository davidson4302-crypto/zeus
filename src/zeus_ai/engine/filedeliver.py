"""filedeliver: a file background work prepared reaches the owner's phone as an attachment.

Why: a background job's result is one JSON object of words, so without this module it has nowhere to hand a file
back, nothing records a file as prepared, artifact.py has no verified route (`delivery-routes.json`), and the note
goes out as words only: a prepared file would stay "prepared, delivery pending" for ever.

What this adds, only with the switch file `file-delivery-on` in the life folder (without it nothing here is loaded, no
prompt gains a word and no file is written):

  job_line()            one sentence at the end of the background job's instructions: a file for the owner goes in the job's
                        own scratch folder and is named in "file" of its answer.
  harvest(...)          a finished, clean job's "file" is checked by code (where it is, what it is, how big; the file is
                        untrusted data and is never opened as instructions) and recorded as PREPARED with artifact.py.
  line_for(...)         the notifier's ONE send carries the file: the note's own send command (Zeus's line: `hermes send`
                        to the owner's iMessage) is given `MEDIA:<path>` after the note's words, in the same call. That is the
                        Photon adapter's own file send (`/send-attachment`). No second sender, no second message path:
                        every quiet-hour rule, cap and check that held the note holds the file, because it IS the note's
                        send. What the line records about the note (its words, its receipt, its read-back) is untouched.
  tend(...)             every tick, after the notifier. Sends nothing. Reads Hermes's send journal (read-only) for the
                        file's own row (by the file's sha256): the provider took it = "sent, not confirmed on the owner's phone
                        yet"; the phone's own delivery or read receipt = DELIVERED (artifact.mark_delivered, with the
                        receipt). The first real phone receipt is also what makes the route verified
                        (`delivery-routes.json`): it is written from evidence, never by hand and never by a guess.
                        A file that did not go through is told ONCE, as one ordinary item for the next note.
  words(d, life)        what the note writer may say about a file: "attached to this message", "sent, not confirmed yet",
                        or why it did not go. Never "delivered" without the phone's receipt.

Honest states of one file (artifact.py's three facts stay separate):
  prepared, delivery pending          checked and on record; not sent (with the reason when there is one)
  sent, not confirmed on the phone    the provider accepted the attachment (a message id), nothing more
  delivered                           the phone's delivery or read receipt is in the send journal for this exact file
A file is never sent twice: once a send was tried and its outcome is "sent" or cannot be told, it is not tried again.

Limits (said, not hidden): PDFs, images and short text files, up to 20 MB, from the job's scratch folders only, with a
plain path (no spaces or quotes). A file whose item the note writer judged not worth a text is not sent; it stays
prepared. Tests never text the owner, so a real delivery is only ever proven by the phone's own receipt.
Standard library only. No model is asked. Never raises into the tick or the notifier.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402

SWITCH = 'file-delivery-on'
ROUTE = 'imessage-attachment'
DESTINATION = "the owner's iMessage, on Zeus's own line"
CARRIES = ('pdf', 'image', 'text')
MAX_BYTES = 20 * 1024 * 1024
MAX_PER_JOB = 2                  # files one job may hand back
MAX_PER_NOTE = 4                 # attachments one note carries
INDEX = 'file-delivery.json'
WATCH_FOR = 48 * 3600            # a sent file's row is read this long for the phone's receipt
SETTLE_AFTER = 10 * 60           # = the line's own GRACE: a send with no journal row this long after it ended did not go
LIVE_LIFE = settings.LIFE_HOME
LIVE_JOURNAL = settings.STATE_DB
JOB_FOLDER = str(settings.CACHE_DIR / 'documents') + '/'   # the job's scratch, and a folder the line may attach from
SCRATCH = (str(settings.CACHE_DIR) + '/', '/private/tmp/', '/tmp/', '/var/folders/')   # = jobaudit.SCRATCH
SAFE_PATH = re.compile(r'^/[A-Za-z0-9._/+@=~-]+$')      # a path that reads as one `MEDIA:` tag, whatever follows it
ARRIVED = ('delivered', 'read')
TAKEN = ('accepted', 'sent') + ARRIVED
REFUSED = ('failed', 'cancelled')
FINAL = ('delivered', 'failed', 'refused', 'not_sent')
ATTACHED = 'ready; it is attached to this message'
SENT = 'sent as an attachment; the phone has not confirmed it yet'
DELIVERED_KEPT = 'delivered to the phone as an attachment (the phone confirmed it)'
CLOSED_RECORD = 'the item was already closed, so its record could not take the mark; it is kept in file-delivery.json'
WRITER_RULE = ('\n- When "where_it_stands" says the file is attached to this message, say it is attached, in a few words, and nothing more about '
               'getting it to them. When it says it was sent and the phone has not confirmed it, do not call it delivered.')
JOB_LINE = ('\nA file for the owner: when the useful result is a file they should have on their phone (a PDF, an image or a short text file), save it in your '
            'own scratch folder ' + JOB_FOLDER + ' under a plain name with no spaces, and add "file": {"path": "<its full path>", "kind": "pdf" or '
            '"image" or "text", "name": "<what to call it, in a few words>"} to the JSON object. Your pipes check the file and attach it to '
            'the note themselves. Never put a path in the headline or the details, and never send the file yourself.\n')


def _life(life=None) -> Path:
    return Path(life or os.environ.get('LIFE_HOME') or LIVE_LIFE)


def on(life=None) -> bool:
    try:
        return (_life(life) / SWITCH).exists()
    except Exception:
        return False


def job_line(life=None) -> str:
    """worker.build_prompt: the one sentence, or '' without the switch."""
    return JOB_LINE if on(life) else ''


# ---- the index: what was tried for which file (the ledger keeps the facts; this keeps the send attempts) ---------------

def _index(life) -> dict:
    try:
        d = json.loads((_life(life) / INDEX).read_text())
        return d if isinstance(d, dict) and isinstance(d.get('files'), dict) else {'files': {}}
    except (OSError, ValueError, TypeError):
        return {'files': {}}


def _save_index(life, d: dict) -> None:
    p = _life(life) / INDEX
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(d, indent=1, default=str))
    tmp.replace(p)


def _iso(t=None) -> str:
    try:
        import artifact as AF
        return AF._iso(t if isinstance(t, datetime) else None)
    except Exception:
        return datetime.now().astimezone().isoformat(timespec='seconds')


# ---- may this file go? -------------------------------------------------------------------------------------------------

def _within_scratch(path: str) -> bool:
    try:
        real = os.path.realpath(path)
        return any(real.startswith(os.path.realpath(root).rstrip('/') + '/') for root in SCRATCH)
    except (OSError, ValueError):
        return False


def may_carry(d: dict) -> tuple:
    """Can this prepared file go as an attachment right now? -> (True, '') or (False, the plain reason).
    Code decides: the kind, the size, where the file is, that its path is plain, and that it is still the very file
    that was checked (same sha256)."""
    try:
        import artifact as AF
        kind, path = str(d.get('kind') or ''), str(d.get('path') or '')
        if kind not in CARRIES:
            return False, f'a {kind or "file of this kind"} is not something the texting line is set to carry (PDFs, images and text files are)'
        if not SAFE_PATH.match(path) or '..' in path:
            return False, 'its name or folder has a space or an odd character in it, so it cannot be attached as it is'
        if not _within_scratch(path) or os.path.islink(path):
            return False, 'it is not in the folder background work may hand files over from'
        if int(d.get('size') or 0) > MAX_BYTES:
            return False, f'it is too large to attach ({round(int(d.get("size") or 0) / 1048576)} MB; the limit is {MAX_BYTES // 1048576} MB)'
        again = AF.check(path, kind)
        if not again['ok']:
            return False, 'the file is no longer as it was when it was checked: ' + str(again['why'])
        if again['sha256'] != d.get('sha256'):
            return False, 'the file changed after it was checked, so it was not sent'
        if again['size'] > MAX_BYTES:
            return False, f'it is too large to attach (the limit is {MAX_BYTES // 1048576} MB)'
        return True, ''
    except Exception as e:
        return False, f'it could not be checked ({type(e).__name__})'


def words(d: dict, life=None):
    """artifact.state: where this file stands in words the note writer may use -> {'words', 'reason'} or None (not ours:
    not prepared, or already delivered)."""
    try:
        if not d.get('prepared') or d.get('delivered'):
            return None
        import artifact as AF
        got = _index(life)['files'].get(str(d.get('sha256'))) or {}
        state = got.get('state') or ('sent' if d.get('sent') else '')
        if state == 'delivered' and got.get('recorded_delivered') and got.get('receipt'):
            return {'words': DELIVERED_KEPT, 'reason': '', 'delivered': True}         # the phone's receipt is in the send journal; the item's record was closed
        if state in ('sent', 'uncertain', 'delivered'):
            why = '' if state != 'uncertain' else 'it went out with an earlier message, and it is not known yet whether it got through'
            return {'words': SENT if state != 'uncertain' else AF.PENDING, 'reason': why}
        if state in ('failed', 'refused', 'not_sent'):
            return {'words': AF.PENDING, 'reason': 'it did not go through as an attachment: ' + str(got.get('why') or 'the reason was not recorded')}
        ok, why = may_carry(d)
        return {'words': ATTACHED, 'reason': ''} if ok else {'words': AF.PENDING, 'reason': why}
    except Exception:
        return None


# ---- worker: a job hands a file back -------------------------------------------------------------------------------------

def harvest(con, life, key: str, text, broke, now_dt, res: dict) -> None:
    """worker._harvest_one, after the result is saved. A finished job's answer may carry "file" (or "files"). Code checks
    each one and records it as PREPARED (artifact.prepared: exists, not empty, opens as what it claims, sha256). A job
    that broke a limit or could not be audited, or whose item is closed, hands back nothing. What the file SAYS is never
    read as an instruction by anything here. Never raises."""
    try:
        if not on(life):
            return
        raw = str(text or '')
        a, b = raw.find('{'), raw.rfind('}')
        try:
            obj = json.loads(raw[a:b + 1]) if a >= 0 and b > a else {}
        except ValueError:
            obj = {}
        asked = obj.get('files') if isinstance(obj, dict) and isinstance(obj.get('files'), list) else [obj.get('file')] if isinstance(obj, dict) else []
        asked = [x for x in asked if isinstance(x, dict) and x.get('path')]
        if not asked:
            return
        note = res.setdefault('files', {'prepared': 0, 'refused': []})
        row = con.execute('SELECT state,payload FROM opps WHERE key=?', (key,)).fetchone()
        if row is None:
            return
        opp = json.loads(tuple(row)[1])
        if broke is None or broke or (opp.get('exec') or {}).get('broke_a_limit') or tuple(row)[0] not in ('ready', 'surfaced') or opp.get('suspicious_mail'):
            note['refused'].append('the job was not clean, or nothing is waiting to be told any more')
            return
        import artifact as AF
        for spec in asked[:MAX_PER_JOB]:
            path, kind = str(spec.get('path') or ''), str(spec.get('kind') or '').lower()
            shown = {'kind': kind, 'path': path, 'size': 0}
            if kind not in CARRIES or not SAFE_PATH.match(path) or '..' in path or not _within_scratch(path) or os.path.islink(path):
                note['refused'].append(may_carry(shown)[1][:200] if kind in CARRIES else f'not a kind of file this carries ("{kind[:20]}")')
                continue
            try:
                if os.path.getsize(path) > MAX_BYTES:
                    note['refused'].append(f'too large to attach (the limit is {MAX_BYTES // 1048576} MB)')
                    continue
            except OSError:
                note['refused'].append('the file is not there')
                continue
            got = AF.prepared(con, key, path, kind, now_dt, name=str(spec.get('name') or '')[:120])
            if got.get('recorded'):
                note['prepared'] += 1
            else:
                note['refused'].append(str(got.get('why') or 'the file did not pass its check')[:200])
    except Exception as e:
        res.setdefault('files', {}).setdefault('errors', []).append(f'harvest: {type(e).__name__}: {str(e)[:120]}')


# ---- notifier: the note's one send carries the file ----------------------------------------------------------------------

def _eligible(con, life, carried: list) -> tuple:
    """The prepared, not yet tried files of the items this note carries -> (those that go, those that cannot: [(row, why)])."""
    import artifact as AF
    tried = _index(life)['files']
    go, stay = [], []
    for key in carried:
        for d in AF.deliverables(con, key):
            sha = str(d.get('sha256') or '')
            if not d.get('prepared') or d.get('delivered') or d.get('sent') or not sha or sha in tried or any(x['sha256'] == sha for x in go):
                continue
            row = {'opp': str(key), 'sha256': sha, 'path': str(d.get('path')), 'kind': str(d.get('kind')), 'name': str(d.get('name') or 'the file')}
            ok, why = may_carry(d)
            if ok and len(go) >= MAX_PER_NOTE:
                ok, why = False, 'one message carries a few files at most, and this one was over that'
            (go if ok else stay).append(row if ok else (row, why))
    return go, stay


class _Line:
    """Zeus's own line, with this note's files riding its one send. Everything else is the line itself."""

    def __init__(self, line, files: list, life, out: dict):
        self._line, self._files, self._life, self._out = line, list(files), life, out

    def __getattr__(self, name):
        return getattr(self._line, name)

    def send(self, text):
        line, seen = self._line, {}
        inner = line.runner
        clock = getattr(line, 'clock', None) or time.time      # the line's own clock: the send journal is read around these times
        tail = ''.join('\nMEDIA:' + f['path'] for f in self._files)

        def runner(argv, body, env, timeout, cwd):
            seen['started'] = clock()
            try:
                got = inner(argv, str(body) + '\n' + tail, env, timeout, cwd)
            finally:
                seen['ended'] = clock()
            seen['rc'], seen['out'] = got[0], got[1]
            return got

        line.runner = runner
        try:
            result = line.send(text)
        finally:
            line.runner = inner
        try:
            _record(self._life, line, self._files, seen, result, self._out)
        except Exception as e:
            self._out.setdefault('files', {}).setdefault('errors', []).append(f'record: {type(e).__name__}: {str(e)[:120]}')
        return result


def line_for(con, life, line, carried: list, now_dt, out: dict):
    """notifier.deliver, right before the note's one send -> the line to send with. With the switch and a prepared file
    on an item this note carries: the same line, whose send also carries the file(s). Otherwise, and on any trouble: the
    line it was given. Writes nothing in the ledger."""
    try:
        if not on(life) or line is None or not callable(getattr(line, 'runner', None)) or getattr(line, 'dry', False):
            return line
        go, stay = _eligible(con, life, carried)
        if stay:                       # a file that cannot go is written down with its reason; tend() tells the owner once
            d = _index(life)
            for row, why in stay:
                d['files'][row['sha256']] = dict(row, state='not_sent', why=why, at=now_dt.timestamp() if isinstance(now_dt, datetime) else time.time())
            _save_index(life, d)
        if not go:
            return line
        out.setdefault('files', {})['attached'] = len(go)
        return _Line(line, go, life, out)
    except Exception as e:
        out.setdefault('files', {}).setdefault('errors', []).append(f'attach: {type(e).__name__}: {str(e)[:120]}')
        return line


def _journal_path(line=None) -> Path:
    env = os.environ.get('LIFE_SEND_JOURNAL')
    if env:
        return Path(env)
    p = getattr(getattr(line, 'journal', None), 'path', None) if line is not None else None
    return Path(p) if p else LIVE_JOURNAL


def journal_rows(path, sha256: str, since: float) -> list:
    """Hermes's send journal, read-only: the attempts for this exact file since `since`, oldest first. Raises when the
    journal cannot be read (the caller treats that as "cannot tell")."""
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        got = db.execute('SELECT attempt_id, kind, state, provider_id, error_json, created_at, updated_at, artifact_path, route_json FROM outbound_attempts '
                         'WHERE artifact_sha256=? AND created_at>=? ORDER BY created_at', (sha256, float(since))).fetchall()
    out = []
    for r in got:
        try:
            route = json.loads(r['route_json'] or '{}')
        except ValueError:
            route = {}
        if route.get('runtime_profile') not in (None, settings.PROFILE):
            continue
        out.append(dict(r))
    return out


def _read_state(rows: list) -> dict:
    """The newest attempt's state in our words -> {'state': 'sent' | 'delivered' | 'failed' | 'uncertain', ...}."""
    r = rows[-1]
    st = str(r.get('state') or '')
    base = {'provider_id': r.get('provider_id'), 'journal_state': st, 'attempt_id': r.get('attempt_id')}
    if st in ARRIVED:
        return base | {'state': 'delivered', 'why': ''}
    if st in TAKEN:
        return base | {'state': 'sent', 'why': ''}
    if st in REFUSED:
        try:
            err = json.loads(r.get('error_json') or '{}')
        except ValueError:
            err = {}
        return base | {'state': 'failed', 'why': 'the texting service refused the file' + (f" ({str(err.get('type') or err.get('error') or '')[:60]})" if err else '')}
    return base | {'state': 'uncertain', 'why': 'the send was started and its outcome is not known yet'}


def _record(life, line, files: list, seen: dict, result, out: dict) -> None:
    """Right after the note's send returned: what happened to each file, from the send journal first (the file's own row),
    then from what the send command printed. Written to the index only. Nothing is sent again, whatever it says."""
    started = float(seen.get('started') or 0)
    if not started:
        return                                          # the command never started (before 9am, no target): nothing was tried, the file goes with the item's next note
    try:
        printed = json.loads(seen.get('out') or '') if str(seen.get('out') or '').strip() else {}
    except ValueError:
        printed = {}
    printed = printed if isinstance(printed, dict) else {}
    dropped = {os.path.realpath(str(x.get('path') or x.get('media_path') or '')): str(x.get('reason') or x.get('error') or 'the sender would not attach it')
               for x in printed.get('media_dropped') or [] if isinstance(x, dict)}
    kind = (result or {}).get('kind') if isinstance(result, dict) else None
    d = _index(life)
    for f in files:
        row = dict(f, at=float(seen.get('ended') or started), note_kind=kind)
        try:
            rows = journal_rows(_journal_path(line), f['sha256'], started - 5)
            readable = True
        except Exception:
            rows, readable = [], False
        if rows:
            row.update(_read_state(rows))
        elif os.path.realpath(f['path']) in dropped or (printed.get('media_dropped') and not dropped):
            row.update(state='refused', why='the sender would not attach it: ' + (dropped.get(os.path.realpath(f['path'])) or 'no reason given')[:160])
        elif not readable and seen.get('rc') == 0 and printed.get('success') and printed.get('message_id') and not printed.get('media_dropped'):
            row.update(state='sent', provider_id=printed.get('message_id'), why='', journal_state='not read')
        elif kind == 'failed' and readable:
            continue                                    # the note itself provably did not go, and no attempt was made for the file: it goes with the note's next try
        elif kind == 'sent' and readable and seen.get('rc') is not None:
            row.update(state='not_sent', why='the note went, but the sender made no attempt to attach the file')
        else:
            row.update(state='uncertain', why='the send ended without a clear answer, so it is not known whether the file went')
        d['files'][f['sha256']] = row
        out.setdefault('files', {}).setdefault('outcomes', []).append(row['state'])
    _save_index(life, d)


# ---- tick: receipts, the record, and the one line when a file did not go ---------------------------------------------------

def _verify_route(life, kind: str, proof: dict, now_dt) -> None:
    """The first real phone receipt for a file makes the route verified. Written from that evidence only."""
    p = _life(life) / 'delivery-routes.json'
    try:
        d = json.loads(p.read_text())
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    routes = d.setdefault('routes', {}) if isinstance(d.get('routes', {}), dict) else {}
    d['routes'] = routes
    r = routes.get(ROUTE) if isinstance(routes.get(ROUTE), dict) else {}
    carries = sorted(set(r.get('carries') or []) | {kind})
    if r.get('verified_at') and carries == sorted(r.get('carries') or []):
        return
    routes[ROUTE] = {'carries': carries, 'verified_at': r.get('verified_at') or _iso(now_dt), 'how': 'the phone\'s own delivery receipt for a file sent on Zeus\'s line',
                     'proof': r.get('proof') or proof}
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(d, indent=1, default=str))
    tmp.replace(p)


def _on_record(con, opp_key, sha256: str, mark: str) -> bool:
    """Did the mark really land on the item's record? artifact.deliverables reads the commitment when the item has one,
    but artifact._write writes the item's own list once that commitment is closed (the note closes most items), so a
    write can find nothing and say nothing. Read it back instead of trusting the write."""
    try:
        import artifact as AF
        x = AF._find(AF.deliverables(con, opp_key), sha256)
        return bool(x and x.get(mark))
    except Exception:
        return False


def _tell(con, row: dict, now_dt) -> bool:
    """ONE ordinary finished item for the next note: the file did not go through. Never a second one: the key is the guard."""
    key = f"file:{row['opp']}:{row['sha256'][:8]}"
    if con.execute('SELECT 1 FROM opps WHERE key=?', (key,)).fetchone():
        return False
    t = time.time()
    step = 'Tell the owner in one short line.'
    headline = f"The file \"{row.get('name') or 'the file'}\" did not reach your phone as an attachment."
    details = f"Why: {row.get('why') or 'the reason was not recorded'}. The file itself is ready and unchanged. It was not sent a second time."
    payload = {'title': f"A file that did not go through: {row.get('name') or 'the file'}", 'why': 'A file prepared for the owner did not go out with its note.', 'urgency': 'today',
               'dedupe_key': key, 'confidence': 1.0, 'owner_involved': 'yes', 'action_class': 'notify_owner_now', 'proposed_action': step,
               'steps': [{'do': step, 'action_class': 'notify_owner_now'}], 'parent': row['opp'],
               'decision': {'autonomy': 'auto', 'surface_at': now_dt.isoformat(), 'channel': 'imessage', 'reason': 'a file did not go through, told once'},
               '_source': 'files', 'ready_at': t,
               'packet': {'status': 'done', 'headline': headline, 'details': details, 'decision': None, 'followups': [], 'learned': []}}
    con.execute('INSERT OR IGNORE INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)', (key, 'ready', 'auto', now_dt.isoformat(), 'today', 'notify_owner_now',
                                                                          json.dumps(payload, default=str), '[]', t, t))
    con.commit()
    return True


def tend(con, life, now_dt, live: bool = True, journal=None):
    """Every tick, after the notifier. Sends nothing. -> counts, or None when there was nothing to do.
    1. What the send recorded about a file is written on the item's own record (`sent`: when, the message id).
    2. A sent file is looked up in the send journal: the phone's receipt makes it delivered (and the route verified);
       a refusal makes it failed; a send nobody can account for after ten minutes did not go.
    3. A file that did not go is told once, as one ordinary item."""
    if not on(life):
        return None
    d = _index(life)
    if not d['files']:
        return None
    import artifact as AF
    res, changed = {'sent': 0, 'delivered': 0, 'failed': 0, 'told': 0, 'watching': 0, 'kept_here': 0}, False
    now = now_dt.timestamp() if isinstance(now_dt, datetime) else time.time()
    path = Path(journal) if journal else _journal_path()
    for sha, row in list(d['files'].items()):
        try:
            state = row.get('state')
            if state in ('sent', 'uncertain') and now - float(row.get('at') or now) <= WATCH_FOR:
                try:
                    rows = journal_rows(path, sha, float(row.get('at') or now) - 120)
                except Exception:
                    rows = None
                if rows:
                    got = _read_state(rows)
                    if got['state'] != state or got.get('provider_id') != row.get('provider_id'):
                        row.update(got)
                        row.pop('synced', None)
                        state, changed = got['state'], True
                elif rows == [] and state == 'uncertain' and now - float(row.get('at') or now) > SETTLE_AFTER:
                    row.update(state='not_sent', why='no attempt to send the file is on record ten minutes after the note went')
                    state, changed = 'not_sent', True
            if state in ('sent', 'delivered') and not row.get('synced'):
                def change(rs, _row=row, _sha=sha):
                    x = AF._find(rs, _sha)
                    if x is not None and not x.get('sent'):
                        x['sent'] = {'at': _iso(datetime.fromtimestamp(float(_row.get('at') or now), AF._tz())), 'route': ROUTE,
                                     'message_id': _row.get('provider_id'), 'receipt': 'the texting service accepted it'}
                AF._write(con, row['opp'], change, now_dt)
                row['synced'], changed = True, True
                if _on_record(con, row['opp'], sha, 'sent'):
                    res['sent'] += 1
                else:
                    row['kept_here'] = CLOSED_RECORD          # the item's record cannot take it; this index keeps the fact
                    res['kept_here'] += 1
            if state == 'delivered' and not row.get('recorded_delivered'):
                proof = {'message_id': row.get('provider_id'), 'state': row.get('journal_state'), 'sha256': sha, 'seen_at': _iso(now_dt)}
                _verify_route(life, str(row.get('kind')), proof, now_dt)
                got = AF.mark_delivered(con, row['opp'], sha, _life(life), ROUTE, DESTINATION, proof, now_dt)
                landed = bool(got.get('delivered')) and _on_record(con, row['opp'], sha, 'delivered')     # mark_delivered says yes even when its write found nothing
                row['recorded_delivered'], row['receipt'] = True, proof
                row['delivered_note'] = '' if landed else (got.get('reason') or CLOSED_RECORD)
                if not landed:
                    row['kept_here'] = CLOSED_RECORD
                changed = True
                res['delivered'] += 1                           # the phone's own receipt is in the send journal: it arrived
            if state in ('failed', 'refused', 'not_sent') and not row.get('told'):
                res['failed'] += 1
                if _tell(con, row, now_dt):
                    res['told'] += 1
                row['told'], changed = True, True
            if state in ('sent', 'uncertain'):
                res['watching'] += 1
        except Exception as e:
            res.setdefault('errors', []).append(f'{sha[:8]}: {type(e).__name__}: {str(e)[:100]}')
    # the ledger keeps the facts, so the index forgets a try only once the item's record shows it went. A file that did not
    # go, or whose record could not take the mark, stays here: forgetting it would let it be attached again, or be called
    # "ready" again, later
    for sha in [s for s, r in d['files'].items() if r.get('state') in ('delivered',) and not r.get('kept_here') and now - float(r.get('at') or now) > 30 * 86400]:
        d['files'].pop(sha, None)
        changed = True
    if changed:
        _save_index(life, d)
    return res if any(v for v in res.values()) else None

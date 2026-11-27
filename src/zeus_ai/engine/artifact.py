"""artifact: a file the owner asked for is checked before anyone says it exists.

Why: "the PDF is ready" is otherwise only a sentence a model wrote. This module looks at the file, and a path on this
computer (a home-folder path) is never handed to the owner as if their phone could open it.

check(path, kind) proves three things about a requested file and records a fourth:
  it exists; it is not empty; it opens as what it claims to be (a PDF has its header and its end marker, JSON parses,
  text decodes and has words in it, an image has a known image header); and its sha256, so "the same file" can be
  checked later.

The job keeps three SEPARATE facts about a deliverable, and none implies the next:
  prepared    the file passed check(). Nothing more.
  done        what was asked for is finished and that was verified (who checked what). A prepared file is not done.
  delivered   it reached the owner by a route the line supports, with the destination and a receipt recorded.
              A send with no receipt is not delivered.
A local path is never something their phone can open. Delivery needs a route that is listed as verified in
<life>/delivery-routes.json ({"routes": {"<name>": {"carries": ["pdf", ...], "verified_at": "..."}}}). The texting line
carries words only, so with no such route the state is "prepared, delivery pending", with the reason. Nothing here
sends anything: it keeps the record and tells the truth about it.

Where the record lives: on the job's commitment (`deliverables`, written through commit.py's own bookkeeping write, the
version checked and unchanged; the check itself is also written down as proof with commit.record_verification), or, for
an item that has no commitment, at payload['deliverables'].

No switch of its own: nothing in the engine calls in here except recover.py's writer hook (switch recover-on), which
hands the writer the facts below (for_writer) and checks the note names no path on this Mac (local_paths).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import settings

KINDS = ('pdf', 'json', 'text', 'image')
ROUTES_FILE = 'delivery-routes.json'
PENDING = 'prepared, delivery pending'
NO_ROUTE = 'the texting line carries words only, and no other route to their phone has been set up and verified'
_IMAGE = ((b'\x89PNG\r\n\x1a\n', 'PNG'), (b'\xff\xd8\xff', 'JPEG'), (b'GIF87a', 'GIF'), (b'GIF89a', 'GIF'))
_LOCAL = re.compile(r"(?:file://\S+|(?<![\w.:/])~/[^\s\"'<>)]+|(?<![\w.:/])/(?:Users|private|tmp|var|Volumes|home|Library|Applications)/[^\s\"'<>)]*)")


def _tz():
    try:
        from ownertime import PACIFIC
        return PACIFIC
    except Exception:
        from zoneinfo import ZoneInfo
        return ZoneInfo(settings.OWNER_TZ)


def _iso(now=None) -> str:
    t = now if isinstance(now, datetime) else datetime.now(_tz())
    return (t if t.tzinfo else t.replace(tzinfo=_tz())).astimezone(_tz()).isoformat(timespec='seconds')


def _image_kind(head: bytes):
    for magic, name in _IMAGE:
        if head.startswith(magic):
            return name
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'WEBP'
    if head[4:8] == b'ftyp' and head[8:12] in (b'heic', b'heix', b'heif', b'mif1', b'msf1', b'avif'):
        return 'HEIC'
    return None


def check(path, kind: str) -> dict:
    """-> {'ok', 'path', 'kind', 'exists', 'size', 'sha256', 'why', 'checked_at'}. ok only when the file is there, is not
    empty and opens as `kind` ('pdf' | 'json' | 'text' | 'image'). `why` is one plain line when it is not ok. The
    sha256 is recorded whenever the file could be read at all. Never raises."""
    out = {'ok': False, 'path': str(path), 'kind': str(kind), 'exists': False, 'size': 0, 'sha256': None, 'why': None, 'checked_at': _iso()}
    try:
        p = Path(path)
        if not p.is_file():
            out['why'] = 'the file is not there' if not p.exists() else 'that is a folder, not a file'
            return out
        out['exists'] = True
        size = p.stat().st_size
        out['size'] = size
        if size == 0:
            out['why'] = 'the file is empty'
            return out
        h = hashlib.sha256()
        head, tail = b'', b''
        with open(p, 'rb') as fh:
            while True:
                block = fh.read(1 << 20)
                if not block:
                    break
                if not head:
                    head = block[:4096]
                tail = (tail + block)[-4096:]
                h.update(block)
        out['sha256'] = h.hexdigest()
        if kind == 'pdf':
            if b'%PDF-' not in head[:1024]:
                out['why'] = 'it does not open as a PDF: the PDF header is missing'
            elif b'%%EOF' not in tail:
                out['why'] = 'it is not a whole PDF: its end marker is missing, so it was cut short'
        elif kind == 'json':
            if size > 50_000_000:
                out['why'] = 'it is too large to check as JSON'
            else:
                try:
                    json.loads(p.read_bytes().decode('utf-8'))
                except (ValueError, UnicodeDecodeError):
                    out['why'] = 'it does not open as JSON: it does not parse'
        elif kind == 'text':
            try:
                with open(p, 'rb') as fh:
                    text = fh.read(5_000_000).decode('utf-8', 'strict' if size <= 5_000_000 else 'ignore')
            except UnicodeDecodeError:
                text = None
            if text is None or '\x00' in text:
                out['why'] = 'it does not open as text: it is not readable text'
            elif not text.strip():
                out['why'] = 'the file has no words in it'
        elif kind == 'image':
            if _image_kind(head) is None:
                out['why'] = 'it does not open as an image: no known image header'
        else:
            out['why'] = f'there is no check for this kind of file ("{str(kind)[:20]}")'
        out['ok'] = out['why'] is None
    except OSError as e:
        out['why'] = f'the file could not be read ({type(e).__name__})'
    return out


# ---- routes --------------------------------------------------------------------------------------------------------------

def routes(life) -> dict:
    """The delivery routes that are set up AND verified for this life folder: {name: {'carries': [...], 'verified_at'}}.
    {} when there is no file, when it cannot be read, and for any route with no verified_at. Data, never a guess."""
    try:
        d = json.loads((Path(life) / ROUTES_FILE).read_text())
    except (OSError, ValueError, TypeError):
        return {}
    got = d.get('routes') if isinstance(d, dict) else None
    return {str(k): v for k, v in (got or {}).items() if isinstance(v, dict) and v.get('verified_at') and isinstance(v.get('carries'), list)} if isinstance(got, dict) else {}


def route_for(life, kind: str):
    """A verified route that carries this kind of file, or None."""
    return next((name for name, r in routes(life).items() if kind in r['carries']), None)


# ---- the record ----------------------------------------------------------------------------------------------------------

def _load(con, ref):
    """-> (key, payload, commitment id or None)."""
    r = con.execute('SELECT key,payload FROM opps WHERE key=?', (str(ref),)).fetchone()
    if r is None:
        try:
            import commit as CM
            got = CM.get(con, ref)
        except Exception:
            got = None
        if got is None:
            return None
        r = con.execute('SELECT key,payload FROM opps WHERE key=?', (got['opp_key'],)).fetchone()
        if r is None:
            return None
    key, raw = tuple(r)
    p = json.loads(raw)
    c = p.get('commitment') if isinstance(p.get('commitment'), dict) else {}
    return key, p, c.get('commitment_id')


def deliverables(con, ref) -> list:
    got = _load(con, ref)
    if got is None:
        return []
    key, p, cid = got
    if cid:
        try:
            import commit as CM
            view = CM.get(con, cid) or {}
            return [dict(d) for d in view.get('deliverables') or [] if isinstance(d, dict)]
        except Exception:
            pass
    return [dict(d) for d in p.get('deliverables') or [] if isinstance(d, dict)]


def _write(con, ref, change, now=None) -> list:
    """change(list) edits the item's deliverables in place. One write, through the commitment when the item has one."""
    got = _load(con, ref)
    if got is None:
        raise KeyError(f'no item or job is saved under "{str(ref)[:80]}"')
    key, p, cid = got
    if cid:
        import commit as CM
        view = CM.get(con, cid)
        if view is not None and view.get('open'):
            def edit(obj, _opp):
                rows = [dict(d) for d in obj.get('deliverables') or [] if isinstance(d, dict)]
                change(rows)
                obj['deliverables'] = rows[-10:]
            done = CM._book(con, cid, 'deliverable', 'artifact:' + uuid.uuid4().hex, 'worker', now, edit, revision=view['revision'], kind='deliverable')
            return list((done.get('commitment') or {}).get('deliverables') or [])
    rows = [dict(d) for d in p.get('deliverables') or [] if isinstance(d, dict)]
    change(rows)
    con.execute("UPDATE opps SET payload=json_set(payload,'$.deliverables',json(?)), updated=? WHERE key=?", (json.dumps(rows[-10:], default=str), time.time(), key))
    con.commit()
    return rows[-10:]


def _find(rows: list, sha256: str):
    return next((d for d in rows if d.get('sha256') == sha256), None)


def prepared(con, ref, path, kind: str, now=None, name: str = '') -> dict:
    """Check the file and, only when it passes, record it as PREPARED on the job (path, kind, size, sha256, when).
    -> the check's result, plus 'recorded': True/False. A file that fails the check is recorded nowhere: nothing may
    later claim it exists. The check is also written down as proof on a job that has a commitment."""
    res = check(path, kind)
    res['recorded'] = False
    if not res['ok']:
        return res

    def change(rows):
        d = _find(rows, res['sha256'])
        if d is None:
            rows.append({'name': str(name or Path(path).name)[:120], 'path': str(path), 'kind': kind, 'size': res['size'], 'sha256': res['sha256'],
                         'prepared': {'at': _iso(now)}, 'done': None, 'delivered': None})
        else:
            d.update(path=str(path), prepared=d.get('prepared') or {'at': _iso(now)})
    _write(con, ref, change, now)
    res['recorded'] = True
    try:
        import commit as CM
        got = _load(con, ref)
        view = CM.get(con, got[2]) if got and got[2] else None
        if view is not None and view.get('open'):
            CM.record_verification(con, got[2], {'what': f'the {kind} file exists, is not empty and opens as a {kind} file', 'ref': 'sha256:' + res['sha256']},
                                   revision=view['revision'], op_id='artifact-check:' + res['sha256'][:16] + ':' + str(view['revision']), now=now)
    except Exception:
        pass
    return res


def mark_done(con, ref, sha256: str, checked: str, by: str = 'code', now=None) -> bool:
    """DONE: what was asked for is finished, and somebody checked that (`checked`: what was compared with what; `by`:
    'code' or their own word). Only a file that is on record as prepared, and that still passes its check with the same
    sha256, can be done. -> True when it was recorded."""
    rows = deliverables(con, ref)
    d = _find(rows, sha256)
    if d is None or not d.get('prepared') or not str(checked or '').strip():
        return False
    again = check(d.get('path'), d.get('kind'))
    if not again['ok'] or again['sha256'] != sha256:
        return False

    def change(rs):
        x = _find(rs, sha256)
        if x is not None:
            x['done'] = {'at': _iso(now), 'checked': str(checked)[:300], 'by': str(by)[:40]}
    _write(con, ref, change, now)
    return True


def mark_delivered(con, ref, sha256: str, life, route: str, destination: str, receipt, now=None) -> dict:
    """DELIVERED: it reached the owner. Recorded only when all of this holds: the file is on record as prepared; `route` is a
    verified route of this folder that carries this kind of file; a destination is named; and there is a receipt (what
    the route handed back). Anything less changes nothing and says why: -> {'delivered': bool, 'state', 'reason'}."""
    rows = deliverables(con, ref)
    d = _find(rows, sha256)
    if d is None or not d.get('prepared'):
        return {'delivered': False, 'state': 'not prepared', 'reason': 'no checked file is on record for this'}
    r = routes(life).get(str(route))
    if r is None or d.get('kind') not in r['carries']:
        return {'delivered': False, 'state': PENDING, 'reason': NO_ROUTE if not routes(life) else f'"{str(route)[:40]}" is not a verified route for this kind of file'}
    if not str(destination or '').strip():
        return {'delivered': False, 'state': PENDING, 'reason': 'no destination was named'}
    if not receipt:
        return {'delivered': False, 'state': PENDING, 'reason': 'it was sent with no receipt, so it is not known to have arrived'}

    def change(rs):
        x = _find(rs, sha256)
        if x is not None:
            x['delivered'] = {'at': _iso(now), 'route': str(route)[:40], 'destination': str(destination)[:200], 'receipt': receipt}
    _write(con, ref, change, now)
    return {'delivered': True, 'state': 'delivered', 'reason': ''}


def state(d: dict, life=None) -> dict:
    """The three facts of one deliverable, each by itself, and the plain words for where it stands:
    -> {'prepared': bool, 'done': bool, 'delivered': bool, 'words', 'reason'}."""
    pre, done, sent = bool(d.get('prepared')), bool(d.get('done')), bool(d.get('delivered'))
    if not pre:
        return {'prepared': False, 'done': False, 'delivered': False, 'words': 'not prepared', 'reason': 'no checked file is on record'}
    if sent:
        return {'prepared': True, 'done': done, 'delivered': True, 'words': ('done and delivered' if done else 'delivered, not yet verified as what was asked'), 'reason': ''}
    if life is not None and (Path(life) / 'file-delivery-on').exists():   # filedeliver.py says where a file stands once it can ride the note: attached, sent and not confirmed, or why not. "Delivered" only with the phone's own receipt (kept by filedeliver.py when this record was already closed)
        try:
            got = __import__('filedeliver').words(d, life)
            if got:
                return {'prepared': True, 'done': done, 'delivered': bool(got.get('delivered')), 'words': got['words'], 'reason': got['reason']}
        except Exception:
            pass
    route = route_for(life, d.get('kind')) if life is not None else None
    reason = NO_ROUTE if route is None else f'it has not been sent yet (it can go by {route})'
    return {'prepared': True, 'done': done, 'delivered': False, 'words': ('done, delivery pending' if done else PENDING), 'reason': reason}


def for_writer(d: dict, life=None) -> dict:
    """What the note writer may be told about a deliverable: its name, what kind of file it is, where it stands and why.
    Never its path: a path on this Mac is not something they can open on their phone."""
    s = state(d, life)
    size = int(d.get('size') or 0)
    return {k: v for k, v in (('file', str(d.get('name') or 'the file')), ('kind_of_file', str(d.get('kind') or '')),
                              ('size', f'{max(1, round(size / 1024))} KB' if size else ''), ('where_it_stands', s['words']),
                              ('why_not_delivered', s['reason'] if not s['delivered'] else '')) if v}


def local_paths(text: str) -> list:
    """Paths on this computer that a text names (home-folder paths, ~/..., file://...). A note to the owner's phone must name none."""
    return list(dict.fromkeys(m.group(0).rstrip('.,;:') for m in _LOCAL.finditer(str(text or ''))))

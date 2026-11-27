#!/usr/bin/env python3
"""zeus-checkpoint: save Zeus's work and history and prove a saved copy would come back safely.

`zeus-snapshot` saves Zeus's settings and code and skips every database. This tool saves the other half: the
databases (the life ledger, the durable-work store, the scheduler's run records, the delivery and outbox stores; a
full checkpoint adds the chat history and the memory outbox) and the small state files beside them.

  zeus-checkpoint take [--small | --full] [-m "why"]   save one checkpoint (default: small)
  zeus-checkpoint list [-n 20]                         newest first
  zeus-checkpoint status                               last small, last full, total size, last drill
  zeus-checkpoint verify <id>                          hash every saved file again and check every database copy
  zeus-checkpoint drill [<id>] [--engine DIR] [--monthly] [--keep]
                                                       restore into a throwaway folder and prove it would come back
  zeus-checkpoint prune [--keep-days 30] [--dry-run]   remove checkpoints older than that

How a checkpoint is made
- Databases are copied with SQLite's online backup (sqlite3.Connection.backup), never by copying the file: a file copy
  misses rows that are committed but still sit in the write-ahead log. Each copy is then one self-contained file.
- Credentials are never copied: .env, auth.json, token, key, cookie and password files, by name, by folder and by a
  look at the content. Everything refused is listed in the manifest with the reason.
- There is no single transaction across all stores. manifest.json carries the start and end time: anything that
  changed between the two may be in one store's copy and not in another's, and must be treated as uncertain.
- Only `prune` deletes, and only whole checkpoints older than the days given. It never removes the newest small or the
  newest full, whatever their age. No other command deletes.

Backups stay on this machine. To keep a copy elsewhere, point `checkpoint.store` at a synced or external folder, or
copy the store with your own backup tool.

Where: settings `checkpoint.store` (default <hermes home>/checkpoints/<profile>/), one folder per checkpoint named
<YYYYmmdd-HHMMSS>-<small|full>/ (folders 0700, files 0600).
Overrides (tests and rehearsals never touch live):
  ZEUS_CHECKPOINT_STORE, ZEUS_CHECKPOINT_PROFILE, ZEUS_CHECKPOINT_LAUNCHER, ZEUS_CHECKPOINT_ENGINE.
Standard library only. Python 3.11 or newer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'engine'))
import settings  # noqa: E402

FORMAT = 1
LIFE_REL = 'workspace/life-engine'
LEDGER_REL = LIFE_REL + '/ledger.sqlite3'
WORK_REL = 'plugin-data/zeus-work/state.sqlite3'
MAX_FILE = 50 * 1024 * 1024                 # one small state or evidence file; larger ones are listed as skipped
ID_RE = re.compile(r'^\d{8}-\d{6}-(small|full)$')
PRUNE_MIN_DAYS = 7                           # prune refuses to keep less than this
DEFAULT_ENGINE = str(settings.get('checkpoint.engine_dir', str(settings.ENGINE_DIR)))
OWNER_HOME_ZONE = settings.OWNER_TZ
DRILL_HOUR = int(settings.get('checkpoint.drill_hour', 10))    # the monthly drill: the 1st, at or after this hour where the owner is
DRILL_TIMEOUT = 900
IN_FLIGHT = ('There is no single moment across these stores: each was copied on its own between started_at and '
             'ended_at. Anything that changed in that window may be in one copy and not in another. Treat it as uncertain.')

# (what it is, path under the profile, the smallest checkpoint that carries it)
DATABASES = (
    ('life ledger', LEDGER_REL, 'small'),
    ('durable work', WORK_REL, 'small'),
    ('scheduler runs', 'cron/executions.db', 'small'),
    ('scheduler deliveries', 'cron/deliveries.db', 'small'),
    ('texting line receipts', LIFE_REL + '/line/line.sqlite3', 'small'),
    ('own email sends', 'plugin-data/own-agentmail/sends.sqlite3', 'small'),
    ('older proactive outbox', 'plugin-data/proactive-core/quiet-core.sqlite3', 'small'),
    ('older proactive line receipts', 'plugin-data/proactive-core/line.sqlite3', 'small'),
    ('owner-message admissions', 'plugin-data/zeus-scope/chat_admission.sqlite3', 'small'),
    ('morning briefs and asks', 'workspace/morning/morning.sqlite3', 'small'),
    ('chat history', 'state.db', 'full'),
    ('memory outbox', 'unified-memory/outbox.sqlite3', 'full'),
)
# small state outside the life folder: (what it is, path under the profile; a folder is taken whole)
STATE_PATHS = (
    ('scheduled jobs', 'cron/jobs.json'),
    ('pending memory intake', 'pending'),
    ('memory-sync gap log', 'unified-memory/gaps.jsonl'),
)
SCAN_FOR_OTHER_STORES = ('', 'cron', 'plugin-data', 'unified-memory', 'data', 'state', 'workspace')   # where "left out" is looked for
LEFT_OUT_ALWAYS = (
    ('settings, memories, plugin code, engine code', 'zeus-snapshot saves those; this tool saves work and history'),
    ('credentials (.env, auth.json, token, key, cookie and password files)', 'never copied'),
    ('answers of finished background jobs (cron/output)', 'only the answers of jobs still running at the time are kept'),
    ('unified-memory/operations', 'install records, not a queue'),
    ('screenshots of store pages (relogin-shots)', 'can show account details and are not needed to resume work'),
)

DB_SUFFIX = ('.sqlite3', '.sqlite', '.db')
SIDECAR = ('-wal', '-shm', '-journal')
SECRET_NAMES = frozenset(('.env', 'auth.json', 'photon-sidecar.json', '.netrc', 'credentials', 'credentials.json'))
SECRET_DIRS = frozenset(('mcp-tokens', 'vault', 'pairing', 'photon', 'browser-profile', '.ssh', '.gnupg'))
SECRET_NAME = re.compile(r'(^\.env|token|secret|credential|passw(?:or)?d|api[-_]?key|private[-_]?key|cookie|keychain'
                         r'|^(?:signin-handoff|knows-page)\.|^id_(?:rsa|ed25519|ecdsa|dsa)|\.(?:pem|key|p12|pfx|jks)$)', re.I)
SECRET_CONTENT = re.compile(rb'(sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|xox[abp]-[0-9A-Za-z-]{20,}|AKIA[0-9A-Z]{16}'
                            rb'|ghp_[A-Za-z0-9]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY)')
SNIFF_MAX = 2 * 1024 * 1024
OPEN_STATES = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')   # the five the engine calls open
UNCERTAIN_ACTIONS = ('sending', 'unknown')
JOB_ID = re.compile(r'^[0-9A-Za-z_-]{6,64}$')

class Problem(Exception):
    """Something that stops a command. The message is plain words."""


# ---- small things ----------------------------------------------------------------------------------------------------

def profile() -> Path:
    return Path(os.environ.get('ZEUS_CHECKPOINT_PROFILE') or settings.PROFILE_DIR)


def store() -> Path:
    return Path(os.environ.get('ZEUS_CHECKPOINT_STORE') or settings.get('checkpoint.store', str(settings.HERMES_HOME / 'checkpoints' / settings.PROFILE)))


def launcher() -> Path:
    return Path(os.environ.get('ZEUS_CHECKPOINT_LAUNCHER') or settings.get('engine.launcher', str(Path.home() / '.local/bin/zeus-life')))


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso(t: dt.datetime) -> str:
    return t.isoformat(timespec='seconds')


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def digest(lines) -> str:
    return hashlib.sha256('\n'.join(sorted(str(x) for x in lines)).encode()).hexdigest()


def read_json(p: Path):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def write_private(p: Path, text: str) -> None:
    """A file only its owner can read (0600), swapped in whole."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def write_json(p: Path, data) -> None:
    write_private(p, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, default=str) + '\n')


def private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def ensure_store() -> Path:
    st = store()
    new_parent = not st.parent.exists()
    st.mkdir(parents=True, exist_ok=True)
    os.chmod(st, 0o700)
    if new_parent:
        os.chmod(st.parent, 0o700)
    return st


@contextmanager
def store_lock():
    """One writer at a time (two takes, or a take and a prune, never step on each other)."""
    st = ensure_store()
    fd = os.open(st / '.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def human(n) -> str:
    n = float(n or 0)
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'bytes' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'


def within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def is_db_name(name: str) -> bool:
    return name.endswith(DB_SUFFIX)


def is_sidecar(name: str) -> bool:
    return any(name.endswith(s) for s in SIDECAR) or name.endswith('.lock') or name.endswith('.tmp')


# ---- what must never be copied -----------------------------------------------------------------------------------------

def secret_reason(path: Path, prof: Path, sniff: bool = True):
    """Why this file must not be copied, or None. By name, by the folder it sits in, and (small files) by content."""
    parts = path.relative_to(prof).parts if within(path, prof) else path.parts
    if path.name in SECRET_NAMES or SECRET_NAME.search(path.name):
        return 'a credential, token or key file (by its name): never copied'
    if set(parts[:-1]) & SECRET_DIRS:
        return 'it sits in a folder that holds credentials: never copied'
    if sniff:
        try:
            if path.stat().st_size <= SNIFF_MAX:
                with open(path, 'rb') as f:
                    if SECRET_CONTENT.search(f.read()):
                        return 'its content looks like a credential: never copied'
        except OSError:
            return None
    return None


class Take:
    """One checkpoint being made: where it goes, what went in, what was refused."""

    def __init__(self, prof: Path, work: Path, tier: str):
        self.prof, self.work, self.tier = prof, work, tier
        self.tree = work / 'profile'
        self.real_prof = Path(os.path.realpath(prof))
        self.files, self.databases, self.skipped, self.problems = [], [], [], []
        self.seen = set()

    def skip(self, source, why: str) -> None:
        self.skipped.append({'source': str(source), 'why': why})

    def copy_file(self, src: Path, kind: str) -> bool:
        """Every small file goes through here: inside the profile, not a credential, a plain file, not too large."""
        try:
            real = Path(os.path.realpath(src))
            if not within(real, self.real_prof):
                self.skip(src, 'it points outside the profile')
                return False
            rel = real.relative_to(self.real_prof)
            if str(rel) in self.seen:
                return True
            for candidate in ((src,) if real == src else (src, real)):
                why = secret_reason(candidate, self.prof if within(candidate, self.prof) else self.real_prof,
                                    sniff=candidate == real)
                if why:
                    self.skip(src, why)
                    return False
            if not real.is_file():
                self.skip(src, 'not a plain file')
                return False
            size = real.stat().st_size
            if size > MAX_FILE:
                self.skip(src, f'larger than {MAX_FILE // (1024 * 1024)} MB ({human(size)})')
                return False
            if is_db_name(real.name) or is_sidecar(real.name):
                self.skip(src, 'a database or one of its side files: databases are saved through SQLite, not as files')
                return False
            out = self.tree / rel
            private_dir(out.parent)
            shutil.copyfile(real, out)
            os.chmod(out, 0o600)
            self.seen.add(str(rel))
            self.files.append({'source': str(src), 'saved_as': 'profile/' + str(rel), 'kind': kind,
                               'bytes': out.stat().st_size, 'sha256': sha256_file(out)})
            return True
        except OSError as e:
            self.skip(src, f'could not be read ({type(e).__name__})')
            self.problems.append(f'a state file could not be read: {Path(src).name} ({type(e).__name__})')
            return False

    def copy_tree(self, root: Path, kind: str, leave=()) -> None:
        for base, dirs, files in os.walk(root, followlinks=False):
            b = Path(base)
            keep = []
            for d in sorted(dirs):
                p = b / d
                if p.is_symlink():
                    self.skip(p, 'a link to a folder: not followed')
                elif d in leave or d.startswith('.') or d == '__pycache__':
                    if d in leave:
                        self.skip(p, 'left out on purpose (see left_out)')
                else:
                    keep.append(d)
            dirs[:] = keep
            for f in sorted(files):
                p = b / f
                if is_sidecar(f) or is_db_name(f):
                    continue                    # locks and side files are not state; databases go through backup_db
                self.copy_file(p, kind)


# ---- databases -------------------------------------------------------------------------------------------------------

def _ro(path: Path, immutable: bool = False):
    uri = 'file:' + quote(str(path)) + ('?mode=ro&immutable=1' if immutable else '?mode=ro')
    return sqlite3.connect(uri, uri=True, timeout=60)


def table_counts(path: Path) -> dict:
    """Row counts of a COPY (opened so that nothing is written beside it)."""
    out = {}
    with closing(_ro(path, immutable=True)) as con:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names[:120]:
            if re.search(r'_fts(_|$)', name):
                continue                        # search-index side tables: rebuilt from the tables they index
            try:
                out[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error as e:
                out[name] = f'could not be counted ({type(e).__name__})'
    return out


def check_db(path: Path) -> dict:
    """integrity_check and user_version of a copy, without writing anything beside it."""
    with closing(_ro(path, immutable=True)) as con:
        version = con.execute('PRAGMA user_version').fetchone()[0]
        try:
            rows = [str(r[0]) for r in con.execute('PRAGMA integrity_check')][:5]
        except sqlite3.Error as e:      # the check itself could not run: said as that, never passed off as "ok"
            rows = [f'the check could not be run ({type(e).__name__}: {str(e)[:80]})']
    return {'integrity_check': 'ok' if rows == ['ok'] else '; '.join(rows) or 'no answer', 'user_version': version}


def backup_db(src: Path, dst: Path) -> dict:
    """SQLite's online backup of a live database into dst. Pages still in the write-ahead log are included, which a
    file copy would miss. The copy is left as ONE self-contained file (no -wal, no -shm)."""
    t0 = time.time()
    private_dir(dst.parent)
    for side in ('', *SIDECAR):
        Path(str(dst) + side).unlink(missing_ok=True)
    with closing(_ro(src)) as con:
        mode = str(con.execute('PRAGMA journal_mode').fetchone()[0])
        with closing(sqlite3.connect(str(dst))) as out:
            con.backup(out)
    with closing(sqlite3.connect(str(dst))) as out:     # a copy of a write-ahead-log database says so in its header:
        out.execute('PRAGMA journal_mode=DELETE')       # fold it back into one plain file
        out.commit()
    for side in SIDECAR:
        Path(str(dst) + side).unlink(missing_ok=True)
    os.chmod(dst, 0o600)
    info = {'journal_mode_at_source': mode, 'copied_with': 'sqlite online backup', 'seconds': round(time.time() - t0, 2)}
    info.update(check_db(dst))
    info['tables'] = table_counts(dst)
    info['bytes'] = dst.stat().st_size
    info['sha256'] = sha256_file(dst)
    return info


def wanted_databases(prof: Path, tier: str) -> list:
    """-> [(label, rel, tier)] for this tier, plus any other database sitting in the life folder (old ledgers)."""
    out = [d for d in DATABASES if tier == 'full' or d[2] == 'small']
    known = {d[1] for d in DATABASES}
    life = prof / LIFE_REL
    if life.is_dir():
        for base, dirs, files in os.walk(life, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(base) / d).is_symlink())
            for f in sorted(files):
                rel = str((Path(base) / f).relative_to(prof))
                if is_db_name(f) and rel not in known:
                    out.append(('another life-folder database', rel, 'small'))
    return out


def other_stores(prof: Path, taken: set) -> list:
    """Databases this checkpoint did NOT take, so the manifest says what was left out and why."""
    found = []
    later = {d[1] for d in DATABASES if d[2] == 'full'}
    for top in SCAN_FOR_OTHER_STORES:
        root = prof / top if top else prof
        if not root.is_dir():
            continue
        depth0 = len(root.parts)
        for base, dirs, files in os.walk(root, followlinks=False):
            b = Path(base)
            if not top or len(b.parts) - depth0 >= 3:
                dirs[:] = []
            else:
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (b / d).is_symlink()
                                 and str((b / d).relative_to(prof)) != LIFE_REL)      # the life folder's own databases are all taken
            for f in sorted(files):
                rel = str((b / f).relative_to(prof))
                if is_db_name(f) and rel not in taken:
                    found.append({'what': rel, 'why': 'only a full checkpoint carries it' if rel in later
                                  else "another part's own data: not on this tool's list"})
    return found


# ---- what the saved copies say (the counts a restore must match) --------------------------------------------------------

def _tables(con) -> set:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def safety_counts(tree: Path, switches) -> dict:
    """The things that must survive a restore, counted from a tree laid out like the profile: open items and open
    commitments, the owner's stop file and muted kinds, recorded approvals, and receipts that are still uncertain. Each count
    comes with a digest of the ids behind it, so "the same number of different rows" is caught too."""
    out = {'stop_file': 'off' in set(switches or ()), 'ledger': None}
    prefs = read_json(tree / LIFE_REL / 'preferences.json')
    fams = sorted(str(m.get('family')) for m in (prefs or {}).get('muted', []) if isinstance(m, dict) and m.get('family')) \
        if isinstance(prefs, dict) else []
    out['muted'] = {'file_present': (tree / LIFE_REL / 'preferences.json').is_file(), 'count': len(fams), 'digest': digest(fams)}
    ledger = tree / LEDGER_REL
    if not ledger.is_file():
        return out
    out['ledger'] = LEDGER_REL
    with closing(_ro(ledger, immutable=True)) as con:
        have = _tables(con)
        opps = con.execute('SELECT key, state, payload FROM opps').fetchall() if 'opps' in have else []
        open_rows = [(k, s) for k, s, _ in opps if s in OPEN_STATES]
        by = {}
        for _, s in open_rows:
            by[s] = by.get(s, 0) + 1
        out['open_items'] = {'count': len(open_rows), 'by_state': by, 'digest': digest(f'{k}|{s}' for k, s in open_rows)}
        approved = []
        for k, _, payload in opps:
            try:
                if (json.loads(payload) or {}).get('approved'):
                    approved.append(k)
            except (ValueError, AttributeError):
                continue
        acts = con.execute('SELECT id, state FROM actions').fetchall() if 'actions' in have else []
        by = {}
        for _, s in acts:
            by[s] = by.get(s, 0) + 1
        out['approvals'] = {'actions_table_present': 'actions' in have,
                            'actions': {'count': len(acts), 'by_state': by, 'digest': digest(f'{i}|{s}' for i, s in acts)},
                            'payload_approved': {'count': len(approved), 'digest': digest(approved)}}
        unsure = [(i, s) for i, s in acts if s in UNCERTAIN_ACTIONS]
        outbox = con.execute("SELECT key, state FROM outbox WHERE state IS NOT 'sent'").fetchall() if 'outbox' in have else []
        by = {}
        for _, s in outbox:
            by[str(s)] = by.get(str(s), 0) + 1
        out['uncertain'] = {'actions': {'count': len(unsure), 'digest': digest(f'{i}|{s}' for i, s in unsure)},
                            'outbox_not_sent': {'count': len(outbox), 'by_state': by, 'digest': digest(f'{k}|{s}' for k, s in outbox)}}
        if 'commitments' in have:
            rows = con.execute("SELECT commitment_id, revision, phase FROM commitments WHERE phase NOT IN ('done','stopped')").fetchall()
            out['open_commitments'] = {'table_present': True, 'count': len(rows), 'digest': digest(f'{c}|{r}|{p}' for c, r, p in rows)}
        else:
            out['open_commitments'] = {'table_present': False, 'count': 0, 'digest': digest([])}
    return out


def differences(expected, found, path: str = '') -> list:
    """Where two safety blocks differ, as plain dotted names."""
    if isinstance(expected, dict) and isinstance(found, dict):
        out = []
        for k in sorted(set(expected) | set(found)):
            out += differences(expected.get(k), found.get(k), f'{path}.{k}' if path else str(k))
        return out
    return [] if expected == found else [f'{path}: the checkpoint recorded {expected!r}, the restored copy has {found!r}']


# ---- the small state --------------------------------------------------------------------------------------------------

def evidence_paths(work_copy: Path) -> list:
    """Every file path the durable-work records refer to (their completion evidence), read from the saved copy."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ('path', 'artifact') and isinstance(v, str) and v.startswith('/'):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    with closing(_ro(work_copy, immutable=True)) as con:
        if 'work' not in _tables(con):
            return []
        for (payload,) in con.execute('SELECT payload FROM work'):
            try:
                walk(json.loads(payload))
            except ValueError:
                continue
    return sorted(set(found))


def running_job_ids(ledger_copy: Path) -> list:
    """Background jobs that were still running for an open item when the ledger was copied."""
    ids = []
    with closing(_ro(ledger_copy, immutable=True)) as con:
        if 'opps' not in _tables(con):
            return []
        for (payload,) in con.execute("SELECT payload FROM opps WHERE state='executing'"):
            try:
                jid = str(((json.loads(payload) or {}).get('exec') or {}).get('job_id') or '')
            except (ValueError, AttributeError):
                continue
            if JOB_ID.match(jid):
                ids.append(jid)
    return sorted(set(ids))


def read_links(prof: Path) -> dict:
    """Where each plugin link points and where the engine link points (names and paths only, no code)."""
    plugins, pdir = {}, prof / 'plugins'
    if pdir.is_dir():
        for p in sorted(pdir.iterdir()):
            if p.name.startswith('.'):
                continue
            plugins[p.name] = {'link': os.readlink(p) if p.is_symlink() else None, 'exists': p.exists()}
    eng = {'launcher': str(launcher())}
    try:
        m = re.search(r'(/\S+)/lifecli\.py', launcher().read_text())
        if m:
            code = Path(m.group(1))
            eng['code'] = str(code)
            if code.is_symlink():
                eng['code_points_at'] = os.readlink(code)
            eng['code_exists'] = code.exists()
        else:
            eng['could_not_be_read'] = 'the launcher names no engine folder'
    except OSError as e:
        eng['could_not_be_read'] = f'the launcher could not be read ({type(e).__name__})'
    return {'plugins': plugins, 'engine': eng}


def read_switches(life: Path) -> list:
    """A switch is a file in the life folder whose existence is what counts: a top-level name with no extension."""
    out = []
    if life.is_dir():
        for p in sorted(life.iterdir()):
            if p.is_file() and '.' not in p.name:
                out.append({'name': p.name, 'bytes': p.stat().st_size})
    return out


# ---- take ------------------------------------------------------------------------------------------------------------

def new_id(st: Path, tier: str):
    while True:
        t = now()
        cid = f'{t:%Y%m%d-%H%M%S}-{tier}'
        if not (st / cid).exists() and not (st / ('.incoming-' + cid)).exists():
            return cid, t
        time.sleep(0.25)


def take(tier: str = 'small', reason: str = '') -> dict:
    """Make one checkpoint. -> its manifest. Raises Problem when nothing could be made at all."""
    prof = profile()
    if not prof.is_dir():
        raise Problem(f'there is no profile folder at {prof}')
    with store_lock():
        st = store()
        cid, started = new_id(st, tier)
        work = private_dir(st / ('.incoming-' + cid))
        tk = Take(prof, work, tier)
        private_dir(tk.tree)

        # 1. databases, through SQLite's online backup
        for label, rel, db_tier in wanted_databases(prof, tier):
            src = prof / rel
            if not src.is_file():
                tk.skip(src, 'not there')
                if rel == LEDGER_REL:
                    tk.problems.append('the life ledger is not there: this checkpoint cannot bring work back')
                continue
            refused = secret_reason(src, prof, sniff=False)
            if refused:                         # a database named like a credential store (cookies, tokens) is refused like any other file
                tk.skip(src, refused)
                continue
            entry = {'label': label, 'source': str(src), 'saved_as': 'profile/' + rel, 'tier': db_tier}
            try:
                entry.update(backup_db(src, tk.tree / rel))
            except (sqlite3.Error, OSError) as e:
                Path(tk.tree / rel).unlink(missing_ok=True)
                tk.skip(src, f'could not be read ({type(e).__name__}: {str(e)[:120]})')
                tk.problems.append(f'{label} could not be read, so it is NOT in this checkpoint')
                continue
            if entry['integrity_check'] != 'ok':
                tk.problems.append(f'the copy of {label} did not pass its integrity check')
            tk.databases.append(entry)
        taken = {d['saved_as'][len('profile/'):] for d in tk.databases}

        # 2. the life folder's small state; switch files are kept as a list of names
        life = prof / LIFE_REL
        switches = read_switches(life)
        if life.is_dir():
            names = {s['name'] for s in switches}
            for p in sorted(life.iterdir()):
                if p.is_symlink() and p.is_dir():
                    tk.skip(p, 'a link to a folder: not followed')
                elif p.is_dir():
                    if p.name == 'relogin-shots':
                        tk.skip(p, 'left out on purpose (see left_out)')
                    elif not p.name.startswith('.') and p.name != '__pycache__':
                        tk.copy_tree(p, 'life state')
                elif p.name in names or is_sidecar(p.name) or is_db_name(p.name):
                    continue
                else:
                    tk.copy_file(p, 'life state')

        # 3. small state outside the life folder
        for what, rel in STATE_PATHS:
            src = prof / rel
            if src.is_dir():
                tk.copy_tree(src, what)
            elif src.exists():
                tk.copy_file(src, what)
            else:
                tk.skip(src, 'not there')

        # 4. completion evidence the durable-work records refer to (only files that exist under the profile)
        evidence = {'referenced': 0, 'saved': 0, 'outside_the_profile': 0, 'not_found': 0, 'refused': 0}
        if WORK_REL in taken:
            try:
                for raw in evidence_paths(tk.tree / WORK_REL):
                    evidence['referenced'] += 1
                    p = Path(raw)
                    real = Path(os.path.realpath(p))
                    if not within(real, tk.real_prof):
                        evidence['outside_the_profile'] += 1
                    elif not real.is_file():
                        evidence['not_found'] += 1
                    elif tk.copy_file(p, 'completion evidence'):
                        evidence['saved'] += 1
                    else:
                        evidence['refused'] += 1
            except (sqlite3.Error, OSError) as e:
                tk.problems.append(f'the durable-work copy could not be read for its evidence files ({type(e).__name__})')

        # 5. the answers of background jobs that were still running (so a restore can read a result that was on its way)
        job_answers = {'jobs_running': 0, 'files_saved': 0}
        if LEDGER_REL in taken:
            try:
                for jid in running_job_ids(tk.tree / LEDGER_REL):
                    job_answers['jobs_running'] += 1
                    folder = prof / 'cron/output' / jid
                    if folder.is_dir():
                        for p in sorted(folder.glob('*.md')):
                            job_answers['files_saved'] += bool(tk.copy_file(p, 'answer of a job still running'))
            except (sqlite3.Error, OSError) as e:
                tk.problems.append(f'the ledger copy could not be read for its running jobs ({type(e).__name__})')

        links = read_links(prof)
        write_json(work / 'switches.json', switches)
        write_json(work / 'links.json', links)
        ended = now()
        manifest = {
            'format': FORMAT, 'tool': 'zeus-checkpoint', 'id': cid, 'tier': tier, 'reason': reason or '',
            'started_at': iso(started), 'ended_at': iso(ended), 'started_ts': started.timestamp(), 'ended_ts': ended.timestamp(),
            'in_flight': IN_FLIGHT, 'profile': str(prof),
            'databases': tk.databases, 'files': tk.files,
            'lists': [{'saved_as': n, 'bytes': (work / n).stat().st_size, 'sha256': sha256_file(work / n)} for n in ('switches.json', 'links.json')],
            'switches': switches, 'links': links,
            'safety': safety_counts(tk.tree, [s['name'] for s in switches]),
            'evidence': evidence, 'job_answers': job_answers,
            'included': ['every database listed under "databases", copied with SQLite online backup',
                         'the life folder: every state file (also the markers that stop a repeat send), the people, companies and dated-note '
                         'folders and the other small folders',
                         'the scheduled jobs list (cron/jobs.json)',
                         'pending intake: the pending/ folder (memory intake waiting to be taken in) and the life folder\'s own facts queue',
                         'memory sync: its gap log (unified-memory/gaps.jsonl) in every checkpoint; its queue (unified-memory/outbox.sqlite3) '
                         'in a full checkpoint only, because of its size',
                         'the completion evidence files the durable-work records name, where they exist under the profile',
                         'the answers of background jobs that were still running',
                         'switch file names and where each plugin link and the engine link point (as lists, not as files)'],
            'skipped': tk.skipped,
            'left_out': [{'what': w, 'why': y} for w, y in LEFT_OUT_ALWAYS] + other_stores(prof, taken),
            'problems': tk.problems, 'complete': not tk.problems,
            'bytes': sum(d['bytes'] for d in tk.databases) + sum(f['bytes'] for f in tk.files),
        }
        write_json(work / 'manifest.json', manifest)
        for base, dirs, _files in os.walk(work):
            os.chmod(base, 0o700)
        os.rename(work, st / cid)
        write_status()
        return manifest


# ---- list, verify, status ----------------------------------------------------------------------------------------------

def checkpoints(st=None) -> list:
    """-> [(id, folder, manifest)] oldest first; only finished checkpoints."""
    st = Path(st or store())
    out = []
    if st.is_dir():
        for p in sorted(st.iterdir()):
            if p.is_dir() and ID_RE.match(p.name):
                m = read_json(p / 'manifest.json')
                if isinstance(m, dict):
                    out.append((p.name, p, m))
    return out


def find(cid=None):
    all_ = checkpoints()
    if not all_:
        raise Problem(f'there is no checkpoint in {store()} yet')
    if cid in (None, '', 'latest'):
        return all_[-1]
    for c in all_:
        if c[0] == cid:
            return c
    raise Problem(f'there is no checkpoint called {cid}. See: zeus-checkpoint list')


def folder_bytes(p: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += (Path(base) / f).stat().st_size
            except OSError:
                pass
    return total


def verify(cid=None) -> dict:
    """Hash every saved file again and check every database copy. -> {'id', 'ok', 'problems', 'checked'}"""
    cid, folder, m = find(cid)
    problems, checked = [], 0
    listed = {'manifest.json'}
    for entry in list(m.get('databases') or []) + list(m.get('files') or []) + list(m.get('lists') or []):
        rel = entry['saved_as']
        listed.add(rel)
        p = folder / rel
        if not p.is_file():
            problems.append(f'missing: {rel}')
            continue
        checked += 1
        if sha256_file(p) != entry.get('sha256'):
            problems.append(f'changed since it was saved (its hash no longer matches): {rel}')
            continue
        if 'integrity_check' in entry:
            try:
                got = check_db(p)
            except sqlite3.Error as e:
                problems.append(f'database copy does not open: {rel} ({type(e).__name__})')
                continue
            if got['integrity_check'] != 'ok':
                problems.append(f'database copy fails its integrity check: {rel}')
            if got['user_version'] != entry.get('user_version'):
                problems.append(f'database copy has another schema version than recorded: {rel}')
    for base, _dirs, files in os.walk(folder):
        for f in files:
            rel = str((Path(base) / f).relative_to(folder))
            if rel not in listed:
                problems.append(f'not in the manifest: {rel}')
    if not any(d['saved_as'] == 'profile/' + LEDGER_REL for d in m.get('databases') or []):
        problems.append('the life ledger is not in this checkpoint')
    return {'id': cid, 'ok': not problems, 'problems': problems, 'checked': checked}


def status() -> dict:
    st = store()
    all_ = checkpoints(st)

    def last(tier):
        for cid, folder, m in reversed(all_):
            if m.get('tier') == tier:
                return {'id': cid, 'ended_at': m.get('ended_at'), 'bytes': m.get('bytes'), 'complete': bool(m.get('complete')),
                        'problems': len(m.get('problems') or [])}
        return None

    drill = read_json(st / 'drill-status.json')
    unfinished = sorted(p.name for p in st.iterdir() if p.is_dir() and p.name.startswith('.incoming-')) if st.is_dir() else []
    return {'updated': iso(now()), 'store': str(st), 'count': len(all_),
            'last_small': last('small'), 'last_full': last('full'),
            'total_bytes': folder_bytes(st) if st.is_dir() else 0,
            'unfinished': unfinished,
            'drill': drill, 'prune': read_json(st / 'prune-status.json')}


def write_status() -> dict:
    s = status()
    try:
        write_json(store() / 'status.json', s)
    except OSError:
        pass
    return s


# ---- prune: keep the last N days ---------------------------------------------------------------------------------------

def id_time(cid: str):
    """When a checkpoint was taken, from its name (local time) -> seconds, or None for a name that is not a checkpoint's."""
    try:
        return dt.datetime.strptime(str(cid)[:15], '%Y%m%d-%H%M%S').timestamp()
    except ValueError:
        return None


def _newest_per_tier(ids) -> set:
    keep = set()
    for tier in ('small', 'full'):
        mine = sorted(i for i in ids if i.endswith('-' + tier))
        if mine:
            keep.add(mine[-1])
    return keep


def prune(keep_days: int = 30, dry_run: bool = False, at=None) -> dict:
    """Remove whole checkpoints older than keep_days. -> what was removed.
    Never removed, whatever their age: the newest small and the newest full.
    Only a real folder (not a link) directly inside the store, named like a checkpoint, with its manifest."""
    keep_days = int(keep_days)
    if keep_days < PRUNE_MIN_DAYS:
        raise Problem(f'prune keeps at least {PRUNE_MIN_DAYS} days (asked for {keep_days})')
    at = time.time() if at is None else float(at)
    cutoff = at - keep_days * 86400
    with store_lock():
        st = store()
        all_ = checkpoints(st)
        protect = _newest_per_tier([c[0] for c in all_])
        here, problems = [], []
        for cid, folder, _m in all_:
            t = id_time(cid)
            if t is None or t >= cutoff or cid in protect:
                continue
            if folder.is_symlink() or not folder.is_dir() or folder.parent.resolve() != st.resolve() or not ID_RE.match(folder.name) \
                    or not (folder / 'manifest.json').is_file():
                problems.append(f'left alone (not a plain checkpoint folder in the store): {cid}')
                continue
            size = folder_bytes(folder)
            if not dry_run:
                shutil.rmtree(folder)
                if folder.exists():
                    problems.append(f'could not be removed: {cid}')
                    continue
            here.append({'id': cid, 'bytes': size})
        result = {'at': iso(dt.datetime.fromtimestamp(at).astimezone()), 'keep_days': keep_days, 'dry_run': bool(dry_run),
                  'removed_here': here, 'bytes_freed_here': sum(x['bytes'] for x in here),
                  'problems': problems, 'kept_whatever_their_age': sorted(protect), 'ok': not problems}
        if not dry_run:
            write_json(st / 'prune-status.json', result)
            try:
                with open(st / 'prune-log.jsonl', 'a') as f:
                    f.write(json.dumps(result) + '\n')
                os.chmod(st / 'prune-log.jsonl', 0o600)
            except OSError:
                pass
            write_status()
        return result


# ---- drill: would this checkpoint come back safely? ----------------------------------------------------------------------

def owner_zone(life: Path, at: float):
    """Where the owner is at that moment (the engine's own rule, ownertime.py: owner-zone.json with zone, from, until;
    else the owner's home time zone)."""
    d = read_json(Path(life) / 'owner-zone.json')
    try:
        z = ZoneInfo(str(d['zone']))
        first = dt.date.fromisoformat(str(d['from'])) if d.get('from') else None
        until = dt.date.fromisoformat(str(d['until'])) if d.get('until') else None
        if first and at < dt.datetime(first.year, first.month, first.day, tzinfo=z).timestamp():
            return ZoneInfo(OWNER_HOME_ZONE)
        if until and at >= (dt.datetime(until.year, until.month, until.day, tzinfo=z) + dt.timedelta(days=1)).timestamp():
            return ZoneInfo(OWNER_HOME_ZONE)
        return z
    except Exception:
        return ZoneInfo(OWNER_HOME_ZONE)


def monthly_due(at: float = None) -> tuple:
    """The monthly drill: once per calendar month where the owner is, at the first run at or after DRILL_HOUR on the 1st.
    -> (due, why). A month whose 1st was missed is caught up once, at the next run."""
    at = time.time() if at is None else float(at)
    here = dt.datetime.fromtimestamp(at, owner_zone(profile() / LIFE_REL, at))
    month = f'{here:%Y-%m}'
    done = (read_json(store() / 'drill-status.json') or {}).get('month')
    if done == month:
        return False, f'the drill for {month} has already run'
    if here.day == 1 and here.hour < DRILL_HOUR:
        return False, f'it is before {DRILL_HOUR}:00 on the 1st where the owner is'
    return True, month


def restore_into(folder: Path, m: dict, sandbox: Path) -> list:
    """Lay the checkpoint out as a profile inside the sandbox. -> problems. Switch files are made again from the list,
    except notify-live: with it left out the engine cannot send."""
    problems = []
    prof = private_dir(sandbox / 'profile')
    for entry in list(m.get('databases') or []) + list(m.get('files') or []):
        rel = entry['saved_as']
        src, dst = folder / rel, sandbox / rel
        if not src.is_file():
            problems.append(f'missing from the checkpoint: {rel}')
            continue
        private_dir(dst.parent)
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
        if sha256_file(dst) != entry.get('sha256'):
            problems.append(f'the restored copy is not the file that was saved: {rel}')
    life = private_dir(prof / LIFE_REL)
    for s in m.get('switches') or []:
        if s['name'] != 'notify-live':
            write_private(life / s['name'], '')
    private_dir(sandbox / 'home')
    return problems


def open_restored(sandbox: Path, m: dict) -> tuple:
    """Open every restored database the ordinary way (read and write) and check it. -> (results, problems)"""
    results, problems = [], []
    for entry in m.get('databases') or []:
        p = sandbox / entry['saved_as']
        one = {'database': entry['saved_as'], 'opens': False}
        if p.is_file():
            try:
                with closing(sqlite3.connect(str(p), timeout=10)) as con:
                    rows = [str(r[0]) for r in con.execute('PRAGMA integrity_check')][:5]
                    one.update(opens=True, integrity_check='ok' if rows == ['ok'] else '; '.join(rows),
                               user_version=con.execute('PRAGMA user_version').fetchone()[0])
            except sqlite3.Error as e:
                one['error'] = type(e).__name__
            if not one['opens']:
                problems.append(f'restored database does not open: {entry["saved_as"]}')
            elif one['integrity_check'] != 'ok':
                problems.append(f'restored database fails its integrity check: {entry["saved_as"]}')
            elif one['user_version'] != entry.get('user_version'):
                problems.append(f'restored database has another schema version than recorded: {entry["saved_as"]}')
            elif table_counts(p) != entry.get('tables'):
                problems.append(f'restored database has other row counts than recorded: {entry["saved_as"]}')
        results.append(one)
    return results, problems


def credentials_in(sandbox: Path, m: dict) -> list:
    """A second look at every state file that was restored: none may be a credential. (take refuses them; this is the
    check that the refusal held.)"""
    prof = sandbox / 'profile'
    return [e['saved_as'] for e in m.get('files') or [] if (sandbox / e['saved_as']).is_file() and secret_reason(sandbox / e['saved_as'], prof)]


def run_engine(sandbox: Path, engine: Path, at=None) -> dict:
    """Two runs of the engine against the restored life folder, in its own process with every door closed
    (drill_engine.py). -> what it reported, or {'error': ...}."""
    out = sandbox / 'engine-result.json'
    cmd = [sys.executable, '-I', '-B', str(HERE / 'drill_engine.py'), '--sandbox', str(sandbox), '--engine', str(engine), '--out', str(out),
           '--live-profile', str(profile()), '--owner-tz', OWNER_HOME_ZONE]
    if at:
        cmd += ['--now', at]
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(sandbox / 'home'), 'LANG': 'en_US.UTF-8'}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=DRILL_TIMEOUT, env=env, cwd=str(sandbox), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {'error': f'the engine did not finish within {DRILL_TIMEOUT // 60} minutes'}
    except OSError as e:
        return {'error': f'the engine could not be started ({type(e).__name__})'}
    res = read_json(out)
    if not isinstance(res, dict):
        return {'error': 'the engine run left no result', 'exit': p.returncode, 'stderr_tail': (p.stderr or '')[-800:]}
    res['exit'] = p.returncode
    return res


def judge_engine(res: dict) -> tuple:
    """What the two engine runs prove. -> (problems, notes, summary). Code decides; nothing here is a guess:
    a job started twice for one item, or anything handed to the line, fails the drill."""
    problems, notes = [], []
    if res.get('error'):
        return [f"the engine could not be run against the restored copy: {res['error']}"], notes, {}
    runs = res.get('runs') or []
    if len(runs) != 2:
        problems.append(f'the engine was to run twice and ran {len(runs)} time(s)')
    if res.get('sends'):
        problems.append(f"{res['sends']} message(s) were handed to the texting line during the drill; none may be")
    if res.get('live_switch_present'):
        problems.append('the restored life folder had the live-sending switch; the drill must run without it')
    per_item = res.get('jobs_per_item') or {}
    twice = sorted(k for k, ids in per_item.items() if len(ids) > 1)
    once = sorted(k for k, ids in per_item.items() if len(ids) == 1)
    if twice:
        problems.append(f'work was started twice for {len(twice)} item(s): a restore would repeat it')
    if res.get('jobs_without_item'):
        problems.append(f"{len(res['jobs_without_item'])} job(s) were booked that no item owns")
    trouble = {}
    for i, r in enumerate(runs, 1):
        if r.get('stopped_by'):
            problems.append(f"run {i} of the engine stopped: {r['stopped_by']}")
        for line in r.get('crashed') or []:
            part = str(line).split(':', 1)[0]
            if part in ('work', 'tick'):
                problems.append(f'run {i}: the part of the engine that resumes work failed ({str(line)[:160]})')
            else:
                trouble.setdefault(part, []).append(i)
    if trouble:
        notes.append('with every source closed, these parts of the engine said they could not do their reading: '
                     + ', '.join(f"{part} (run {' and '.join(str(i) for i in runs_)})" for part, runs_ in sorted(trouble.items())))
    due = res.get('due_before') or []
    after = res.get('after_run_2') or {}
    moved = [k for k in due if (after.get(k) or {}).get('state') != 'pending']
    stopped = bool(res.get('stop_file'))
    if stopped:
        notes.append('the owner\'s stop file was set when the checkpoint was made: the restored engine stayed stopped, as it must')
        if res.get('jobs'):
            problems.append('the stop file was set and the engine still started work')
    elif not due:
        notes.append('no work was due in this checkpoint at the time of the drill, so resuming was not exercised')
    elif not res.get('can_work'):
        notes.append('work was due, but background work was switched off in the checkpoint (no active mandate, or executor-off)')
    elif not moved:
        problems.append(f'{len(due)} item(s) were due and nothing resumed across two runs')
    waiting = [k for k in due if k not in moved]
    if waiting and moved:
        notes.append(f'{len(waiting)} due item(s) were still waiting after two runs (the engine starts a few jobs at a time)')
    if res.get('doors'):
        notes.append('the engine reached for closed doors and was refused: ' + ', '.join(f'{kind} ({n})' for kind, n in sorted(res['doors'].items())))
    summary = {'due': len(due), 'due_resumed_exactly_once': len([k for k in due if k in once]), 'resumed_exactly_once': len(once),
               'resumed_twice': len(twice), 'moved_on': len(moved),
               'still_waiting': len(waiting), 'in_flight_when_saved': len(res.get('in_flight_before') or []),
               'jobs_booked': len(res.get('jobs') or []), 'messages_handed_to_the_line': int(res.get('sends') or 0),
               'resume_exercised': bool(due) and not stopped and bool(res.get('can_work')), 'stopped_by_owner_stop_file': stopped}
    return problems, notes, summary


def drill(cid=None, engine=None, keep: bool = False, at=None) -> dict:
    """Restore one checkpoint into a throwaway folder and prove it would come back. -> the drill record (also written).
    Never touches live data, never needs the network."""
    t0 = time.time()
    cid, folder, m = find(cid)
    engine = Path(engine or os.environ.get('ZEUS_CHECKPOINT_ENGINE') or DEFAULT_ENGINE)
    record = {'at': iso(now()), 'checkpoint': cid, 'tier': m.get('tier'), 'checkpoint_ended_at': m.get('ended_at'),
              'engine': str(engine), 'problems': [], 'notes': [], 'checks': {}}
    problems = record['problems']
    sandbox = Path(tempfile.mkdtemp(prefix='zeus-drill-'))
    os.chmod(sandbox, 0o700)
    try:
        v = verify(cid)
        record['checks']['saved_files'] = {'checked': v['checked'], 'ok': v['ok']}
        problems += ['the checkpoint was made with a problem: ' + p for p in m.get('problems') or []]
        problems += v['problems']
        problems += [p for p in restore_into(folder, m, sandbox) if p not in problems]
        dbs, db_problems = open_restored(sandbox, m)
        record['checks']['databases'] = dbs
        problems += db_problems
        expected = m.get('safety') or {}
        found = safety_counts(sandbox / 'profile', [s['name'] for s in m.get('switches') or []]) \
            if (sandbox / 'profile' / LEDGER_REL).is_file() else {}
        diffs = differences(expected, found) if found else ['the life ledger could not be restored, so nothing could be counted']
        record['checks']['kept'] = {'same': not diffs, 'expected': expected, 'found': found}
        problems += ['not everything came back: ' + d for d in diffs]
        creds = credentials_in(sandbox, m)
        record['checks']['no_credentials'] = not creds
        if creds:
            problems.append(f'{len(creds)} credential file(s) are in the checkpoint; there must be none')
        if not engine.is_dir() or not (engine / 'tick.py').is_file():
            problems.append(f'there is no engine at {engine}')
        elif not (sandbox / 'profile' / LEDGER_REL).is_file():
            problems.append('the engine was not run: there is no restored ledger to run it on')
        else:
            res = run_engine(sandbox, engine, at)
            p2, notes, summary = judge_engine(res)
            problems += p2
            record['notes'] += notes
            record['checks']['resume'] = summary
            record['checks']['engine'] = {k: res.get(k) for k in ('runs', 'jobs_per_item', 'due_before', 'in_flight_before', 'doors', 'door_examples',
                                                                    'python', 'drill_clock', 'error', 'exit', 'stderr_tail', 'switches', 'alarms', 'alarm_kinds', 'scheduler_checks',
                                                                    'after_run_1', 'after_run_2', 'jobs', 'can_work', 'open_before')
                                          if k in res}
    except Problem:
        raise
    except Exception as e:      # a drill that breaks is a failed drill, said plainly, never a silent pass
        problems.append(f'the drill itself broke: {type(e).__name__}: {str(e)[:200]}')
    finally:
        if keep:
            record['sandbox'] = str(sandbox)
        else:
            shutil.rmtree(sandbox, ignore_errors=True)
            record['sandbox'] = 'deleted'
    record['ok'] = not problems
    record['seconds'] = round(time.time() - t0, 1)
    st = ensure_store()
    stamp = f'{now():%Y%m%d-%H%M%S}'
    private_dir(st / 'drills')
    write_json(st / 'drills' / f'{stamp}-{cid}.json', record)
    here = dt.datetime.fromtimestamp(time.time(), owner_zone(profile() / LIFE_REL, time.time()))
    write_json(st / 'drill-status.json', {'ok': record['ok'], 'at': record['at'], 'checkpoint': cid, 'month': f'{here:%Y-%m}',
                                           'problems': problems[:20], 'resume': record['checks'].get('resume'),
                                           'record': str(st / 'drills' / f'{stamp}-{cid}.json')})
    write_status()
    return record


# ---- the command -------------------------------------------------------------------------------------------------------

def _print_status(s: dict) -> None:
    def one(label, c):
        if not c:
            return f'{label}: none yet'
        return f"{label}: {c['id']} ({human(c['bytes'])})" + ('' if c['complete'] else f", with {c['problems']} problem(s): see its manifest")

    print(one('Last small', s['last_small']))
    print(one('Last full', s['last_full']))
    print(f"Checkpoints: {s['count']}, {human(s['total_bytes'])} in {s['store']}")
    if s['unfinished']:
        print(f"Unfinished folders left by an interrupted run: {len(s['unfinished'])}")
    d = s.get('drill')
    if not d:
        print('Restore drill: never run')
    else:
        print(f"Restore drill: {'PASSED' if d.get('ok') else 'FAILED'} at {d.get('at')} on {d.get('checkpoint')}"
              + ('' if d.get('ok') else f" ({len(d.get('problems') or [])} problem(s))"))
    pr = s.get('prune')
    if not pr:
        print('Old checkpoints: never cleared yet')
    else:
        print(f"Old checkpoints: kept {pr.get('keep_days')} days; last cleared {pr.get('at')} ({len(pr.get('removed_here') or [])} removed)"
              + ('' if pr.get('ok') else ' WITH A PROBLEM: ' + str(pr.get('problems'))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='zeus-checkpoint', description="Save Zeus's work and history and prove it comes back.")
    sub = ap.add_subparsers(dest='cmd', required=True)
    t = sub.add_parser('take', help='save one checkpoint')
    g = t.add_mutually_exclusive_group()
    g.add_argument('--small', action='store_true', help='work stores and small state (the default)')
    g.add_argument('--full', action='store_true', help='also the chat history and the memory outbox')
    t.add_argument('-m', '--reason', default='')
    ls = sub.add_parser('list', help='newest first')
    ls.add_argument('-n', type=int, default=20)
    sub.add_parser('status', help='last small, last full, total size, last drill')
    v = sub.add_parser('verify', help='hash every saved file again and check every database copy')
    v.add_argument('id', nargs='?', default=None)
    d = sub.add_parser('drill', help='restore into a throwaway folder and prove it would come back')
    d.add_argument('id', nargs='?', default=None)
    d.add_argument('--engine', default=None, help='the engine folder to run against the restored copy')
    d.add_argument('--monthly', action='store_true', help='run only if this month has had no drill and it is at or after the drill hour on the 1st where the owner is')
    d.add_argument('--keep', action='store_true', help='keep the throwaway folder (it is deleted otherwise)')
    d.add_argument('--now', default=None, help='for tests: the time the restored engine believes it is (ISO with offset)')
    pr = sub.add_parser('prune', help='remove checkpoints older than --keep-days (the newest small and full always stay)')
    pr.add_argument('--keep-days', type=int, default=30)
    pr.add_argument('--dry-run', action='store_true', help='say what would be removed; remove nothing')
    a = ap.parse_args(argv)
    old_mask = os.umask(0o077)
    try:
        if a.cmd == 'take':
            m = take('full' if a.full else 'small', a.reason)
            print(f"Saved checkpoint {m['id']}: {len(m['databases'])} databases, {len(m['files'])} state files, {human(m['bytes'])}. "
                  f"{len(m['skipped'])} left out or refused (listed in its manifest).")
            for p in m['problems']:
                print('PROBLEM: ' + p, file=sys.stderr)
            return 0 if m['complete'] else 1
        if a.cmd == 'list':
            rows = checkpoints()[-a.n:][::-1]
            for cid, _folder, m in rows:
                print(f"{cid}  {human(m.get('bytes')):>10}  "
                      f"{'ok' if m.get('complete') else 'PROBLEMS'}  {m.get('reason') or ''}")
            if not rows:
                print('No checkpoints yet.')
            return 0
        if a.cmd == 'status':
            _print_status(write_status())
            return 0
        if a.cmd == 'verify':
            r = verify(a.id)
            if r['ok']:
                print(f"{r['id']}: all {r['checked']} saved files match their hashes and every database copy passes its check.")
                return 0
            print(f"{r['id']}: {len(r['problems'])} PROBLEM(S)", file=sys.stderr)
            for p in r['problems']:
                print('  ' + p, file=sys.stderr)
            return 1
        if a.cmd == 'prune':
            r = prune(a.keep_days, a.dry_run)
            verb = 'Would remove' if r['dry_run'] else 'Removed'
            print(f"{verb} {len(r['removed_here'])} checkpoint(s) older than {r['keep_days']} days ({human(r['bytes_freed_here'])}). "
                  f"Always kept: {', '.join(r['kept_whatever_their_age']) or 'nothing yet'}.")
            for x in r['removed_here']:
                print(f"  here: {x['id']} ({human(x['bytes'])})")
            for q in r['problems']:
                print('PROBLEM: ' + q, file=sys.stderr)
            return 0 if r['ok'] else 1
        if a.cmd == 'drill':
            if a.monthly:
                due, why = monthly_due()
                if not due:
                    print(f'No drill now: {why}.')
                    return 0
            r = drill(a.id, a.engine, a.keep, a.now)
            res = r['checks'].get('resume') or {}
            if r['ok']:
                print(f"Restore drill PASSED on {r['checkpoint']}: every saved file and database came back and the counts match. "
                      + (f"Across two runs of the engine, work started again for {res.get('resumed_exactly_once', 0)} item(s), each exactly once"
                         f" ({res.get('due_resumed_exactly_once', 0)} of {res.get('due', 0)} that were due; {res.get('still_waiting', 0)} still waiting their turn)."
                         if res.get('resume_exercised') else 'Resuming was not exercised (see the notes).') + ' Nothing was sent.')
            else:
                print(f"RESTORE DRILL FAILED on {r['checkpoint']}: {len(r['problems'])} problem(s)", file=sys.stderr)
                for p in r['problems'][:30]:
                    print('  ' + p, file=sys.stderr)
            for n in r['notes'][:12]:
                print('note: ' + n)
            return 0 if r['ok'] else 1
    except Problem as e:
        print(f'zeus-checkpoint: {e}', file=sys.stderr)
        return 2
    finally:
        os.umask(old_mask)
    return 0


if __name__ == '__main__':
    sys.exit(main())

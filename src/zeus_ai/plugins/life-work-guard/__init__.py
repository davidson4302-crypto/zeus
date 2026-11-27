"""life-work-guard: a background prep job cannot send or change anything.

A life-work job holds every tool Zeus has. Telling it not to send or change anything, and auditing it after it
finished (life-engine jobaudit.py), would find a break only when it could no longer be undone. So this hook stops such
an action at the moment it is tried: every tool call made inside a life pipes background job is judged BEFORE it
runs, with the audit's own rules, so the two can never disagree.

Only sessions of jobs the life engine created are touched (their names start with life-work, life-exec or
life-sim). The owner's chat, their other scheduled jobs and every other profile are never looked at.

Stopped: sends, changes, bookings, deletes, memory saves, new jobs, account connections, and reads of the owner's
private text threads with other people. Allowed: reading, searching, drafting into scratch folders, and the job's own
work list (that list polices itself).

Switch files in the life-engine folder:  guard-off = this guard does nothing;  guard-shadow = log only, stop nothing.

When the guard itself breaks: with the switch file guard-fail-safe-on, a call made inside a known life job is judged
by a small stand-alone check instead (fail_safe below, no engine files needed): sends, purchases, deletes and starting
other jobs are stopped; reading and prep go ahead. Without that switch, or when the job cannot be identified at all,
the error is logged and the call goes ahead (the audit still runs afterwards).

Stopped or changed after it was booked (commitment integrity): with the switch file commit-integrity-on, a job whose
name ends in an attempt mark ("[a_" + 12 hex + "]") asks the engine before EVERY tool call whether it is still the
current attempt of an open item (integrity.py job-gate: the owner's off switch, the item dropped, closed or corrected,
permission gone). A no, or a question that could not be asked or answered, refuses the call, reads included. The same
question is asked when the guard itself broke, before the stand-alone check may allow anything. Jobs without the
mark, and everything without the switch, are judged exactly as before.

Quiet hours on what leaves through Hermes itself: nothing breaks the owner's quiet hours (settings.QUIET_START to
QUIET_END on their clock) unless they messaged first. The engine holds its own notes; a scheduled job's result, an
approval card and anything sent with `hermes send` leave through shared Hermes, which asks this plugin first (hook
pre_deliver, only because Zeus's config sets cron.deliver_gate). deliver_gate below answers with the engine's own
clock (ownertime.contact_hold): during quiet hours the whole message waits and goes once they end. None of these is a
reply to the owner, so a recent text from them opens nothing here. Any trouble answering holds. Hermes also holds
when this plugin did not load at all. No switch file: taking cron.deliver_gate out of Zeus's config turns it off.

Answering the owner at night: if the owner texted first, what Zeus sends as part of answering them (a file, a link, a
second message) goes right away; what Zeus starts on its own still waits. Zeus's direct reply never comes through
here. What does is a `hermes send` its chat turn runs in its terminal, and that one is let through only when all
three can be shown (answering_him below, then the engine's contact_hold(reply=True)):
  1. it was run by Zeus's own live chat turn with the owner. Hermes writes each turn's facts into every command the
     turn runs (HERMES_SESSION_PLATFORM / _USER_ID / _CHAT_ID / _CHAT_TYPE; gateway/run.py _set_session_env, bridged
     by tools/environments/local.py). A scheduled job's commands carry an empty platform and HERMES_CRON_SESSION=1
     (cron/scheduler.py), a script started by a timer carries none of them, the approval-card sender strips them.
     The turn must be a private chat and its author one of the owner's own addresses (owner_ids in Zeus's config).
  2. a real message from the owner arrived no more than ownertime.REPLY_WINDOW ago (the chat plugin's stamp file).
  3. it goes to the owner: one of those same addresses, or the very chat that turn is in. A bare platform name means
     the profile's home channel there, which must itself be one of the owner's addresses.
Anything missing, unreadable or odd counts as "Zeus started it": it waits for the end of quiet hours, nothing is
dropped.

Helpers: helpers may use the browser read-only, buying stays with Zeus behind the purchase lock, and helpers never
text the owner. Zeus's chat hands long jobs (research, shopping compares, mail digging) to a background helper
(Hermes delegate_task) so its chat stays free. A helper is a background prep job like a life job, so the same guard
judges it, with the same audit rules, and stricter:
  - every tool call made inside a helper (agent.delegation_context.is_delegated_child_context) is judged before it
    runs. Reads go ahead. A send, change, booking, purchase, delete, memory or work-list change, new job, sign-in,
    account connection, desktop control or private-thread read is stopped. Hermes itself already takes
    delegate_task, clarify, memory, send_message and cronjob away from a helper; everything else (agentmail_send,
    zeus_owner_report_send, the terminal, the browser, Composio) it would keep without this.
  - at most HELPER_CAP helpers per chat at once. A fourth is refused before it starts, and so is any helper when the
    shared helper pool is full, because Hermes would then run it inside the chat turn and the chat would be stuck.
    A refused job is kept (in memory, per chat) and handed back to the chat, with its exact tasks, on the turn that
    brings the next helper's result, so it starts as soon as a slot is free. Hermes itself never queues: at capacity
    it refuses (tools/async_delegation.py _dispatch_admitted).
  - a bare "stop" (or "nvm", "never mind", "cancel that") from the owner in their private chat stops every helper of
    that chat at once, and drops its waiting jobs, by code, before the model answers. The stop is Hermes's own
    durable stop (delegation.durable_stop), so a result that comes back later is dropped and never becomes a late text.
The guard-off and guard-shadow switch files cover helpers too. No new switch.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_ENGINE_DIR = Path(__file__).resolve().parents[2] / 'engine'     # zeus_ai/engine, where settings.py lives
try:
    if str(_ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(_ENGINE_DIR))
    import settings as _settings
    _DEFAULT_ENGINE, _DEFAULT_PROFILE = Path(_settings.ENGINE_DIR), Path(_settings.PROFILE_DIR)
except Exception:                                   # settings unreadable: the in-tree engine and the default profile
    _DEFAULT_ENGINE, _DEFAULT_PROFILE = _ENGINE_DIR, Path.home() / '.hermes' / 'profiles' / 'zeus'
ENGINE = Path(os.environ.get('LIFE_ENGINE_DIR') or _DEFAULT_ENGINE)
PREFIXES = ('life-work', 'life-exec', 'life-sim')     # also 'life-work retry: ...' and 'life-work-granted: ...'
GRANTED = re.compile(r'^life-(work|sim)-granted( retry)?:')
SESSION = re.compile(r'^cron_([0-9a-f]{8,})_\d{8}_\d{6}')
STOPPED = ('Stopped before it ran: this background prep job cannot send, change, book, pay for or delete anything, '
           'cannot save to memory, and cannot start other jobs. Do the free prep, put the exact draft or the exact '
           'step into your result, and stop there. The owner gives their yes later in chat. ')
PRIVATE = ('Stopped before it ran: this background prep job cannot read the owner\'s private text threads with other '
           'people. Work from the item you were given. ')

ATTEMPT = re.compile(r'\[a_[0-9a-f]{12}\]\s*$')     # the mark a booked job carries at the end of its name (engine: integrity.tag)
CHANGED = ('Stopped before it ran: this background job was stopped or changed after it was booked. Do nothing more: '
           'make no more tool calls, send and change nothing, and end now. ')
HOLD = 'import sys; sys.path.insert(0, sys.argv[1]); import integrity; integrity._job_hold(sys.argv[2], sys.argv[3], {"why": sys.argv[4]})'
_held = set()     # jobs whose failed question is already written down by this process

_audit = {'mtime': None, 'module': None}
_jobs = {'mtime': None, 'names': {}}


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get('HERMES_HOME') or _DEFAULT_PROFILE)


def _life(home: Path) -> Path:
    return Path(os.environ.get('LIFE_HOME') or home / 'workspace/life-engine')


def audit_rules():
    """The engine's own audit module, loaded by path and reloaded when the file changes, so a fix to the audit
    rules reaches this guard without a restart."""
    path = ENGINE / 'jobaudit.py'
    mtime = path.stat().st_mtime
    if _audit['module'] is None or _audit['mtime'] != mtime:
        spec = importlib.util.spec_from_file_location('life_work_guard_jobaudit', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _audit.update(mtime=mtime, module=module)
    return _audit['module']


def job_name(job_id: str, home: Path) -> str:
    path = home / 'cron/jobs.json'
    mtime = path.stat().st_mtime
    if _jobs['mtime'] != mtime:
        data = json.loads(path.read_text())
        rows = data.get('jobs', data) if isinstance(data, dict) else data
        _jobs.update(mtime=mtime, names={str(j.get('id')): str(j.get('name') or '') for j in rows if isinstance(j, dict)})
    return _jobs['names'].get(job_id, '')


def judge(tool_name: str, args) -> tuple:
    """-> ('allow', '') | ('stop', detail) | ('private', detail), by the audit's own classify()."""
    rules = audit_rules()
    kind, detail = rules.classify(str(tool_name or ''), json.dumps(args if isinstance(args, dict) else {}, default=str))
    if kind == 'write' and not str(detail).startswith(rules.OWN_LIST):
        return 'stop', detail
    if kind == 'private':
        return 'private', detail
    return 'allow', ''


FAIL_SAFE = 'guard-fail-safe-on'
FAILED = ('Stopped before it ran: the safety check for background jobs is being repaired, so sends, purchases, deletes '
          'and new jobs wait for now. Do the free prep, put the exact draft or step into your result, and stop there. ')
_RISKY_WORDS = frozenset((
    'send', 'sendmail', 'reply', 'forward', 'post', 'share', 'rsvp', 'accept', 'decline', 'invite', 'publish', 'tweet',
    'delete', 'trash', 'remove', 'destroy', 'purge', 'drop', 'unsubscribe', 'erase', 'wipe',
    'buy', 'purchase', 'order', 'checkout', 'pay', 'payment', 'charge', 'book', 'reserve', 'subscribe', 'transfer'))
_STARTS_WORK = frozenset(('cronjob', 'delegate_task'))     # a new job would run outside this guard
_SHELL_RISK = re.compile(
    r"(?:^|[\s;&|(`'\"])(?:sudo\s+)?(?:rm|rmdir|srm|shred|trash|unlink)\s|\bosascript\b|\bsendmail\b|\bmail\s+-s\b"
    r"|\b(?:imsg|hermes|wacli)\s+send\b|\bgit\s+push\b|\bdrop\s+(?:table|database)\b|\bdelete\s+from\b"
    r"|\bcurl\b[^|;\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data|\s-d\s|--form|\s-F\s)", re.I)
_PRIVATE_READ = re.compile(r"\bimsg\s+(?:history|chats|search|watch)\b|chat\.db|\bwacli\b", re.I)   # the owner's texts with other people
_SHELL_TOOLS = frozenset(('terminal', 'bash', 'shell', 'execute_code', 'COMPOSIO_REMOTE_BASH_TOOL', 'COMPOSIO_REMOTE_WORKBENCH'))
_BROWSER_ACT = re.compile(r'click|type|press|fill|select|submit|key', re.I)
_BROWSER_RISK = re.compile(
    r"place\s*(?:your\s*)?order|buy\s*(?:it\s*)?now|check\s*-?out|purchase|\bpay\b|pay\s*now|submit|confirm|book\s*now|"
    r"\breserve\b|\bsend\b|\bdelete\b|\bremove\b|\bpost\b|add\s*to\s*cart|subscribe", re.I)


def _words(text: str) -> set:
    return {w for w in re.split(r'[^a-z0-9]+', re.sub(r'([a-z])([A-Z])', r'\1_\2', text).lower()) if w}


def _names_in(value, found: list, depth: int = 0):
    """Tool names a wrapper carries (Composio tool_slug and the like), at most a few levels deep."""
    if depth > 4:
        return
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in ('tool_slug', 'slug', 'tool', 'tool_name', 'name', 'action') and isinstance(inner, str) and len(inner) < 120:
                found.append(inner)
            else:
                _names_in(inner, found, depth + 1)
    elif isinstance(value, list):
        for inner in value[:50]:
            _names_in(inner, found, depth + 1)


def fail_safe(tool_name, args, depth: int = 0) -> str:
    """The stand-alone check used only when the guard itself broke: '' = let it run, else why it is stopped.
    Plain words and patterns only, nothing loaded from the engine, so whatever broke the guard cannot break this."""
    name = str(tool_name or '')
    bare_name = name.split('__')[-1]
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {'text': args}
    args = args if isinstance(args, dict) else {}
    text = json.dumps(args, default=str)[:20000]
    inner = args.get('calls') if bare_name == 'tool_call' else None
    if bare_name == 'tool_call' and depth < 3:          # the wrapper: judged by every call it carries
        items = inner if isinstance(inner, list) else [args]
        for c in items[:50]:
            if not isinstance(c, dict):
                continue
            why = fail_safe(c.get('name') or c.get('tool') or c.get('tool_name') or '', c.get('arguments', c.get('args')), depth + 1)
            if why:
                return why
        hit = _SHELL_RISK.search(text.replace('\\n', '\n'))      # a shape we do not know: the strict way
        return f'tool_call: {hit.group(0).strip()[:80]}' if hit else ''
    hit = _PRIVATE_READ.search(text)
    if hit:
        return f'private: {hit.group(0)}'
    if bare_name in _STARTS_WORK:
        return f'{bare_name}: starts other work'
    if bare_name in _SHELL_TOOLS:
        cmd = args.get('command') or args.get('code') or args.get('cmd')
        hit = _SHELL_RISK.search(cmd if isinstance(cmd, str) else text) or _SHELL_RISK.search(text.replace('\\n', '\n'))
        return f'{bare_name}: {hit.group(0).strip()[:80]}' if hit else ''
    if 'browser' in bare_name.lower():
        if _BROWSER_ACT.search(bare_name):
            hit = _BROWSER_RISK.search(text)
            return f'{bare_name}: {hit.group(0)[:60]}' if hit else ''
        return ''
    names = [bare_name]
    _names_in(args, names)
    for each in names:
        risky = _words(each) & _RISKY_WORDS
        if risky:
            return f'{each[:80]}: {", ".join(sorted(risky))}'
    return ''


def _raw_job_name(job_id: str, home: Path) -> str:
    """Job name straight from the text of jobs.json, for when it does not parse (for example mid-write)."""
    raw = (home / 'cron/jobs.json').read_text(errors='replace')
    m = re.search(r'"id"\s*:\s*"' + re.escape(job_id) + r'"\s*,\s*"name"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)   # Hermes writes name right after id
    return m.group(1) if m else ''


def _log(life: Path, row: dict):
    try:
        with open(life / 'guard-log.jsonl', 'a') as fh:
            fh.write(json.dumps({'at': time.time(), **row}, default=str, ensure_ascii=False) + '\n')
    except OSError:
        pass


def guard(tool_name='', args=None, session_id='', **_):
    try:
        verdict = helper_guard(tool_name, args, session_id)
        if verdict is not NOT_HELPER:
            return verdict
    except Exception as error:           # a helper call the guard cannot judge is not let through
        try:
            if is_helper():
                _log(_life(_home()), {'helper': True, 'tool': str(tool_name)[:80], 'verdict': 'guard broke', 'error': type(error).__name__, 'stopped': True})
                return {'action': 'block', 'message': HELPER_STOPPED}
        except Exception:
            return {'action': 'block', 'message': HELPER_STOPPED}
    blocked = fence_before(tool_name, args, session_id, _.get('tool_call_id', ''))
    if blocked:
        return blocked
    try:
        return _guard(tool_name, args, session_id)
    except Exception as error:     # see the module note: the stand-alone check, or the old let-it-run rule
        return _when_broken(error, tool_name, args, session_id)


# ---- Effect fence: see fence.py ------------------------------------------------------------------------------------
_fence = {'module': None}


def _fence_mod():
    if _fence['module'] is None:
        spec = importlib.util.spec_from_file_location('life_work_guard_fence', Path(__file__).with_name('fence.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _fence['module'] = module
    return _fence['module']


def fence_before(tool_name, args, session_id, tool_call_id):
    """A write the audit sees (not the owner's memory, not the own work list) is written down before it runs; the same exact
    write while an earlier one was cut off is refused. Any trouble in here lets the call through."""
    try:
        if not _ours():
            return None
        life = _life(_home())
        F = _fence_mod()
        if not F.on(life) or (life / 'guard-off').exists():
            return None
        rules = audit_rules()
        kind, detail = rules.classify(str(tool_name or ''), json.dumps(args if isinstance(args, dict) else {}, default=str))
        if kind != 'write' or str(detail).startswith(rules.OWN_LIST):
            return None
        message = F.before(life, tool_name, args, detail, session_id=session_id, tool_call_id=tool_call_id)
        if message and (life / 'guard-shadow').exists():
            return None
        return {'action': 'block', 'message': message} if message else None
    except Exception as error:
        try:
            _log(_life(_home()), {'fence': True, 'verdict': 'fence broke, call allowed', 'error': type(error).__name__, 'tool': str(tool_name)[:80]})
        except Exception:
            pass
        return None


def fence_after(tool_name, args, session_id, tool_call_id, status):
    try:
        if _fence['module'] is not None and _ours():
            _fence['module'].after(_life(_home()), tool_name, args, session_id=session_id, tool_call_id=tool_call_id, status=status)
    except Exception:
        pass


def _when_broken(error, tool_name, args, session_id):
    home = life = None
    row = {'error': type(error).__name__, 'tool': str(tool_name)[:80], 'session': str(session_id)[:80]}
    try:
        home = _home()
        life = _life(home)
        m = SESSION.match(str(session_id or ''))
        if m and (life / 'commit-integrity-on').exists() and not (life / 'guard-off').exists():
            # commit integrity: a broken guard still asks the job gate for a marked life job, before anything below may allow the call
            try:
                name = job_name(m.group(1), home)
            except Exception:
                name = _raw_job_name(m.group(1), home)
            if name.startswith(PREFIXES):
                try:
                    held = job_gate(life, m.group(1), name)
                except Exception as inner:
                    held = f'the check could not be made ({type(inner).__name__})'
                if held:
                    _log(life, {**row, 'job': m.group(1), 'name': name, 'verdict': 'stopped or changed after booking', 'detail': held, 'stopped': True})
                    return {'action': 'block', 'message': CHANGED}
        if m and (life / FAIL_SAFE).exists():
            try:
                name = job_name(m.group(1), home)
            except Exception:
                name = _raw_job_name(m.group(1), home)
            if name.startswith(PREFIXES) and not (life / 'guard-off').exists():
                row.update(job=m.group(1), name=name)
                try:
                    why = fail_safe(tool_name, args)
                except Exception as inner:                # even the plain check broke: stop, this is a known life job
                    why = f'fail-safe check broke too ({type(inner).__name__})'
                granted = bool(why) and bool(GRANTED.match(name)) and not why.startswith('private')
                shadow = (life / 'guard-shadow').exists()
                stop = bool(why) and not granted and not shadow
                row.update(verdict='fail-safe stop' if why else 'fail-safe allow', detail=why[:300], stopped=stop)
                if granted:
                    row['verdict'] = 'fail-safe: allowed by standing OK'
                _log(life, row)
                return {'action': 'block', 'message': PRIVATE if why.startswith('private') else FAILED} if stop else None
    except Exception as outer:
        row['also'] = type(outer).__name__
    try:
        _log(life or _life(home or _home()), row)
    except Exception:
        pass
    return None


def job_gate(life: Path, job_id: str, name: str) -> str:
    """'' = go on (also: no switch, or a job booked without an attempt mark). Anything else is why EVERY tool call of
    this job is refused: the engine said no, or the question failed, timed out or came back as something else."""
    if not ATTEMPT.search(name) or not (life / 'commit-integrity-on').exists():
        return ''
    try:
        asked = subprocess.run([sys.executable, '-I', '-B', str(ENGINE / 'integrity.py'), 'job-gate', str(life), job_id, name],
                               capture_output=True, text=True, timeout=10)
        got = json.loads(asked.stdout)
        if asked.returncode == 0 and got.get('go') is True:
            return ''
        if asked.returncode == 0 and got.get('go') is False:
            return str(got.get('why') or 'the engine said no')[:300]      # answered: the engine wrote its own hold where one is due
        why = 'the check gave no answer'
    except Exception as error:
        why = f'the check could not be made ({type(error).__name__})'
    try:      # the question itself failed: the engine's own hold line (once), so the worker starts the item again instead of using what this run answers
        if job_id not in _held and subprocess.run([sys.executable, '-I', '-B', '-c', HOLD, str(ENGINE), str(life), job_id, why], capture_output=True, timeout=10).returncode == 0:
            _held.add(job_id)
    except Exception:
        pass
    return why


def _guard(tool_name, args, session_id):
    m = SESSION.match(str(session_id or ''))
    if not m:
        return None
    home = _home()
    name = job_name(m.group(1), home)
    if not name.startswith(PREFIXES):
        return None
    life = _life(home)
    if (life / 'guard-off').exists():
        return None
    try:
        held = job_gate(life, m.group(1), name)
    except Exception as error:      # any error in here ends as a stop, right here: never in the broken-guard path below
        held = f'the check could not be made ({type(error).__name__})'
    if held:
        _log(life, {'job': m.group(1), 'name': name, 'tool': tool_name, 'verdict': 'stopped or changed after booking', 'detail': held, 'stopped': True})
        return {'action': 'block', 'message': CHANGED}
    verdict, detail = judge(tool_name, args)
    if verdict == 'allow':
        return None
    if verdict == 'stop' and GRANTED.match(name):
        # The owner gave a standing OK for this kind of item (life grant), so its job may carry the step out.
        # Every such action is written down. The owner's private text threads stay off limits even here.
        _log(life, {'job': m.group(1), 'name': name, 'tool': tool_name, 'verdict': 'allowed by standing OK', 'detail': str(detail)[:300], 'stopped': False})
        return None
    shadow = (life / 'guard-shadow').exists()
    _log(life, {'job': m.group(1), 'name': name, 'tool': tool_name, 'verdict': verdict, 'detail': str(detail)[:300], 'stopped': not shadow})
    if shadow:
        return None
    return {'action': 'block', 'message': PRIVATE if verdict == 'private' else STOPPED}


_clockwork = {'mtime': None, 'module': None}


def owner_clock():
    """The engine's ownertime module, loaded by path and reloaded when the file changes (like audit_rules)."""
    path = ENGINE / 'ownertime.py'
    mtime = path.stat().st_mtime
    if _clockwork['module'] is None or _clockwork['mtime'] != mtime:
        spec = importlib.util.spec_from_file_location('life_work_guard_ownertime', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _clockwork.update(mtime=mtime, module=module)
    return _clockwork['module']


OWNER_IDS = ('plugins', 'entries', 'zeus-work', 'settings', 'owner', 'owner_ids')     # where Zeus's config lists the owner's own addresses


def his_addresses() -> set:
    """The owner's own addresses as Zeus's config lists them, lower-cased: {'photon:+1...', 'slack:u...'}. Read each time."""
    from hermes_cli.config import load_config_readonly
    node = load_config_readonly()
    for key in OWNER_IDS:
        node = node.get(key) if isinstance(node, dict) else None
    return {str(item).strip().casefold() for item in (node or []) if isinstance(item, str) and ':' in item}


def answering_him(job) -> bool:
    """Was this `hermes send` run by Zeus's own private chat turn with the owner, and does it go to the owner? (Points
    1 and 3 in the note at the top; the clock and their last message are the engine's part.) Any doubt: False."""
    from gateway.session_context import get_session_env as fact
    if fact('HERMES_CRON_SESSION', '') == '1':
        return False                                    # a scheduled job, even one that names a chat
    platform = fact('HERMES_SESSION_PLATFORM', '').strip().casefold()
    author = fact('HERMES_SESSION_USER_ID', '').strip().casefold()
    chat = fact('HERMES_SESSION_CHAT_ID', '').strip().casefold()
    if not (platform and author and chat) or fact('HERMES_SESSION_CHAT_TYPE', '').strip().casefold() != 'dm':
        return False                                    # no live turn behind this command, or not a private chat
    his = his_addresses()
    if f'{platform}:{author}' not in his:
        return False                                    # somebody else's turn
    where, _, ref = str((job or {}).get('deliver') or '').strip().partition(':')
    where, ref = where.strip().casefold(), ref.strip().casefold()
    if where and not ref:                               # a bare platform: the profile's home channel there
        ref = os.environ.get(where.upper() + '_HOME_CHANNEL', '').strip().casefold()
    if not (where and ref):
        return False
    return f'{where}:{ref}' in his or (where, ref) == (platform, chat)


def deliver_gate(job=None, content=None, for_failure=False, surface='cron', **_):
    """Hermes asks before a scheduled result or a `hermes send` message leaves Zeus. -> {'open': True}, or
    {'deliver_after': epoch seconds} to make the whole message wait. First the answer for something Zeus started on
    its own (reply=False). Only if that says "wait", and only for `hermes send`, the one exception is tried: its own
    chat turn answering the owner (answering_him), with their message no older than the engine's REPLY_WINDOW. If that
    cannot be shown, the first answer stands and the message waits for the end of quiet hours. Trouble with the first answer: no answer at
    all, which Hermes treats as closed (a scheduled result is parked and asked about again; `hermes send` says no)."""
    try:
        clock = owner_clock()
        life = _life(_home())
        wait = clock.contact_hold(reply=False, life=life)
        if wait == clock.UNKNOWN:
            return None
    except Exception:
        return None
    if wait is None:
        return {'open': True}
    try:
        if surface == 'send' and answering_him(job) and clock.contact_hold(reply=True, life=life) is None:
            return {'open': True}
    except Exception:
        pass
    return {'deliver_after': wait}


# ---- Helpers: see the module note -----------------------------------------------------------------------------------
HELPER_CAP = 3          # helpers working at once for one chat of the owner's
NOT_HELPER = object()   # helper_guard: this call is not a helper's and not a new helper; the old rules judge it
HELPER_STOPPED = ('Stopped before it ran: a helper only looks things up. It cannot send, post, buy, book, sign in, '
                  'change or delete anything, cannot change the owner\'s work list or memory, and cannot start other jobs. Put '
                  'the exact draft or the exact step into your result and stop there; Zeus decides in chat what happens. ')
HELPER_PRIVATE = ('Stopped before it ran: a helper cannot read the owner\'s private text threads with other people. '
                  'Work from what you were given. ')
HELPER_FULL = ('Not started yet: {n} helpers are already working on the owner\'s earlier asks. This one is saved and you will be '
               'told to start it the moment one of them finishes. Do not do it yourself in this chat turn. Tell them in one '
               'short line that it starts as soon as one of the others is done. ')
POOL_FULL = ('Not started yet: the helper pool is full right now, and a helper started now would run inside this chat turn '
             'and keep the owner waiting. This one is saved and you will be told to start it when a helper finishes. Tell them '
             'in one short line that it starts in a moment. ')
WAITING_KEEP = 6 * 3600   # a waiting job older than this is dropped (the ask is stale by then)
WAITING_MAX = 5           # waiting jobs kept per chat; a sixth replaces the oldest
HELPER_DONE = re.compile(r'^\s*\[(?:ASYNC DELEGATION|IMPORTANT: \d+ background subagent delegations)')
WAITING_NOTE = ('A job the owner asked for earlier was waiting for a free helper, and one is free now. Start it now with '
                'delegate_task, with exactly these tasks: {tasks} . Then answer about the finished one as usual; '
                'mention the new one only if they would not expect it.')
_waiting: dict = {}       # chat session id -> [{'tasks': [...], 'at': time, 'his': ref}] (the owner's message ref), in memory: a restart ends running helpers too
_starting: dict = {}      # chat session id -> the owner's message ref a spawn being started belongs to (pre_tool_call -> post_tool_call)
HELPER_TURNS = 'helper-turns.jsonl'   # which of the owner's messages a helper is working for (read by the chat adapter's tapbacks)


def _his_ref() -> dict:
    """The platform, chat and message id of the turn being run (Hermes's own facts for this turn)."""
    from gateway.session_context import get_session_env as fact
    return {'platform': fact('HERMES_SESSION_PLATFORM', '').strip(), 'chat': fact('HERMES_SESSION_CHAT_ID', '').strip(),
            'message': fact('HERMES_SESSION_MESSAGE_ID', '').strip()}


def note_turn(kind, ref, **more) -> None:
    """One line in helper-turns.jsonl: started (a helper works for the owner's message), waiting (refused for room, kept),
    stopped (the owner said stop in that chat). Never raises; a missing line only means 👀 is not kept."""
    try:
        if not (ref and ref.get('chat')):
            return
        row = dict(at=time.time(), kind=kind, **ref, **more)
        path = _life(_home()) / HELPER_TURNS
        if path.exists() and path.stat().st_size > 256_000:      # keep it small: the last 500 lines are all anyone reads
            keep = path.read_text(encoding='utf-8', errors='replace').splitlines()[-500:]
            tmp = path.with_suffix('.tmp')
            tmp.write_text('\n'.join(keep) + '\n', encoding='utf-8')
            tmp.replace(path)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception:
        pass

# Never for a helper, whatever the audit rules say: signing in, account connections, desktop control, raw browser
# protocol and dialogs (a dialog can be the "confirm order" box).
HELPER_NEVER = frozenset(('computer_use', 'manage_connections', 'browser_cdp', 'browser_dialog', 'browser_vault_fill',
                          'browser_vault_save_login', 'browser_vault_unlock', 'browser_vault_enter_code'))
PROCESS_READS = frozenset(('list', 'poll', 'log', 'wait'))   # process_manage: looking at its own processes only
CODE_TOOLS = frozenset(('execute_code',))    # judged like a terminal command (the audit reads these as plain reads)
ARGV_GLUE = re.compile(r"['\"]\s*,\s*['\"]")      # the quote-comma-quote between the words of an argument list
BARE_STOP = re.compile(r"^\W*(?:stop|stop it|stop that|stop looking|stop (?:the )?(?:search|research)|cancel(?: it| that)?"
                       r"|never ?mind|nvm|nm|forget it|forget that|scratch that)\W*$", re.I)
_loaded_home = {'path': None}


def _ours() -> bool:
    """True only while the profile this plugin was loaded for is the active one (the gateway serves several)."""
    loaded = _loaded_home['path']
    return loaded is not None and _home().resolve() == loaded


def is_helper() -> bool:
    from agent.delegation_context import is_delegated_child_context
    return bool(is_delegated_child_context()) and _ours()


def helper_judge(tool_name, args) -> tuple:
    """-> ('allow', '') | ('stop', detail) | ('private', detail) for a call made inside a helper."""
    name = str(tool_name or '').split('__')[-1]
    args = args if isinstance(args, dict) else {}
    if name in HELPER_NEVER:
        return 'stop', f'{name}: never for a helper'
    if name == 'process_manage' and str(args.get('action') or '') not in PROCESS_READS:
        return 'stop', f'process_manage {str(args.get("action"))[:20]}: changes a running process'
    if name in CODE_TOOLS:
        tool_name, args = 'terminal', {'command': str(args.get('code') or args.get('command') or json.dumps(args, default=str))}
    if isinstance(args.get('command'), str):            # ["imsg", "send", ...] in code reads as: imsg send ...
        args = dict(args, command=ARGV_GLUE.sub(' ', args['command']))
    try:
        rules = audit_rules()
        kind, detail = rules.classify(str(tool_name or ''), json.dumps(args, default=str))
    except Exception as error:                          # the audit could not be read: the stand-alone check, strictly
        why = fail_safe(tool_name, args)
        return ('stop', f'audit unavailable ({type(error).__name__}): {why}') if why else ('allow', '')
    if kind == 'private':
        return 'private', detail
    if kind == 'write':                                 # the owner's work list too: a helper hands findings back, Zeus books them
        return 'stop', detail
    why = fail_safe(tool_name, args)                    # and the stand-alone check: for a helper the stricter of the two wins
    if why:                                             # (it also knows "add to cart", "subscribe" and "imsg send" in code)
        return ('private' if why.startswith('private') else 'stop'), why
    return 'allow', ''


def helper_room(args, session_id) -> str:
    """'' = a new helper may start; else why not (the cap for this chat, or a full pool)."""
    action = str((args or {}).get('action') or 'spawn').strip().lower() if isinstance(args, dict) else 'spawn'
    if action not in ('', 'spawn'):
        return ''                                       # list / steer / stop: always allowed
    from tools import async_delegation as pool
    from tools.delegate_tool import _get_max_async_children
    mine = pool._session_records(pool._ACTIVE_STATES, '', '', str(session_id or '')) if session_id else []
    if len(mine) >= HELPER_CAP:
        return HELPER_FULL.format(n=len(mine))
    with pool._records_lock:
        slots = {r.get('slot_key') or r['delegation_id'] for r in pool._records.values() if r.get('status') in pool._ACTIVE_STATES}
    if len(slots) >= _get_max_async_children():
        return POOL_FULL
    return ''


def _tasks_of(args) -> list:
    """The task list of a delegate_task spawn, in the shape delegate_task takes back."""
    args = args if isinstance(args, dict) else {}
    tasks = args.get('tasks')
    if isinstance(tasks, list) and tasks:
        return [t for t in tasks if isinstance(t, dict)]
    if args.get('goal'):
        return [{k: args[k] for k in ('goal', 'context', 'toolsets') if k in args}]
    return []


def wait_for_room(session_id, args) -> None:
    """A spawn refused for room is kept for this chat, so the next finished helper brings it back."""
    tasks = _tasks_of(args)
    if not (session_id and tasks):
        return
    now = time.time()
    rows = [r for r in _waiting.get(str(session_id), []) if now - r['at'] < WAITING_KEEP and r['tasks'] != tasks]
    his = _his_ref()
    rows.append({'tasks': tasks, 'at': now, 'his': his})
    note_turn('waiting', his)
    _waiting[str(session_id)] = rows[-WAITING_MAX:]


def started(session_id, args) -> None:
    """A spawn went ahead: it is no longer waiting (the same tasks, or else the oldest waiting one when it was offered)."""
    _starting[str(session_id or '')] = _his_ref()
    rows = _waiting.get(str(session_id or ''))
    if not rows:
        return
    tasks = _tasks_of(args)
    same = [r for r in rows if r['tasks'] == tasks]
    gone = same[0] if same else (rows[0] if rows[0].get('offered') else None)
    if gone is not None:
        rows.remove(gone)
        _starting[str(session_id or '')] = gone.get('his') or _his_ref()   # it works for the message that asked for it
    if not rows:
        _waiting.pop(str(session_id), None)


def offer_waiting(session_id, user_message, turn_display_kind):
    """pre_llm_call, on a helper's result coming back: the oldest waiting job of this chat, when there is room now."""
    if turn_display_kind != 'internal_notification' or not HELPER_DONE.match(str(user_message or '')):
        return None
    now = time.time()
    rows = [r for r in _waiting.get(str(session_id or ''), []) if now - r['at'] < WAITING_KEEP]
    if not rows:
        _waiting.pop(str(session_id or ''), None)
        return None
    _waiting[str(session_id)] = rows
    if helper_room({'tasks': rows[0]['tasks']}, session_id):
        return None                                     # still no room: the next finished helper offers it again
    rows[0]['offered'] = True
    _log(_life(_home()), {'helper': True, 'verdict': 'waiting job offered', 'session': str(session_id)[:80], 'waiting': len(rows)})
    return {'context': WAITING_NOTE.format(tasks=json.dumps(rows[0]['tasks'], ensure_ascii=False))}


def helper_guard(tool_name, args, session_id):
    """pre_tool_call for helpers and for starting one. NOT_HELPER = leave the call to the life-job rules."""
    if not _ours():
        return NOT_HELPER
    life = _life(_home())
    if (life / 'guard-off').exists():
        return None
    shadow = (life / 'guard-shadow').exists()
    if is_helper():
        verdict, detail = helper_judge(tool_name, args)
        if verdict == 'allow':
            return None
        _log(life, {'helper': True, 'tool': str(tool_name)[:80], 'verdict': verdict, 'detail': str(detail)[:300], 'stopped': not shadow})
        if shadow:
            return None
        return {'action': 'block', 'message': HELPER_PRIVATE if verdict == 'private' else HELPER_STOPPED}
    if str(tool_name or '').split('__')[-1] == 'delegate_task':
        why = helper_room(args, session_id)
        if why:
            _log(life, {'helper': True, 'tool': 'delegate_task', 'verdict': 'no room', 'detail': why[:120], 'stopped': not shadow})
            if shadow:
                return None
            wait_for_room(session_id, args)
            return {'action': 'block', 'message': why}
        started(session_id, args)
        return None
    return NOT_HELPER


def his_private_turn() -> bool:
    """Is the current turn the owner's own message in a private chat with Zeus? (the facts Hermes gives every turn)"""
    from gateway.session_context import get_session_env as fact
    if fact('HERMES_CRON_SESSION', '') == '1':
        return False
    platform = fact('HERMES_SESSION_PLATFORM', '').strip().casefold()
    author = fact('HERMES_SESSION_USER_ID', '').strip().casefold()
    if not (platform and author) or fact('HERMES_SESSION_CHAT_TYPE', '').strip().casefold() != 'dm':
        return False
    return f'{platform}:{author}' in his_addresses()


def stop_on_his_word(user_message='', session_id='', turn_display_kind=None, parent_session_id='', **_):
    """pre_llm_call: the owner's bare "stop" stops this chat's helpers (and drops its waiting jobs) by code, before the model
    answers; a helper's result coming back brings the oldest waiting job with it (offer_waiting). Returns a one-line
    note for the model only when something was stopped or a waiting job can start."""
    try:
        if parent_session_id or not _ours():
            return None
        life = _life(_home())
        if (life / 'guard-off').exists() or (life / 'guard-shadow').exists():
            return None
        if turn_display_kind is not None:
            return offer_waiting(session_id, user_message, turn_display_kind)
        if not BARE_STOP.match(str(user_message or '')) or not his_private_turn():
            return None
        from tools.async_delegation import interrupt_for_session
        stopped = interrupt_for_session(parent_session_id=str(session_id or ''), reason='stop_command_text')
        dropped = len(_waiting.pop(str(session_id or ''), []))
        note_turn('stopped', _his_ref())
        _log(life, {'helper': True, 'verdict': 'stopped on the owner\'s word', 'helpers': stopped, 'waiting_dropped': dropped,
                    'session': str(session_id)[:80]})
        if stopped or dropped:
            return {'context': f'Your {stopped + dropped} helper job(s) for the owner were stopped just now, by your setup, '
                               'because they said stop. Nothing from them will reach them. Confirm in a few words.'}
    except Exception as error:
        try:
            _log(_life(_home()), {'helper': True, 'verdict': 'helper turn hook failed', 'error': type(error).__name__})
        except Exception:
            pass
    return None


def after_tool(tool_name='', args=None, result=None, session_id='', **_):
    """post_tool_call: a helper that really started (status dispatched) is written down for the message it works for.
    Also closes this call's effect-fence row (fence.py)."""
    fence_after(tool_name, args, session_id, _.get('tool_call_id', ''), _.get('status'))
    try:
        if str(tool_name or '').split('__')[-1] != 'delegate_task' or not _ours() or is_helper():
            return None
        his = _starting.pop(str(session_id or ''), None)
        body = json.loads(result) if isinstance(result, str) else (result or {})
        if isinstance(body, dict) and body.get('status') == 'dispatched' and body.get('delegation_id'):
            note_turn('started', his or _his_ref(), delegation=str(body['delegation_id']))
    except Exception:
        pass
    return None


def register(ctx):
    _loaded_home['path'] = _home().resolve()
    ctx.register_hook('pre_tool_call', guard)
    ctx.register_hook('pre_deliver', deliver_gate)
    ctx.register_hook('pre_llm_call', stop_on_his_word)
    ctx.register_hook('post_tool_call', after_tool)

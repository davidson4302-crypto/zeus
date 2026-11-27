"""replyhook: where the reply checks (replychecks.py) meet the chat plugin. Standard library only, no Hermes imports.

The plugin calls after_guard() once per reply to the owner, after its own guard has had its look. What happens next is
decided by switch files in the life folder:

  reply-checks-on        check the reply, make the two repairs code may make (delete a last sentence that is only
                         an offer or a stock phrase; em dash to comma), log the trip
  reply-checks-shadow    log only: the reply goes out exactly as it would have. Wins when both files are there.
  reply-checks-rewrite   only together with reply-checks-on: when code cannot repair a finding (too long, a
                         machinery word inside a sentence that says something), ask the model for ONE rewrite. Code
                         then checks the rewrite (nothing new, nothing protected lost, clean on a second check, the
                         guard would leave it alone). A rewrite that fails any of that is thrown away and the
                         code-repaired reply goes out. No second try.
  (none of them)         the plugin never loads this file

Optional settings: reply-checks.json in the life folder, for example {"chat_words": 50}. A missing file means the
defaults in replychecks.DEFAULTS. A file that cannot be read also means the defaults, and the log says so.

Every trip is one line appended to reply-checks.log.jsonl in the life folder: what was found, what was done, the
reply before and after. Nothing here is ever shown to the owner, and nothing here may cost them a reply: the plugin wraps
the call, and a log that cannot be written never blocks the repaired reply.
"""
from __future__ import annotations

import contextvars
import importlib.util
import json
import os
import threading
import time
import sys
from pathlib import Path


def _settings():
    mod = sys.modules.get('settings')
    if mod is not None and hasattr(mod, 'OWNER_FIRST'):
        return mod
    spec = importlib.util.spec_from_file_location('settings', Path(__file__).resolve().parents[2] / 'engine' / 'settings.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return sys.modules.setdefault('settings', mod)


_CFG = _settings()
OWNER = _CFG.OWNER_FIRST if _CFG.OWNER_FIRST != 'the owner' else 'the owner'

ON, SHADOW, REWRITE = 'reply-checks-on', 'reply-checks-shadow', 'reply-checks-rewrite'
SETTINGS, LOG = 'reply-checks.json', 'reply-checks.log.jsonl'
LOG_MAX = 2_000_000          # bytes; past this the log is moved aside once (reply-checks.log.jsonl.1) and started fresh
# The plugin's reply hook is abandoned by Hermes after 30 seconds, and then the reply goes out with NO guard at all
# (hermes_cli/plugins_dispatch.py: _HOOK_CALLBACK_TIMEOUT_SECS, transform_llm_output is in _HOOK_TIMEOUT_BOUNDED_HOOKS).
# So the one rewrite gets a hard wall well inside that, enforced here whatever the model call does.
REWRITE_SECONDS = 12

_RC = None


def checks():
    """replychecks.py, loaded from beside this file (the plugin is loaded by path, so a plain import would not find it)."""
    global _RC
    if _RC is None:
        spec = importlib.util.spec_from_file_location('life_items_replychecks', Path(__file__).with_name('replychecks.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RC = mod
    return _RC


def mode_of(life_home) -> str:
    """'shadow', 'on' or '' from the switch files."""
    life_home = Path(life_home)
    return 'shadow' if (life_home / SHADOW).exists() else 'on' if (life_home / ON).exists() else ''


def load_settings(life_home):
    """-> (settings, problem). No file is normal. A file that cannot be read is a problem to log, not a reason to stop."""
    path = Path(life_home) / SETTINGS
    if not path.exists():
        return checks().settings_from(None), ''
    try:
        given = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(given, dict):
            raise ValueError('not a set of settings')
        seconds = given.get('rewrite_seconds')
        out = checks().settings_from(given)
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 1 <= seconds <= 20:
            out['rewrite_seconds'] = seconds
        return out, ''
    except (OSError, ValueError) as e:
        return checks().settings_from(None), f'{SETTINGS} could not be read ({type(e).__name__}); the defaults were used'


def log(life_home, record: dict) -> bool:
    """One line per trip. True when it was written."""
    try:
        path = Path(life_home) / LOG
        if path.exists() and path.stat().st_size > LOG_MAX:
            os.replace(path, path.with_name(LOG + '.1'))
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        return True
    except OSError:
        return False


# ---- the one rewrite -----------------------------------------------------------------------------------------------
SYSTEM = (f'You are fixing one text message that an assistant is about to send to {OWNER}, the person it works for. '
          'Return only the fixed message: no explanation, and no quotation marks around it.')
WHAT = {'long': 'It is {count} words. Make it {cap} words or fewer. Drop what they did not ask about and what they already know. Keep what they have to act on.',
        'machinery': 'It talks about how the assistant works inside ("{words}"). Say the same thing the way a person would, without those words.',
        'receipt': 'It reports how much was read ("{words}"). Leave the count out and keep what was found.',
        'offer': 'It has an offer of help or asks permission to help ("{words}"). Take that out, and do not put another offer in its place.',
        'stock': 'It has a stock assistant phrase ("{words}"). Take it out.',
        'heading': 'It has a heading, a label or bold ("{words}"). Make it plain lines of text.',
        'em_dash': 'It uses a dash as punctuation. Use a comma or a full stop.',
        'order': 'It opens with what needs their word. Put the finished news they have not heard first, one short line each, '
                 'then what needs their word in one line.'}


def rewrite_messages(reply: str, findings: list, asked: str, keep=()) -> list:
    """The short, plain instruction for the one rewrite. keep = the pieces code will insist on finding word for word."""
    lines = []
    for f in findings:
        line = '- ' + WHAT[f['kind']].format(words=f.get('words', ''), count=f.get('count', ''), cap=f.get('cap', ''))
        if line not in lines:
            lines.append(line)
    user = ((f'{OWNER[:1].upper() + OWNER[1:]} wrote: {asked.strip()}\n\n' if str(asked or '').strip() else '')
            + 'The reply below is about to go to their phone. Fix only this:\n' + '\n'.join(lines) + '\n\n'
            'Change nothing else. Keep the answer to their question first. Add no new fact, number, time, name, question, '
            'greeting, offer or sign-off. Keep word for word: any question they have to answer, anything inside quotation '
            'marks, and anything that says what is not done or not confirmed.\n'
            + ('These pieces must stay exactly as written:\n' + '\n'.join('- ' + k for k in list(keep)[:12]) + '\n' if keep else '')
            + '\nTHE REPLY:\n' + reply)
    return [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': user}]


def ask_once(llm, messages: list, seconds: float):
    """-> (the model's text, '', which model answered) or (None, why not, ''). One call, with a wall clock of its own:
    when the time is up the call is left behind and the caller carries on without it. The outcome of a call that did
    not come back is unknown, and unknown means no rewrite."""
    box = {}

    def run():
        try:
            result = llm.complete(messages, max_tokens=700, timeout=seconds, purpose='reply checks: one rewrite')
            box['text'] = str(getattr(result, 'text', '') or '')
            box['model'] = '/'.join(str(x) for x in (getattr(result, 'provider', ''), getattr(result, 'model', '')) if x)
        except Exception as e:                       # the host raises its own kinds; any of them means no rewrite
            box['error'] = type(e).__name__
    # The host finds the right profile and sign-in through context variables, which a new thread does not inherit.
    worker = threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True, name='reply-checks-rewrite')
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        return None, f'the model did not answer within {seconds:g} seconds', ''
    if 'error' in box:
        return None, 'the model call failed (' + box['error'] + ')', ''
    text = box.get('text', '').strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.strip('`').strip()
    return (text, '', box.get('model', '')) if text else (None, 'the model sent back nothing', box.get('model', ''))


def slim(findings: list) -> list:
    """Findings as they go in the log: what, the words, where, what code may do, why it left it."""
    return [{k: f[k] for k in ('kind', 'words', 'where', 'fix', 'note', 'sentence') if f.get(k)} for f in findings]


def after_guard(out, text, life_home, asked='', host=None, guard=None, now=None, news=0):
    """-> what the reply hook should hand back to Hermes: a replacement reply, or `out` untouched.
    out    what the plugin's guard decided (None = it left the reply alone, else its replacement)
    text   the reply as the model wrote it
    asked  the owner's message
    host   the plugin's Hermes context; only its .llm is used, and only for the one rewrite
    guard  a function(reply) -> None when the plugin's guard would leave that reply alone"""
    mode = mode_of(life_home)
    if not mode:
        return out
    rc = checks()
    before = out if isinstance(out, str) and out else str(text or '')
    settings, problem = load_settings(life_home)
    context = {'asked': asked, 'settings': settings}
    if news:
        context['news'] = news       # finished things the owner has not heard (a rundown must lead with them)
    found = rc.check(before, context)
    if not found:
        return out
    after, did = rc.repair(before, found)
    left = [f for f in rc.check(after, context) if f.get('fix') == 'rewrite'] if after != before else [f for f in found if f.get('fix') == 'rewrite']
    record = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(now)), 'mode': mode, 'asked': str(asked or '')[:500],
              'found': slim(found), 'did': did, 'before': before}
    if problem:
        record['settings_problem'] = problem
    if left:
        # Style notes are observational. Keep deterministic fixes, and leave factual and promise checks to their owners.
        record['rewrite'] = {'for': slim(left), 'asked_for': False, 'why_not': 'style checks do not call a model'}
    record['after'] = after
    record['sent'] = before if mode == 'shadow' else after
    record['changed'] = record['sent'] != before
    log(life_home, record)
    if mode == 'shadow' or after == before:
        return out
    return after

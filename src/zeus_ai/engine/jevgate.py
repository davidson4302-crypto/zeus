"""Quiet check for the research wake gate: a small yes/no classifier that may call an hour quiet.

Python's wake gate (research.wake_reasons) wakes research for anything new. When the ONLY reasons are new items that can be
judged on their own words (new mail, mail the owner sent, new or changed calendar entries, watched pages), this asks an
optional outside classifier one small yes/no question per item: "does this need the assistant to do something or tell the
owner soon?". The hour is a quiet skip only when the classifier says no with at least QUIET_SURE certainty for EVERY item.
Anything else wakes research as usual. Every failure (not configured, no key, network, timeout, bad answer, too many items)
wakes research: the check can only ever remove a wake, never add a miss on its own say-so.

The classifier is off unless configured:
    [features.quiet_check]
    endpoint = "https://..."      # HTTPS endpoint that accepts {state, model, questions} and answers a probability
    model = "..."                 # model name sent with each request
    key_file = "~/.config/zeus/quiet_check_key"   # or env ZEUS_QUIET_CHECK_KEY

Privacy: only the item's header line, up to BODY_CHARS of its text and two plain flags are sent. Never the Facts JSON,
memory, people pages or chat.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import settings

ENDPOINT = str(settings.get('features.quiet_check.endpoint', '') or '')
MODEL = str(settings.get('features.quiet_check.model', '') or '')
QUIET_SURE = float(settings.get('features.quiet_check.quiet_sure', 0.90))   # quiet only when at least this sure
BODY_CHARS = 600
MAX_ITEMS = 25              # more than this in one hour is a busy hour: research runs
TIMEOUT = 20
KEY_FILE = Path(os.path.expanduser(str(settings.get('features.quiet_check.key_file', '~/.config/zeus/quiet_check_key'))))

# wake reasons (the sentence endings research.WAKE_ON writes) that the classifier may judge. Every other reason always wakes.
JUDGEABLE = ('new mail', 'new messages he sent', 'new or changed calendar entries', 'watched pages that changed')

QUESTION = {
    'type': 'noul',
    'instructions': (f'{settings.OWNER_FIRST} has a personal assistant that reads their email and calendar. Does this item '
                     f'need the assistant to do something for {settings.OWNER_FIRST}, remember a promise, prepare for '
                     'something, or tell them about it soon?'),
    'criteria': {
        'true': ('A real person or business needs a reply or action from them, a deadline, bill, delivery, booking, travel, '
                 'appointment, medical, legal, money or security matter, a new or changed meeting they attend, or something '
                 'they promised to do'),
        'false': ('A newsletter, marketing, promotion, digest, automatic notification with nothing to do, a receipt for '
                  'something already finished, a note they already handled, or a routine calendar entry with nothing new'),
    },
}


def judgeable_only(reasons: list) -> bool:
    """True when every wake reason is one the classifier may judge (and there is at least one)."""
    return bool(reasons) and all(any(str(r).endswith(k) for k in JUDGEABLE) for r in reasons)


def api_key():
    k = os.environ.get('ZEUS_QUIET_CHECK_KEY', '').strip()
    if k:
        return k
    try:
        return KEY_FILE.read_text().strip() or None
    except OSError:
        return None


_HEAD = re.compile(r'^## \[source:(\d+)\] (.*)$', re.M)


def items_from_observations(text: str) -> list:
    """The new items research would read, as (source id, short text). Only sections under the 'new since' and 'sent by
    the owner' headings; each is cut to its header plus BODY_CHARS of body; the Facts JSON is reduced to two plain flags."""
    out = []
    heads = list(_HEAD.finditer(text))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end]
        body = re.split(r'\n# ', body)[0]                      # stop at the next top-level heading
        flags = []
        fm = re.search(r'^Facts: (\{.*)$', body, re.M)
        if fm:
            f = fm.group(1)
            if '"he_already_replied": true' in f:
                flags.append('The owner already replied to this.')
            if '"sent_by_him": true' in f:
                flags.append('The owner sent this themselves.')
            body = body[:fm.start()]
        body = re.sub(r'\n{3,}', '\n\n', body).strip()
        if len(body) > BODY_CHARS:
            body = body[:BODY_CHARS] + ' ...'
        out.append((m.group(1), (m.group(2).strip() + '\n' + body + ('\n' + ' '.join(flags) if flags else '')).strip()))
    return out


def _ask(key: str, state: str):
    body = json.dumps({'state': state, 'model': MODEL, 'questions': {'needs_attention': QUESTION}}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        ans = json.loads(r.read().decode())
    v = float(ans['answers']['needs_attention']['noul'])
    if not 0.0 <= v <= 1.0:
        raise ValueError('noul out of range')
    return v, ans.get('model'), (ans.get('usage') or {}).get('input_tokens', 0)


def check(reasons: list, observations_text: str, ask=None) -> dict:
    """Decide whether this hour can be a quiet skip. Returns {'quiet': bool, 'why': str, 'scores': {source: p}, ...}.
    `ask(state) -> (p, model, tokens)` is the classifier call (fixed answers in tests)."""
    res = {'quiet': False, 'scores': {}, 'model': None, 'tokens': 0}
    if not judgeable_only(reasons):
        res['why'] = 'a wake reason the quiet check may not judge'
        return res
    items = items_from_observations(observations_text)
    if not items:
        res['why'] = 'no readable new items to judge'          # a count with no matching item is never treated as quiet
        return res
    if len(items) > MAX_ITEMS:
        res['why'] = f'{len(items)} new items is a busy hour'
        return res
    if ask is None:
        if not ENDPOINT:
            res['why'] = 'the quiet check is not configured'
            return res
        key = api_key()
        if not key:
            res['why'] = 'no quiet-check key'
            return res
        ask = lambda state: _ask(key, state)
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            got = list(pool.map(lambda it: (it[0], ask(it[1])), items))
    except Exception as e:                                   # any failure wakes research, exactly as without this check
        res['why'] = f'the quiet check failed ({type(e).__name__})'
        return res
    for sid, (p, model, tok) in got:
        res['scores'][sid] = round(p, 3)
        res['model'] = model
        res['tokens'] += int(tok or 0)
    top = max(res['scores'].values())
    res['quiet'] = top <= 1 - QUIET_SURE
    res['why'] = (f'the quiet check was at least {int(QUIET_SURE*100)}% sure none of the {len(items)} new items needs anything'
                  if res['quiet'] else f'the quiet check thinks an item may need attention (top {top:.2f})')
    return res

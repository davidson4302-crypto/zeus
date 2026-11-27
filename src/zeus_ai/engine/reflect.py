"""The rules list of standing ways of working (reflect.Rules) and its change log.

`life rules` reads the list, `life rule yes|no|retire` answers or takes back a rule already on it, and conflicts.py
sets a rule aside when the owner settles a clash. Nothing new is added to it by code: no step proposes rules from mail.
"""

import hashlib, json, os, re, tempfile, time

from datetime import datetime

OPEN_STATES = ('proposed', 'confirmed', 'declined', 'unanswered', 'noted', 'adopted')

class Rules:
    """The rules list and its change log. Two plain files, written whole (temp file then rename)."""

    def __init__(self, folder):
        self.folder = str(folder)
        self.path = os.path.join(self.folder, 'standing-rules.json')
        self.log = os.path.join(self.folder, 'standing-rules-log.jsonl')

    def all(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, rules, what, key, why=''):
        os.makedirs(self.folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.folder, prefix='.rules-')
        with os.fdopen(fd, 'w') as f:
            json.dump(rules, f, indent=1, default=str)
        os.replace(tmp, self.path)
        with open(self.log, 'a') as f:
            f.write(json.dumps({'at': datetime.now().astimezone().isoformat(timespec='seconds'), 'what': what, 'key': key, 'why': why}) + '\n')

    def add(self, rec):
        rules = self.all()
        if any(r.get('key') == rec['key'] and r.get('state') in OPEN_STATES for r in rules):
            return False
        rules.append(rec)
        self._save(rules, 'added as ' + rec['state'], rec['key'], rec.get('why', ''))
        return True

    def _set(self, key, state, why, **more):
        rules = self.all()
        for r in rules:
            if r.get('key') == key and r.get('state') in OPEN_STATES:
                r.update(state=state, **more)
                self._save(rules, state, key, why)
                return r
        return None

    def confirm(self, key, his_words, message_id, **more):
        """The owner's yes. Needs the id of a real message of theirs (checked by the caller against their chat, as `life grant` does)."""
        if not his_words or message_id in (None, ''):
            raise ValueError('a rule is only confirmed with the owner\'s own words and the message they came from')
        return self._set(key, 'confirmed', 'the owner said yes', his_words=his_words, message_id=message_id,
                         confirmed_at=datetime.now().astimezone().isoformat(timespec='seconds'), **more)

    def decline(self, key, his_words=''):
        return self._set(key, 'declined', 'the owner said no', his_words=his_words)

    def unanswered(self, key, why='no answer; never asked again'):
        return self._set(key, 'unanswered', why)

    def amend(self, key, what, **more):
        """Add facts to a rule without changing its state (for example: its line is now in the preferences file)."""
        rules = self.all()
        for r in rules:
            if r.get('key') == key and r.get('state') in OPEN_STATES:
                r.update(**more)
                self._save(rules, what, key)
                return r
        return None

    def keep_as_note(self, key, why):
        """A proposal that must not be put to the owner after all (its wording cannot be shown to them): kept, never asked."""
        return self._set(key, 'noted', why)

    def retire(self, key, why):
        """The owner said "don't do that again", or the rule fired wrong. Kept in the file as retired, with the reason."""
        was = next((r.get('state') for r in self.all() if r.get('key') == key and r.get('state') in OPEN_STATES), None)
        return self._set(key, 'retired', why, retired_from=was)

    def to_ask(self, limit=1):
        """Proposals not yet put in front of the owner. At most one per note. Only ever a rule with a step that needs their yes."""
        return [r for r in self.all() if r.get('state') == 'proposed' and r.get('needs_his_yes') and not r.get('asked_at')][:limit]

    def mark_asked(self, key):
        rules = self.all()
        for r in rules:
            if r.get('key') == key and r.get('state') == 'proposed':
                r['asked_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
                self._save(rules, 'asked', key)
                return True
        return False


# ==== offers: the rule-offer helpers ====
#
# The offer rows an older offer left in the ledger (offer_rows, read by conflicts.py so a clash question never shares a day with
# one), and a confirmed rule's line in the preferences `rules` list (pref_text / pref_add / pref_remove), which
# `life rule yes | retire` (lifecli.py) still use. Nothing here runs from the tick, calls a model or sends.
import json

import re

import time

from datetime import datetime

from pathlib import Path

from zoneinfo import ZoneInfo

import settings

PACIFIC = ZoneInfo(settings.OWNER_TZ)   # the owner's home zone

PREF_RULES_MAX = 40          # same cap the quiet pass keeps on the preferences rules list (quietpass.MAX_RULES)

PREF_TEXT_MAX = 900

OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')

def _sentence(text) -> str:
    t = ' '.join(str(text or '').split())
    return t if not t or t[-1] in '.!?' else t + '.'

def offer_rows(con) -> list:
    """Every offer item in the ledger, oldest first: (row, payload). Told apart by what is in them, not by their key."""
    rows = con.execute("SELECT key,state,surface_at,first_seen,payload FROM opps WHERE json_extract(payload,'$.offer.rule_key') IS NOT NULL "
                       'ORDER BY first_seen').fetchall()
    return [(r, json.loads(r['payload'])) for r in rows]

ADOPTED_LABEL = "Learned from the owner's mail, not something they said: "

def pref_text(rec: dict) -> str:
    """The one plain line the noticer and the background jobs get for a confirmed rule."""
    parts = ['When this comes in: ' + _sentence(rec.get('when')), 'What to do: ' + _sentence(rec.get('action'))]
    if str(rec.get('stay_quiet_when') or '').strip():
        parts.append('Stay quiet when: ' + _sentence(rec['stay_quiet_when']))
    text = re.sub(r'\s*[—–]\s*', ', ', ' '.join(p for p in parts if p))
    if len(text) > PREF_TEXT_MAX:       # never cut inside a sentence
        cut = max(text.rfind('. ', 0, PREF_TEXT_MAX), text.rfind('? ', 0, PREF_TEXT_MAX), text.rfind('! ', 0, PREF_TEXT_MAX))
        text = text[:cut + 1] if cut > 0 else text[:PREF_TEXT_MAX]
    return text

def _prefs(life):
    """-> (path, dict) or (path, None) when the file is there but cannot be read. Never saved over in that case:
    that would wipe what the owner told Zeus to stop raising (same care as quietpass.add_rule)."""
    p = Path(life) / 'preferences.json'
    if not p.exists():
        return p, {}
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return p, None
    return p, (d if isinstance(d, dict) else None)

def _prefs_save(p: Path, d: dict):
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    tmp.replace(p)

def pref_add(life, text: str, quote: str, message_id, now: float | None = None) -> bool:
    """Append one rule in the shape the quiet pass writes (text, quote, message_id, at). False = file unreadable, nothing written."""
    p, d = _prefs(life)
    if d is None:
        return False
    have = [r for r in (d.get('rules') or []) if isinstance(r, dict)]
    if not any(r.get('text') == text for r in have):
        have.append({'text': text, 'quote': quote, 'message_id': message_id,
                     'at': datetime.fromtimestamp(now or time.time(), PACIFIC).strftime('%Y-%m-%dT%H:%M:%S%z')})
    d['rules'] = have[-PREF_RULES_MAX:]
    _prefs_save(p, d)
    return True

def pref_remove(life, text: str):
    """Take a retired rule's line out of the preferences rules list. -> how many were removed, or None when the file is unreadable."""
    p, d = _prefs(life)
    if d is None:
        return None
    have = [r for r in (d.get('rules') or []) if isinstance(r, dict)]
    keep = [r for r in have if not (text and r.get('text') == text)]
    if len(keep) != len(have):
        d['rules'] = keep
        _prefs_save(p, d)
    return len(have) - len(keep)

"""lastsaid: what Zeus's last message to the owner was about.

Why. Zeus tells the owner at 10:38 that the kids' art class moved to 10:30. That asks nothing. Two minutes later the
owner writes "sounds good". Jordan's question from the day before is still open, so a careless reader would take
"sounds good" as a yes to Jordan. The rule: "sounds good" is a yes only to the question Zeus just asked. A plain
"yes" is the same: it answers the ask in Zeus's last message, nothing older.

So code reads, from stored facts only, what that last message was:
  - the times things were told to them (each item's told stamp; the background note and the chat both set it),
  - the times an ask was put to them (the one-send record's "asked"),
  - Zeus's own worded replies in their chat (read-only, through chats.py).
Things sent within CLUSTER seconds of the newest one went out as one message (a morning note with three lines).

  last(con, before, store)   -> {'at', 'asks': [keys of the open asks that message was about], 'question': it asked
                                something, 'news': it told something that asked nothing, 'told': [keys of every item it
                                told]} or None when it cannot be told
                                (the chat store could not be read, nothing was ever sent). Unknown is never "news".

Standard library only. Nothing here writes anything. No model is asked.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402

CLUSTER = 90            # seconds: what went out this close together was one message
LOOK_BACK = 2 * 86400   # their chat is read this far back for Zeus's last words
# A sentence that asks for their word without a question mark ("Say yes and I'll send it.").
ASKING = re.compile(r"\?|\bsay (?:yes|the word)\b|\bwant me to\b|\bshould i\b|\bshall i\b|\bok to\b|\bgood to (?:go|send)\b"
                    r"|\byour (?:yes|ok|okay|go-ahead|call)\b", re.I)
COMMON = frozenset(('reply send answer about with from that this your they them yes for the and need needs wants asked owner '
                   'zeus told today tomorrow monday tuesday wednesday thursday friday saturday sunday january february '
                   'march april june july august september october november december').split()
                   + [w.lower() for w in str(settings.OWNER_NAME).split()] + [str(settings.AGENT_NAME).lower()])
_CHATS = None


def _chats():
    """chats.py from this same folder, loaded by path (the chat plugin loads this file outside the engine's import path)."""
    global _CHATS
    if _CHATS is None:
        spec = importlib.util.spec_from_file_location('lastsaid_chats', Path(__file__).with_name('chats.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CHATS = mod
    return _CHATS


def open_asks(con) -> dict:
    """key -> (title, ask) for everything waiting on their word: the same rule as onesend.open_asks."""
    out = {}
    for key, payload in con.execute("SELECT key,payload FROM opps WHERE state='surfaced'").fetchall():
        try:
            p = json.loads(payload)
        except (TypeError, ValueError):
            continue
        d = (p.get('packet') or {}).get('decision') or {}
        offer = isinstance(p.get('offer'), dict) and p['offer'].get('rule_key')
        if p.get('closed') or p.get('approved') or not (offer or ((d.get('ask') or d.get('ready')) and d.get('by') != 'owner')):
            continue
        out[key] = (str(p.get('title') or ''), str(d.get('ask') or ''))
    return out


def _marks(title: str, ask: str):
    """The names (capitalised words) and title words a message must use to be about this ask."""
    names = {w.lower() for w in re.findall(r"\b[A-Z][a-z]{2,}\b", title + ' ' + ask)} - COMMON
    words = {w for w in re.findall(r"[a-z]{4,}", title.lower())} - COMMON - names
    return names, words


def about(text: str, asks: dict) -> list:
    """The open asks one message of Zeus's talks about: it names one (a person, a shop) or uses two words of its title."""
    said = set(re.findall(r"[a-z]{3,}", str(text or '').lower().replace("'", '')))
    hits = []
    for key, (title, ask) in asks.items():
        names, words = _marks(title, ask)
        if names & said or len(words & said) >= 2:
            hits.append(key)
    return sorted(hits)


def _stamps(con, before: float, asks: dict) -> list:
    """(when, item key) for every told stamp, and every "asked" record of an open ask, before this time."""
    out = []
    for key, payload in con.execute('SELECT key,payload FROM opps').fetchall():
        try:
            at = float(((json.loads(payload) or {}).get('told') or {}).get('at') or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 < at < before:
            out.append((at, key))
    if con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='actions'").fetchone():
        for item, history in con.execute('SELECT item, history FROM actions').fetchall():
            if item not in asks:
                continue
            try:
                hist = json.loads(history or '[]')
            except ValueError:
                continue
            out += [(float(h['at']), item) for h in hist if h.get('state') == 'asked' and h.get('at') and 0 < float(h['at']) < before]
    return out


def last(con, before: float, store=None) -> dict | None:
    """What Zeus's last message to them before `before` was about. store = their chat store (a path), or None to leave the
    chat out (only for a caller that has no chat at all). A store that cannot be read: None (not known)."""
    asks = open_asks(con)
    events = [{'at': at, 'asks': [key] if key in asks else [], 'question': key in asks, 'news': key not in asks, 'told': [key]}
              for at, key in _stamps(con, before, asks)]
    if store is not None:
        try:
            msgs = _chats().messages(before - LOOK_BACK, until=before, path=str(store))
        except Exception:
            return None
        for m in msgs:
            if m['who'] == 'zeus':
                hits = about(m['text'], asks)
                q = bool(ASKING.search(m['text']))
                events.append({'at': float(m['at']), 'asks': hits, 'question': q, 'news': not (hits and q), 'told': []})
    if not events:
        return None
    newest = max(e['at'] for e in events)
    group = [e for e in events if e['at'] >= newest - CLUSTER]
    return {'at': newest, 'asks': sorted({k for e in group for k in e['asks']}), 'question': any(e['question'] for e in group),
            'news': any(e['news'] for e in group), 'told': sorted({k for e in group for k in e['told']})}

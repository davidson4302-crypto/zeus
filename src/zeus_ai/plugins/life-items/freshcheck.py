"""freshcheck: a date the reply gives for "the last I saw" must be true.

Why: the chat writer is handed one fact in code ("Newest thing you have on Jordan Lee: Jordan's email to the owner,
Nov 2 (12 days ago)") so it can add one natural clause like "last I saw from Jordan was the Nov 2 email, they may have
texted you since". The writer words it; code checks it ("writer says it, code checks"). A model can still copy the
wrong date off the page (a CRM date, an older email) or say "Monday" for something two weeks old.

What this does, on every reply to the owner, only with switch files `freshness-on` AND `pages-on` in the life folder:
  1. the plugin hands in the fact for the person or company the owner's message names (pages.freshness,
     question=False, so a date is checked whatever was asked). No fact: the reply goes out exactly as written.
  2. find "last seen" sentences: a cue ("last I saw", "the last thing", "latest", "most recent", "haven't heard since")
     plus the person's or company's name or a word about getting in touch (email, text, heard, wrote ...).
  3. read every date in such a sentence: "Nov 2", "11/2", a weekday, "yesterday", "3 days ago", "two weeks ago".
     It passes when one of them is the newest day and every other one is a real day on that page.
  4. a wrong one: the "last seen" part of the sentence is cut (what came before it in the sentence stays). If that
     would leave nothing of the reply, the wrong date is swapped for the true one instead. One line per trip in
     <life>/freshness-checks.log.jsonl. Any trouble: the reply goes out exactly as written.
Reading only: nothing here writes anything but its own log.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

SWITCH = 'freshness-on'
PAGES = 'pages-on'
LOG = 'freshness-checks.log.jsonl'

CUE = re.compile(r"\b(?:last (?:i|we) (?:saw|heard|have|had|got|seen)|(?:the )?last (?:thing|word|email|e-mail|message|note|"
                 r"contact|update|i saw|i heard|i have|time (?:i|we|you) heard)|(?:the )?latest (?:i|thing|email|message|word|"
                 r"note|update|i have|i saw)|(?:the )?newest|(?:the )?most recent|haven'?t (?:seen|heard)|(?:have|has) not "
                 r"(?:seen|heard)|nothing (?:new )?(?:from|since)|last heard|last seen|last saw)\b", re.I)
TOUCH = re.compile(r"\b(?:e-?mails?|emailed|messages?|notes?|texts?|texted|heard|wrote|written|word|reply|replied|calls?|called|"
                   r"in touch|from (?:him|her|them))\b", re.I)
MONTHS = {m: i for i, m in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}
MONTH_DAY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s*\d{4})?", re.I)
DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
SLASH = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
WEEKDAY = re.compile(r"\b(mon|tues|wednes|thurs|fri|satur|sun)day(?:'s)?\b", re.I)
WEEKDAYS = ('mon', 'tues', 'wednes', 'thurs', 'fri', 'satur', 'sun')
AGO = re.compile(r"\b(\d{1,2}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|a couple of|a few)\s+(days?|weeks?)\s+ago\b", re.I)
WORDS = re.compile(r"\b(today|yesterday|last week|this week|earlier this week)\b", re.I)
NUM = {'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
       'ten': 10, 'a couple of': 2, 'a few': 3}
SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")
CLAUSE = re.compile(r"\s*(?:,(?!\s*\d)|;|:(?!\d)|\s[-–—]\s)\s*")        # not "Nov 2, 2026", "12,500" or "1:30"


def on(life_home) -> bool:
    life_home = Path(life_home)
    return (life_home / SWITCH).exists() and (life_home / PAGES).exists()


def _md(at) -> tuple:
    t = datetime.fromtimestamp(float(at))
    return t.month, t.day


def _mentions(s: str, fact: dict, now: float) -> list:
    """[(start, end, ok_newest, ok_on_page)] for every date the sentence gives."""
    newest = _md(fact['at'])
    days = int(fact.get('days') or 0)
    page = set(fact.get('dates') or [])
    out = []

    def add(m, md=None, good=None):
        if md is not None:
            label = datetime(2000, md[0], md[1]).strftime('%b ') + str(md[1])
            out.append((m.start(), m.end(), md == newest, label in page))
        else:
            out.append((m.start(), m.end(), bool(good), False))

    for m in MONTH_DAY.finditer(s):
        try:
            add(m, (MONTHS[m.group(1).lower()[:3]], int(m.group(2))))
        except ValueError:
            pass
    for m in DAY_MONTH.finditer(s):
        if not any(a <= m.start() < b for a, b, *_ in out):
            try:
                add(m, (MONTHS[m.group(2).lower()[:3]], int(m.group(1))))
            except ValueError:
                pass
    for m in SLASH.finditer(s):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            add(m, (mo, d))
    for m in WEEKDAY.finditer(s):
        wd = WEEKDAYS.index(m.group(1).lower())
        add(m, good=days <= 6 and datetime.fromtimestamp(float(fact['at'])).weekday() == wd)
    for m in AGO.finditer(s):
        n = int(m.group(1)) if m.group(1).isdigit() else NUM.get(m.group(1).lower(), 1)
        n = n * 7 if m.group(2).lower().startswith('week') else n
        slack = 3 if m.group(2).lower().startswith('week') else 1
        add(m, good=abs(n - days) <= slack)
    for m in WORDS.finditer(s):
        w = m.group(1).lower()
        add(m, good={'today': days == 0, 'yesterday': days == 1, 'last week': 4 <= days <= 13,
                     'this week': days <= 6, 'earlier this week': 1 <= days <= 6}[w])
    return sorted(out)


def _about(s: str, fact: dict) -> bool:
    if TOUCH.search(s):
        return True
    words = [w for w in re.findall(r"[A-Za-z][\w'-]{2,}", str(fact.get('name') or '')) if w.lower() not in ('inc', 'llc', 'the', 'and', 'company')]
    return any(re.search(r'(?<![\w])' + re.escape(w) + r'(?![\w])', s, re.I) for w in words)


def _wrong(s: str, fact: dict, now: float):
    """-> (cue match, the mentions) when this sentence gives a "last seen" date that is not true, else None."""
    cue = CUE.search(s)
    if not cue or not _about(s, fact):
        return None
    # Only the "last seen" clause itself, or the next one when it gives no date ("Last I saw, Jordan emailed Nov 2"):
    # a date earlier in the sentence is about something else, and so is "they may have texted you today" after it.
    pieces, at = [], cue.start()
    for m in CLAUSE.finditer(s, cue.end()):
        pieces.append((at, m.start()))
        at = m.end()
    pieces.append((at, len(s)))
    got = []
    for a, b in pieces[:2]:
        got = [(x + a, y + a, n, p) for x, y, n, p in _mentions(s[a:b], fact, now)]
        if got:
            break
    if not got:
        return None
    if any(n for _, _, n, _ in got) and all(n or p for _, _, n, p in got):
        return None
    return cue, got


def _cut(s: str, cue) -> str:
    """The sentence without its "last seen" part: whatever came before that clause, ended with a full stop. '' when the
    sentence starts with it."""
    head = s[:cue.start()]
    bounds = [m.start() for m in CLAUSE.finditer(head)]
    if not bounds:
        return ''
    kept = head[:bounds[-1]].rstrip(' ,;:-–—')
    return kept + '.' if len(kept.split()) >= 3 else ''


def _swap(s: str, got: list, fact: dict) -> str:
    for a, b, n, p in sorted(got, reverse=True):
        if not (n or p):
            s = s[:a] + fact['day'] + s[b:]
    return s


def run(reply, life_home, fact=None, asked='', now=None) -> str:
    """-> the reply, with any untrue "last seen" date taken out (or, if nothing would be left, put right)."""
    try:
        if not isinstance(reply, str) or not reply.strip() or not fact or not on(life_home):
            return reply
        now = time.time() if now is None else float(now)
        trips, lines_out = [], []
        for line in reply.split('\n'):
            parts = SENT.split(line) if line.strip() else [line]
            kept = []
            for s in parts:
                bad = _wrong(s, fact, now)
                if not bad:
                    kept.append(s)
                    continue
                cue, got = bad
                short = _cut(s, cue)
                trips.append({'sentence': s, 'cut_to': short})
                if short:
                    kept.append(short)
            lines_out.append(' '.join(x for x in kept if x is not None))
        if not trips:
            return reply
        new = '\n'.join(lines_out)
        new = re.sub(r'\n{3,}', '\n\n', new).strip()
        how = 'cut'
        if len(new.split()) < 3:          # nothing left of the reply: put the date right instead of sending nothing
            how, new = 'swapped', reply
            for t in trips:
                bad = _wrong(t['sentence'], fact, now)
                if bad:
                    new = new.replace(t['sentence'], _swap(t['sentence'], bad[1], fact), 1)
        _log(life_home, {'at': time.time(), 'asked': str(asked)[:300], 'name': fact.get('name'), 'newest': fact.get('day'),
                         'how': how, 'trips': trips, 'before': reply, 'after': new})
        return new
    except Exception as e:
        _log(life_home, {'at': time.time(), 'trouble': repr(e)[:300]})
        return reply


def _log(life_home, row: dict) -> None:
    try:
        with open(Path(life_home) / LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception:
        pass

"""contactcheck: "no reply" must not be said when the contact wrote last.

Why: asked "has Jordan Lee replied?", a writer can answer "Not that I can see. The newest thing from Jordan is the
Nov 9 email ..." when that Nov 9 email WAS the reply: Jordan wrote last. The plugin hands the writer who wrote last
(pages.contact, built in code), and the writer words the answer; this file is the code check after it ("writer says
it, code checks"), in the same shape as freshcheck.py's date check.

What this does, on a reply to the owner, only with switch files `contact-facts-on` AND `pages-on` in the life folder,
only when the owner's message asks whether someone REPLIED or got back to them (pages.replied_ask), and only when the
facts say that person wrote last (their email is the newest either way):
  1. find the denial: a sentence that opens with "No", "Nope", "Not yet", "Not that I can see", "Nothing yet" or "Not
     as far as I can tell", or that says they have not replied / there is no reply, with no "since" or "new" in it
     ("nothing new since the Nov 9 email" is true and stays).
  2. an opening denial ("No, ...", "Not that I can see.") loses only that opening; a sentence that is all denial goes.
  3. one plain true sentence from the facts goes first: "Yes, Jordan replied on Nov 9." (shorter, "Yes, Jordan
     replied.", when what is left already gives that day). Nothing else in the reply is touched.
One line per trip in <life>/contact-checks.log.jsonl. No denial, or any trouble: the reply goes out exactly as written.
Reading only: nothing here writes anything but its own log.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

SWITCH = 'contact-facts-on'
PAGES = 'pages-on'
LOG = 'contact-checks.log.jsonl'

OPENING = re.compile(r"^\W*(?:no(?:pe)?|not yet|not that i (?:can )?(?:see|tell|know of)|not as far as i (?:can )?(?:see|tell|know)|"
                     r"not from what i (?:can )?see|nothing yet|nothing so far|not so far|i don'?t (?:think|see) so)\b[\s,.!;:]*", re.I)
NO_REPLY = re.compile(r"\b(?:hasn'?t|has not|haven'?t|have not|didn'?t|did not|never)\s+(?:yet\s+|actually\s+)?(?:replied|responded|answered|"
                      r"written back|wrote back|gotten back|got back|gotten in touch|got in touch|been in touch|heard back|come back)\b|"
                      r"\bno (?:reply|response|answer|word)(?: yet| back)?\b|\bnothing (?:back )?from (?:him|her|them)\b|"
                      r"\bhaven'?t heard (?:back )?from\b", re.I)
QUALIFIED = re.compile(r"\b(?:since|after|newer|new|more|again|further|else|other)\b", re.I)
SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“])")


def on(life_home) -> bool:
    life_home = Path(life_home)
    return (life_home / SWITCH).exists() and (life_home / PAGES).exists()


def fix(reply: str, fact: dict) -> tuple:
    """-> (the reply put right, [what was cut]) ; (reply, []) when it denies nothing."""
    text = str(reply or '')
    last = fact.get('last') or {}
    if not text.strip() or not last.get('theirs'):
        return text, []
    first = str(fact.get('first') or fact.get('name') or 'They')
    cuts, kept = [], []
    for s in SENT.split(text.strip()):
        flat = s.replace('’', "'")
        m = OPENING.match(flat)
        if m:
            rest = s[m.end():].strip()
            if rest and NO_REPLY.search(rest.replace('’', "'")) and not QUALIFIED.search(rest):
                cuts.append(s)                     # "No, they haven't replied." is all denial
            else:
                cuts.append(s[:m.end()].strip())   # "Not that I can see." / "No, nothing new since ..." keeps the rest
                if rest:
                    kept.append(rest[0].upper() + rest[1:])
            continue
        if NO_REPLY.search(flat) and not QUALIFIED.search(flat):
            cuts.append(s)
            continue
        kept.append(s)
    if not cuts:
        return text, []
    rest = ' '.join(kept).strip()
    day = str(last.get('day') or '')
    lead = f'Yes, {first} replied.' if day and re.search(r'\b' + re.escape(day) + r'\b', rest) else \
        (f'Yes, {first} replied on {day}.' if day else f'Yes, {first} replied.')
    return (lead + (' ' + rest if rest else '')).strip(), cuts


def run(reply, life_home, fact=None, asked='') -> str:
    """The reply, put right when it denies a message the facts show, else exactly as it came in."""
    try:
        if not isinstance(reply, str) or not reply.strip() or not fact or not on(life_home):
            return reply
        new, cuts = fix(reply, fact)
        if not cuts or new == reply:
            return reply
        try:
            path = Path(life_home) / LOG
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'asked': str(asked or '')[:300], 'who': fact.get('name'),
                                     'wrote_last': fact['last'].get('day'), 'cut': cuts, 'before': reply[:1500], 'after': new[:1500]},
                                    ensure_ascii=False) + '\n')
        except OSError:
            pass
        return new
    except Exception:
        return reply

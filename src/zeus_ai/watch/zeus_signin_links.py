"""Text the owner a phone sign-in link when a store signed Zeus out.

Run every few minutes by a Hermes job (for example "Zeus phone sign-in links") in no-agent mode: whatever this prints
is texted to the owner word for word; nothing printed = nothing sent. Only while the owner's alert window is open
(ownertime.alert_open: outside quiet hours, on the owner's clock), only while that store's sign-in is lapsed
(signins.json, written by the engine's signedin.py). State: <life>/signin-handoff-sent.json.
Switch file <life>/handoff-links-off stops it.

Back-off: a link the owner did not act on is not resent every day.
  * Something is actually waiting on that store (an errand held for this sign-in in the ledger): at most one link
    a day, and the text says what is waiting ("<Store> is still signed out, so the Thursday order is waiting.").
  * Nothing is waiting: the next link waits 1 day after the first, then 2 days, then 7 days, then every 7 days.
  * A new lapse (signed in, then lapsed again) starts over, because the state key carries the lapse number.
Switch file <life>/handoff-backoff-off sends at most once a day instead.
"""
import json, os, sqlite3, subprocess, sys, time
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / 'engine'
sys.path.insert(0, str(ENGINE))
import settings  # noqa: E402
from ownertime import alert_open  # noqa: E402

LIFE = Path(os.environ.get('LIFE_HOME') or str(settings.LIFE_HOME))
HERE = Path(__file__).resolve().parents[1] / 'signin'         # handoff.py lives here
PY = str(settings.get('signin.python', '') or sys.executable)   # the Python that runs handoff.py
SENT = LIFE / 'signin-handoff-sent.json'
GAPS = (1, 2, 7)                                   # days to wait after the 1st, 2nd, 3rd+ unanswered link


def _dates(v) -> list:
    """Old state kept one date string per key; new state keeps the list of send dates."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict) and isinstance(v.get('dates'), list):
        return [str(d) for d in v['dates']]
    return []


def waiting(site: str) -> list:
    """Titles of errands held for this site's sign-in (read-only look at the engine's ledger). Unreadable = []."""
    db = LIFE / 'ledger.sqlite3'
    if not db.exists():
        return []
    try:
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=5)) as c:
            rows = c.execute("SELECT payload FROM opps WHERE state='pending' AND json_extract(payload,'$.held_for_signin.site')=?",
                             (site,)).fetchall()
    except Exception:
        return []
    out = []
    for (p,) in rows:
        try:
            t = str(json.loads(p).get('title') or '').strip()
        except Exception:
            t = ''
        if t:
            out.append(t)
    return out


def due(dates: list, today: str, blocked: bool, backoff: bool) -> bool:
    """May a link go out today for this lapse?"""
    if today in dates:
        return False
    if not dates or blocked or not backoff:
        return True
    gap = GAPS[min(len(dates), len(GAPS)) - 1]
    last = date.fromisoformat(max(dates))
    return (date.fromisoformat(today) - last).days >= gap


def main():
    if (LIFE / 'handoff-links-off').exists():
        return
    at = datetime.fromtimestamp(time.time(), timezone.utc)
    opening = alert_open(at, life=LIFE)
    if opening > at:
        return
    try:
        sites = json.loads((LIFE / 'signins.json').read_text()).get('sites', {})
    except Exception:
        return                                     # unreadable: say nothing (the engine's own health note covers it)
    try:
        sent = json.loads(SENT.read_text())
    except Exception:
        sent = {}
    backoff = not (LIFE / 'handoff-backoff-off').exists()
    today = opening.date().isoformat()
    lines = []
    for site, v in sites.items():
        lapse = v.get('lapse') if isinstance(v, dict) else None
        if v.get('status') != 'signed_out' or not lapse:
            continue
        key = f"{site}#{lapse.get('n')}"
        dates = _dates(sent.get(key))
        held = waiting(site)
        if not due(dates, today, bool(held), backoff):
            continue
        at = datetime.fromtimestamp(time.time(), timezone.utc)
        if alert_open(at, life=LIFE) > at:
            return
        r = subprocess.run([PY, '-I', str(HERE / 'handoff.py'), 'link', site], capture_output=True, text=True, timeout=60,
                           env={**os.environ, 'LIFE_HOME': str(LIFE)})
        line = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ''
        if line.startswith('http') or 'http' in line:
            if held and dates:                     # a repeat only earns its place by naming what is stuck
                what = held[0] if len(held) == 1 else f"{held[0]} and {len(held) - 1} more"
                name, sep, rest = line.partition(' signed me out. ')
                line = f"{name} is still signed out, so {what} is waiting. {rest}" if sep else f"Still signed out, so {what} is waiting. {line}"
            lines.append(line)
            sent[key] = {'dates': dates + [today]}
    at = datetime.fromtimestamp(time.time(), timezone.utc)
    if lines and alert_open(at, life=LIFE) <= at:
        tmp = SENT.with_name('.signin-handoff-sent.json.tmp'); tmp.write_text(json.dumps(sent)); os.replace(tmp, SENT)
        print('\n\n'.join(lines))


if __name__ == '__main__':
    main()

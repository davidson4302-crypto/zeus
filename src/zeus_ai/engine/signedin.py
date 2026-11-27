"""signedin: before a browser errand, is the site still signed in?

Zeus has its own Chrome with a one-time copy of the owner's store sign-ins (the sites in the site table below).
When one of them lapses, an errand on that site would fail quietly, so code looks first. Being signed in is
itself just a watch on a page: one saved read of the site's account page (the same reader and the same "not more
often than" floor as watches.py) and three possible answers.

  signed_in    the page shows what a signed-in account page shows. The errand goes ahead.
  signed_out   the page is a sign-in page. The errand is HELD, not failed, and the owner gets ONE plain ask for this lapse:
               sign in once in Zeus's Chrome. Code keeps looking (15 min, 30 min, 1 h, 2 h, then every 4 h). When a
               later look shows signed in, code closes the ask and lets the held errands go.
  unknown      Chrome could not be reached, the page did not load, or it looked like neither. Unknown is NOT signed
               out: nobody is asked anything and nothing is held. It is written into the run's health note.

The site table (signin-sites.json; a copy in the life folder wins over the one beside this file) is data: for each
site its account page, what a signed-in page shows, what a sign-in page shows. A site still marked as a placeholder
is never read and always answers unknown, so this file is safe to install before the real pages are captured.

Switch file in the life folder: `signin-check-on`. Without it nothing here runs.
A held errand that would otherwise be late is not held for ever: two hours before its own deadline or start, or after
a day with no time of its own, the owner is told about it without the prep, with the reason.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import watches as W

HERE = Path(__file__).resolve().parent
LIFE = Path(os.environ.get('LIFE_HOME', str(W.LIVE_HOME)))
PACIFIC = W.PACIFIC
SWITCH = 'signin-check-on'
FILE = 'signins.json'
TABLE = 'signin-sites.json'
FRESH = 30 * 60                # a "signed in" reading is trusted this long before an errand makes code look again
RECHECK_BASE = 15 * 60         # after a lapse, and after an unknown: look again 15 min, 30 min, 1 h, 2 h, then every 4 h
RECHECK_CAP = 4 * 3600
MAX_READS_PER_RUN = 2          # account pages read for errands in one run of the worker
HOLD_STEP = 15 * 60            # a held errand comes round again this soon
LEAD = 2 * 3600                # an errand with a time of its own stops waiting this long before it
GIVE_UP_AFTER = 24 * 3600      # an errand with no time of its own waits this long, then the owner is told without the prep
ASK_STEP = 'Tell the owner the sign-in has lapsed and ask them to sign in once.'


def on(life) -> bool:
    return (Path(life) / SWITCH).exists()


def _store(life):
    return W.Store(life, FILE, 'sites', dict)


def sites(life) -> dict:
    """The site table: {id: {name, match, hosts, account_url, signed_in, signed_out, signed_out_url, placeholder}}."""
    for path in (Path(life) / TABLE, HERE / TABLE):
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get('sites'), dict):
            return {str(k): {f: ([x] if isinstance(x, str) and f in ('match', 'hosts', 'signed_in', 'signed_out', 'signed_out_url') else x) for f, x in v.items()}
                    for k, v in d['sites'].items() if isinstance(v, dict)}   # glue.py build: one string where a list is expected counts as a list of one
    return {}


def usable(entry: dict) -> bool:
    """Filled in with what the real pages show? A placeholder entry is never read."""
    return (not entry.get('placeholder') and str(entry.get('account_url') or '').startswith('https://')
            and bool(entry.get('signed_in')) and bool(entry.get('signed_out') or entry.get('signed_out_url')))


def hosts(life) -> tuple:
    """Every site the owner is signed in to: the only places a watch may read in Zeus's signed-in Chrome."""
    return tuple(sorted({str(h).lower() for e in sites(life).values() for h in e.get('hosts') or [] if h}))


def classify(entry: dict, page: dict) -> tuple:
    """One read of a page of this site -> ('signed_in' | 'signed_out' | 'unknown', why). Signed out needs a positive
    sign: the read ended on a sign-in address, or the page shows sign-in words and nothing a signed-in page shows.
    A blank page, an error, a "are you a robot" page, or signs of both: unknown."""
    if not isinstance(page, dict) or not page.get('ok'):
        return 'unknown', W._one_line((page or {}).get('why') if isinstance(page, dict) else '', 200) or 'the page could not be read'
    final, text = str(page.get('final_url') or '').lower(), str(page.get('text') or '').lower()
    has = lambda marks, hay: any(str(m).lower() in hay for m in marks or [] if str(m).strip())
    inside, out_page, out_words = has(entry.get('signed_in'), text), has(entry.get('signed_out_url'), final), has(entry.get('signed_out'), text)
    if inside and not out_page and not out_words:
        return 'signed_in', ''
    if (out_page or out_words) and not inside:
        return 'signed_out', 'the page was a sign-in page'
    return 'unknown', 'the page showed signs of both' if inside else 'the page looked like neither a signed-in page nor a sign-in page'


def _record(life, site: str, status: str, why: str, now_dt: datetime) -> dict:
    """Write one reading down. A lapse opens on the first signed_out and closes only on a signed_in."""
    now = W._iso(now_dt)

    def put(d):
        s = d['sites'].setdefault(site, {})
        s.update(status=status, checked_at=now, why=why or None)
        lapse = s.get('lapse')
        if status == 'unknown':
            s['unknown_since'] = s.get('unknown_since') or now
            s['unknown_checks'] = int(s.get('unknown_checks') or 0) + 1
            s['next_check'] = W._iso(now_dt + timedelta(seconds=W.backoff(s['unknown_checks'] - 1, RECHECK_BASE, RECHECK_CAP)))
            if lapse:
                lapse['next_check'] = s['next_check']
        else:
            s.update(unknown_since=None, unknown_checks=0, next_check=None, known=status, known_at=now)
        if status == 'signed_out':
            if not lapse:
                s['lapses'] = int(s.get('lapses') or 0) + 1
                s['lapse'] = {'n': s['lapses'], 'since': now, 'ask_key': None, 'rechecks': 0,
                              'next_check': W._iso(now_dt + timedelta(seconds=RECHECK_BASE))}
            else:
                lapse['rechecks'] = int(lapse.get('rechecks') or 0) + 1
                lapse['next_check'] = W._iso(now_dt + timedelta(seconds=W.backoff(lapse['rechecks'], RECHECK_BASE, RECHECK_CAP)))
        elif status == 'signed_in' and lapse:
            s['last_lapse'] = {**lapse, 'closed_at': now}
            s['lapse'] = None
        return dict(s)
    return _store(life).change(put) or {}


def _state(life, site: str) -> dict:
    d = _store(life).load()
    return dict((d or {}).get('sites', {}).get(site) or {})


def check(site: str, life=None, now_dt: datetime | None = None, use=None, force: bool = False, budget: dict | None = None) -> str:
    """check(site) -> 'signed_in' | 'signed_out' | 'unknown'.
    Reads the site's account page in Zeus's Chrome only when it has to: a recent "signed in" is trusted for FRESH, an
    open lapse is looked at again on its own timer, an unknown is not asked again for a while, and the site's floor
    (watches.HOST_FLOOR) is kept. While a lapse is open the answer stays signed_out until a read shows signed in:
    a read that could not tell does not end a lapse. force: the chat Zeus's "the owner says they signed in, look now".
    budget: a counter shared by one run ({'signin_reads': n}), so one run cannot read many account pages."""
    life = Path(life or LIFE)
    now_dt = now_dt or datetime.now(PACIFIC)
    entry = sites(life).get(site)
    if entry is None:
        return 'unknown'
    if not usable(entry):
        return 'unknown'                                    # not captured yet: never read, never a reason to ask the owner
    s = _state(life, site)
    lapse = s.get('lapse')
    held_answer = 'signed_out' if lapse else 'unknown'

    def later(value):
        t = W._dt(value)
        return t is not None and t > now_dt
    if not force:
        checked = W._dt(s.get('checked_at'))
        if not lapse and s.get('status') == 'signed_in' and checked is not None and (now_dt - checked).total_seconds() < FRESH:
            return 'signed_in'
        if lapse and later(lapse.get('next_check')):
            return 'signed_out'
        if not lapse and s.get('status') == 'unknown' and later(s.get('next_check')):
            return 'unknown'
        if budget is not None and int(budget.get('signin_reads') or 0) >= MAX_READS_PER_RUN:
            return held_answer
        w = W.Store(life).load() or {}
        last = W._dt((w.get('hosts') or {}).get(W.site_of(entry['account_url'])))
        if last is not None and (now_dt - last).total_seconds() < W.HOST_FLOOR:
            return held_answer if lapse or s.get('known') != 'signed_in' else 'signed_in'    # too soon to read this site again: the last sure answer stands
    if budget is not None:
        budget['signin_reads'] = int(budget.get('signin_reads') or 0) + 1
    page = W.read(life, {'kind': 'chrome', 'url': entry['account_url']}, use, trusted_path=True)
    W.Store(life).change(lambda d: d['hosts'].__setitem__(W.site_of(entry['account_url']), W._iso(now_dt)))
    status, why = classify(entry, page)
    s = _record(life, site, status, why, now_dt)
    if status == 'signed_out' and (life / 'relogin-on').exists() and __import__('relogin').after_lapse(site, entry, life, now_dt, use):
        return 'signed_in'      # fix-relogin: Zeus signed itself back in (no code was asked) and a fresh read of the account page shows it
    return 'signed_out' if s.get('lapse') else status


def saw(life, url: str, page: dict, now_dt: datetime):
    """A watch just read a page of a signed-in site. If that page plainly is a signed-in page or a sign-in page, it
    counts as a look (no extra read). A page that cannot tell changes nothing."""
    host = W.urllib.parse.urlsplit(str(url)).hostname or ''
    for site, entry in sites(life).items():
        if usable(entry) and any(host == h or host.endswith('.' + h) for h in entry.get('hosts') or []):
            status, why = classify(entry, page)
            if status != 'unknown':
                _record(life, site, status, why, now_dt)
            return


def lapsed_hosts(life) -> set:
    d = _store(life).load() or {}
    table = sites(life)
    return {W.site_of('https://' + h) for site, s in (d.get('sites') or {}).items() if s.get('lapse') for h in (table.get(site) or {}).get('hosts') or []}


def sites_needed(opp: dict, table: dict) -> list:
    """Which signed-in sites an errand is about: a site's own name in what the errand will DO (its title, its plan).
    Only sites that are filled in count."""
    steps = (opp.get('decision') or {}).get('steps') or opp.get('steps') or []
    text = ' '.join([str(opp.get('title') or ''), str(opp.get('proposed_action') or '')] + [str(s.get('do') or '') for s in steps if isinstance(s, dict)]).lower()
    return [site for site, e in table.items() if usable(e)
            and any(re.search(r'(?<![a-z0-9])' + re.escape(str(m).lower()) + r'(?![a-z0-9])', text) for m in e.get('match') or [] if str(m).strip())]


# ---- the one ask ----------------------------------------------------------------------------------------------------------

def _waiting_titles(con, site: str) -> list:
    rows = con.execute("SELECT payload FROM opps WHERE state='pending' AND json_extract(payload,'$.held_for_signin.site')=?", (site,)).fetchall()
    return [_waiting_one(json.loads(r[0])) for r in rows][:3]


def _waiting_one(opp: dict) -> dict:
    """One held errand, as the writer needs it, so the ask can say "before 8pm tonight" and that the owner already okayed
    the order: its title, its own deadline (or, with none, its start), and
    whether the owner already okayed it (their yes on record, or a standing okay for this kind of thing). Code reads all three
    from the item; nothing is guessed."""
    import policy as P
    d = P._moment(opp.get('deadline')) or P._moment(opp.get('event_start'))
    steps = (opp.get('decision') or {}).get('steps') or []
    ok = ({'at': opp['approved'].get('at')} if isinstance(opp.get('approved'), dict) and opp['approved'].get('at')
          else 'standing' if any(isinstance(x, dict) and x.get('granted') for x in steps) else None)
    return {'title': W._one_line(opp.get('title'), 80), 'by': d.isoformat() if d else None, 'okayed': ok}


def _held_words(h) -> str:
    """One held errand in words that are true whenever they are read (full dates): the note words them again for the
    moment it goes (fresh._signin_facts)."""
    if not isinstance(h, dict):
        return str(h)
    import fresh as FR
    bits = []
    if isinstance(h.get('okayed'), dict) and h['okayed'].get('at'):
        bits.append('you okayed it ' + FR.speak(FR.at_mark(h['okayed']['at'], 's')))
    elif h.get('okayed') == 'standing':
        bits.append('you gave a standing okay for this kind of thing')
    if h.get('by'):
        bits.append('it must be done before ' + FR.speak(FR.at_mark(h['by'], 'm')))
    return str(h.get('title') or '') + (' (' + '; '.join(bits) + ')' if bits else '')


def packet_for(name: str, waiting: list) -> dict:
    """The plain facts the note writer gets, already in Zeus's own first person and talking to the owner, as facts (for
    example: "I've lost the store sign-in. Open my Chrome and sign in once and I'll pick up where I left off.").
    Never Zeus's name here: worded "Zeus has lost its ...", the writer would copy it and text the owner about itself in
    the third person.
    waiting: the held errands (_waiting_one); a plain title is taken as it is."""
    held = (' Waiting on it: ' + '; '.join(_held_words(w) for w in waiting) + '.') if waiting else ''
    return {'status': 'needs_decision', 'headline': f'My {name} sign-in has lapsed: I can no longer get into {name} in my own browser, so what I do there for you is on hold.',
            'details': f"Your one step: sign in to {name} once in my own Chrome. I see it by myself and carry on where I left off.{held}",
            'decision': {'ask': f"Sign in to {name} once in my Chrome.",
                         'ready': f"The one step is yours: open my own Chrome, go to {name} and sign in. Nothing else is needed; I see it by myself and carry on.",
                         'default': '', 'by': 'owner'},
            'followups': [], 'learned': [], 'store': name, 'held': [w for w in waiting if isinstance(w, dict)]}


def ensure_ask(con, life, site: str, now_dt: datetime) -> str:
    """One ordinary item on the owner's list for this lapse, ready with the ask, timed by the engine's own policy (so quiet
    hours hold). Its key is made from the lapse itself, so a second call for the same lapse adds nothing.
    -> the item's key, or '' when the owner has said to stop raising these or there is no open lapse."""
    import policy as P
    import prefs as F
    s = _state(life, site)
    lapse = s.get('lapse')
    if not lapse:
        return ''
    if lapse.get('ask_key'):
        return '' if lapse['ask_key'] == 'muted' else lapse['ask_key']
    name = str((sites(life).get(site) or {}).get('name') or site)
    key = f"signin:{site}:{str(lapse.get('since'))[:10]}#{lapse.get('n')}"    # "#": every lapse of one site is the same kind of thing to mute
    opp = {'title': f'{name} sign-in has lapsed', 'why': f'Zeus can no longer get into {name} for the owner, so anything it does there waits.',
           'evidence': f"The {name} account page showed a sign-in page when Zeus's own Chrome opened it.", 'urgency': 'today', 'dedupe_key': key,
           'confidence': 1.0, 'owner_involved': 'yes', 'action_class': 'notify_owner_now', 'proposed_action': ASK_STEP,
           'steps': [{'do': ASK_STEP, 'action_class': 'notify_owner_now'}]}
    decision = P.decide(opp, None, now_dt, muted=F.muted_families(life))
    made = 'muted'
    if decision.get('autonomy') != 'skip':
        now = time.time()
        waiting = _waiting_titles(con, site)
        if site == 'walmart':      # the refill preparation waits on this sign-in too, and it is not a ledger errand (it keeps its place in orders.json): the one ask still says what is on hold
            try:
                waiting = (waiting + __import__('orderwatch').refill_waiting(life, site))[:3]
            except Exception:
                pass
        payload = {**opp, 'decision': decision, '_source': 'signin check', 'packet': packet_for(name, waiting), 'ready_at': now,
                   'signin': {'site': site, 'lapse': lapse.get('n')}}
        con.execute('INSERT OR IGNORE INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (key, 'ready', decision['autonomy'], decision['surface_at'], 'today', 'notify_owner_now', json.dumps(payload, default=str), '[]', now, now))
        con.commit()
        made = key

    def mark(d):
        cur = (d['sites'].get(site) or {}).get('lapse')
        if cur and not cur.get('ask_key'):
            cur.update(ask_key=made, asked_at=W._iso(now_dt))
    _store(life).change(mark)
    return '' if made == 'muted' else made


def _close_ask(con, key: str) -> str:
    """The owner is signed in again: the ask has done its job. One they were never told about closes without a word."""
    row = con.execute('SELECT state,payload FROM opps WHERE key=?', (key,)).fetchone()
    if row is None or row[0] not in ('pending', 'ready', 'surfaced'):
        return ''
    state = 'done' if row[0] == 'surfaced' else 'moot'
    p = json.loads(row[1])
    p['closed'] = {'state': state, 'at': time.time(), 'by': 'engine', 'why': 'signed in again' + ('' if state == 'done' else ' before the owner was told')}
    con.execute('UPDATE opps SET state=?, payload=?, updated=? WHERE key=? AND state=?', (state, json.dumps(p, default=str), time.time(), key, row[0]))
    con.commit()
    return state


def tend(con, life, now_dt: datetime, use=None) -> dict:
    """Once a tick (watches.due). Sites with an open lapse are looked at again when their timer says so; a lapse that
    has no ask yet gets its one ask; a lapse that has closed has its ask closed. Held errands are let go by the
    worker's own next pass (hold)."""
    out = {}
    d = _store(life).load()
    if d is None:
        return {'errors': ['the sign-in notes could not be read']}
    for site, s in list(d['sites'].items()):
        if s.get('lapse'):
            if check(site, life, now_dt, use) == 'signed_out':
                if ensure_ask(con, life, site, now_dt):
                    out['lapsed'] = out.get('lapsed', []) + [site]
        s = _state(life, site)
        last = s.get('last_lapse') or {}
        if not s.get('lapse') and last.get('ask_key') and last['ask_key'] != 'muted' and not last.get('ask_closed'):
            how = _close_ask(con, last['ask_key'])

            def done(data, site=site):
                ((data['sites'].get(site) or {}).get('last_lapse') or {})['ask_closed'] = W._iso(now_dt)
            _store(life).change(done)
            out['back'] = out.get('back', []) + [site]
            if how:
                out['asks_closed'] = out.get('asks_closed', 0) + 1
        # The sign-in is back (the owner tapped Done on the phone page, or a later look saw it). Otherwise an errand held for
        # it would come round only at its own next 15-minute look (HOLD_STEP), so "Zeus will pick the errand back up" could
        # take 15 minutes plus a tick. The errands held for THIS site come round now: only those, and once per lapse (the mark
        # below). Nothing is let go here: each still passes hold() in this same run, which reads this site's record again, and
        # every later gate is untouched (the owner's yes, the purchase lock, the daily budget).
        s = _state(life, site)
        last = s.get('last_lapse') or {}
        if not s.get('lapse') and s.get('status') == 'signed_in' and last.get('closed_at') and not last.get('held_released'):
            moved = con.execute("UPDATE opps SET surface_at=? WHERE state='pending' AND json_extract(payload,'$.held_for_signin.site')=? AND julianday(surface_at)>julianday(?)",
                                (W._iso(now_dt), site, W._iso(now_dt))).rowcount
            con.commit()

            def released(data, site=site):
                ((data['sites'].get(site) or {}).get('last_lapse') or {})['held_released'] = W._iso(now_dt)
            _store(life).change(released)
            if moved:
                out['held_come_round'] = out.get('held_come_round', 0) + moved
    return out


# ---- the worker's hook: hold an errand whose site is signed out -------------------------------------------------------------

def _late(opp: dict, mark: dict, now_dt: datetime) -> bool:
    """Has this errand waited as long as it can? Two hours before its own deadline or start; a day when it has none."""
    return now_dt >= _limit(opp, mark, now_dt)


def _limit(opp: dict, mark: dict, now_dt: datetime) -> datetime:
    """The moment a held errand stops waiting: two hours before its own deadline or start; a day when it has none."""
    import policy as P
    times = P.lands_at(opp)
    if times:
        return min(times) - timedelta(seconds=LEAD)
    since = W._dt(mark.get('since')) or now_dt
    return since + timedelta(seconds=GIVE_UP_AFTER)


def waited_for_signin(opp: dict, now_dt: datetime, grace: timedelta = timedelta(minutes=60)) -> bool:
    """tick.watchdog: is this item not late, because it waited for a sign-in? Treated the way an item held for the
    morning is (otherwise an errand held an hour for a sign-in would raise "Zeus is over an hour late" the moment it
    was let go, because its real due time is put back on release).
      held now      not late while the hold is good; late again only once the hold outlives its own limit (_limit)
                    by the watchdog's usual hour
      let go        (signed in again, or given up on) lateness counts from that moment, not from the old due time
    False for every item that never waited for a sign-in, so the watchdog is exactly what it was for those."""
    try:
        mark = opp.get('held_for_signin')
        if isinstance(mark, dict):
            return now_dt < _limit(opp, mark, now_dt) + grace
        for name in ('signin_released', 'signin_not_renewed'):
            t = W._dt((opp.get(name) or {}).get('at')) if isinstance(opp.get(name), dict) else None
            if t and now_dt < t + grace:
                return True
    except Exception:
        return False
    return False


def hold(con, life, r, opp: dict, now_dt: datetime, res: dict, use=None) -> bool:
    """worker.run asks this just before it would start a background job for a due item. True = do not start it now.
    Signed out: the errand stays where it is (pending, marked, and brought round again in a few minutes) and the one
    ask for this lapse is on the owner's list. Signed in, or cannot tell: False, and an errand that was held is let go.
    With the switch file absent, and on any trouble in here, it answers False: the worker does what it always did."""
    if not on(life):
        return False
    try:
        table = sites(life)
        mark = opp.get('held_for_signin')
        out = next((site for site in sites_needed(opp, table) if check(site, life, now_dt, use, budget=res) == 'signed_out'), None)
        key = r['key']
        if out is None:
            if mark:        # signed in again: pick up where it left off
                opp.pop('held_for_signin', None)
                opp['signin_released'] = {'at': W._iso(now_dt), 'held_since': mark.get('since'), 'site': mark.get('site')}
                con.execute("UPDATE opps SET surface_at=?, payload=?, updated=? WHERE key=? AND state='pending'",
                            (mark.get('was_due') or r['surface_at'], json.dumps(opp, default=str), time.time(), key))
                res['released_after_signin'] = res.get('released_after_signin', 0) + 1
            return False
        import prefs as F
        if F.is_muted(life, f'signin:{out}'):
            return False                                    # the owner said to stop raising these: nothing is asked, so nothing is held
        name = str(table[out].get('name') or out)
        if not mark:
            mark = opp['held_for_signin'] = {'site': out, 'since': W._iso(now_dt), 'was_due': r['surface_at']}
        if _late(opp, mark, now_dt):
            import worker as Wk
            opp.pop('held_for_signin', None)
            opp['signin_not_renewed'] = {'at': W._iso(now_dt), 'site': out, 'held_since': mark.get('since')}
            con.execute("UPDATE opps SET surface_at=? WHERE key=? AND state='pending'", (mark.get('was_due') or r['surface_at'], key))
            Wk._ready(con, key, opp, Wk.synth_packet(opp, f'the background work could not be done: the {name} sign-in lapsed and was not renewed in time'), now_dt, res)
            res['signin_gave_up'] = res.get('signin_gave_up', 0) + 1
            return True
        # Held. It comes round again in a few minutes; its real due time is kept in the mark and put back on release
        # (the same way the sender holds an item for the morning).
        con.execute("UPDATE opps SET surface_at=?, payload=? WHERE key=? AND state='pending'",
                    (W._iso(now_dt + timedelta(seconds=HOLD_STEP)), json.dumps(opp, default=str), key))
        con.commit()
        ensure_ask(con, life, out, now_dt)
        res['held_for_signin'] = res.get('held_for_signin', 0) + 1
        return True
    except Exception as e:
        res.setdefault('errors', []).append(f"signin {str(r['key'])[:40]}: {type(e).__name__}: {str(e)[:80]}")
        return False


def health(life) -> dict:
    """For the run's health note: what each site last showed, and since when it could not be told."""
    d = _store(life).load()
    if d is None:
        return {'readable': False}
    table = sites(life)
    out = {'readable': True, 'not_set_up': sorted(str(e.get('name') or k) for k, e in table.items() if not usable(e)), 'sites': {}}
    for site, s in (d.get('sites') or {}).items():
        out['sites'][site] = {k: v for k, v in (('status', s.get('status')), ('checked_at', s.get('checked_at')), ('why', s.get('why')),
                                                ('unknown_since', s.get('unknown_since')), ('unknown_checks', s.get('unknown_checks') or None),
                                                ('lapsed_since', (s.get('lapse') or {}).get('since'))) if v}
    return out

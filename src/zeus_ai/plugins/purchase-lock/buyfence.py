"""buyfence: Zeus's purchase lock, inside the browser child.

What it does. Zeus drives its own Chrome through browser_exec (public Hermes: Python run by the browser-harness CLI).
Every helper there (click_at_xy, js, cdp, press_key, type_text, fill_input ...) talks to the browser through one pipe,
browser_harness.helpers._send. arm() (called by the prelude the gateway puts in front of Zeus's code) puts a Fence in
front of that pipe. Before a request that could press something reaches the page, code (never the model) decides:

  * Is this a purchase step? A click on a Place order / Buy now / Pay now style button (on one of the owner's stores, or on
    any checkout page), or on a checkout page: the Enter key, raw CDP input, a script that clicks or submits, a click
    on something that cannot be read.
  * Is it the owner's own chat turn? A cron job, a delegated helper or anyone else never gets to press.
  * If it is, is there a grant? A grant is the owner's real yes to THIS store and a total, recorded by `zeus-buy yes`
    (their own message, read by code). No grant: refused, nothing is sent to the page.
  * With a grant: the order total is read off the page. Unreadable, or above their yes, or a different store: refused.
  * Pass: the grant is marked `ordering` on disk (fsync) BEFORE input, the click (mousePressed + mouseReleased) is
    sent once, and the grant becomes `placed_unconfirmed`. Any later purchase step at that store is refused
    `already_ordered` and told to read the store's orders page, never to click again.
  * Raw input (keys, scripts, raw CDP) can never place an order, even with a grant: it cannot be tied to one button and
    one press. Only click_element / click_at_xy on the button itself can.

A refused request is never sent: the step stops with the plain-words reason (printed and raised).
Every decision on a purchase step or a checkout page is appended to buy-log.jsonl in the life folder.
Switch files in the life folder: buy-lock-off (do nothing), buy-lock-shadow (log, refuse nothing). None = enforce.
If anything here errors, the request that could press is refused (fail closed for money).

Booking stores: a booking yes binds the booking, not just store + total. A store with a "booking" block
in stores.json is a booking store. Its grant must carry the trip (dates, flight numbers, airports,
passengers; times and fare when given), and on the press the page's trip is read (booking.py, loaded from this folder
only when a booking store is judged) and compared: anything that differs or is missing is refused
(booking_details_differ), a page with no readable trip is refused (booking_details_unreadable), and a yes recorded
without the trip is refused (booking_needs_details). Non-booking stores are judged on store and total only.

The grants file and the store table are shared with the `zeus-buy` command, which imports this module.
Stdlib only: this runs in the browser-harness child (Python 3.14) and in zeus-buy.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

VERSION = 'purchase-lock-3.0'
HERE = Path(__file__).resolve().parent
ENGINE_DIR = HERE.parents[1] / 'engine'          # zeus_ai/engine, where settings.py lives


def _live_life() -> Path:
    """The live life folder from engine/settings.py. This module also runs in a bare browser child, so a settings
    module that cannot be loaded falls back to LIFE_HOME or the default profile location."""
    try:
        if str(ENGINE_DIR) not in sys.path:
            sys.path.insert(0, str(ENGINE_DIR))
        import settings
        return Path(settings.LIFE_HOME)
    except Exception:
        return Path(os.environ.get('LIFE_HOME') or Path.home() / '.hermes' / 'profiles' / 'zeus' / 'workspace' / 'life-engine')


LIVE_LIFE = _live_life()
GRANTS_FILE = 'buy-grants.json'
LOCK_FILE = 'buy-grants.lock'
LOG_FILE = 'buy-log.jsonl'
OFF_SWITCH = 'buy-lock-off'
SHADOW_SWITCH = 'buy-lock-shadow'
GRANT_SECONDS = 2 * 3600          # the owner's yes holds for 2 hours
STUCK_ORDERING = 10 * 60          # an `ordering` nobody finished becomes `unknown`
RECENT_PLACED = 24 * 3600         # mention an order placed at this store in the last day
STATES = ('approved', 'ordering', 'placed_unconfirmed', 'placed', 'not_placed', 'unknown', 'expired', 'cancelled')
UNSETTLED = ('ordering', 'placed_unconfirmed', 'unknown')
OPEN = ('approved',) + UNSETTLED

ENTER_KEYS = {'enter', 'return', 'numpadenter', '\r', '\n'}
SPACE_KEYS = {' ', 'space', 'spacebar'}
TEXT_INPUTS = {'', 'text', 'search', 'email', 'tel', 'number', 'password', 'url'}
# A script on a checkout page that presses or submits something.
RAW_PRESS = re.compile(r'click|submit|dispatchEvent|(?:Mouse|Pointer|Keyboard|Touch|Input)Event', re.I)
# A script on a checkout page that could hide a press (built names, timers, network calls). Read-only scripts never need these.
RAW_HIDDEN = re.compile(r"\beval\b|\bFunction\s*\(|setTimeout|setInterval|fromCharCode|\batob\b|\bfetch\s*\(|XMLHttpRequest|"
                        r"sendBeacon|\[\s*['\"`]|\bimport\s*\(", re.I)
# Booking checkout pages only: a script that changes what the page SHOWS (not form values) could make the trip
# reader see a trip that is not the one being booked. Reads (el.innerHTML) and typing (el.value = ...) are fine.
RAW_WRITE = re.compile(r"\.(?:innerHTML|outerHTML|textContent|innerText|outerText|nodeValue|className|hidden)\s*(?:\+|-)?=(?!=)|"
                       r"\.style(?:\.[A-Za-z]+)?\s*=(?!=)|\.style\.(?:setProperty|cssText)|insertAdjacent(?:HTML|Element|Text)|"
                       r"document\.(?:write|open)\s*\(|\b(?:appendChild|insertBefore|replaceChild|replaceChildren|replaceWith|removeChild|"
                       r"setAttribute|removeAttribute|setHTML(?:Unsafe)?|attachShadow)\s*\(|\.(?:append|prepend|before|after|remove)\s*\(|"
                       r"classList\.(?:add|remove|toggle|replace)|\bcreateTextNode\b|\badoptedStyleSheets\b|\binsertRule\b", re.I)
DOM_WRITE_CDP = re.compile(r'^(?:DOM\.(?:set|remove|move|insert|undo|redo|copyTo)|CSS\.(?:set|add|create)|Page\.setDocumentContent)')
MONEY = re.compile(r'\$\s?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)')
MONEY_ANY = re.compile(r'\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?')
DIALOG_WORDS = re.compile(r'order|pay|purchase|place|buy|charge|confirm|book|checkout', re.I)


# ---- settings ---------------------------------------------------------------------------------------------------------

def life_home(settings: dict | None = None) -> Path:
    return Path((settings or {}).get('life') or os.environ.get('BUY_LIFE_HOME') or LIVE_LIFE)


def stores_path(settings: dict | None = None) -> Path:
    return Path((settings or {}).get('stores') or os.environ.get('BUY_STORES_FILE') or HERE / 'stores.json')


def mode(life: Path) -> str:
    """'off' | 'shadow' | 'enforce'. A switch that cannot be read counts as absent (enforce)."""
    try:
        if (life / OFF_SWITCH).exists():
            return 'off'
        if (life / SHADOW_SWITCH).exists():
            return 'shadow'
    except OSError:
        pass
    return 'enforce'


# ---- money and time ---------------------------------------------------------------------------------------------------

def cents(value) -> int | None:
    """'42.17', '$42.17', '1,234.5', 42 -> cents. Anything else -> None."""
    s = str(value if value is not None else '').strip().replace('$', '').replace(',', '').strip()
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?', s):
        return None
    whole, _, frac = s.partition('.')
    return int(whole) * 100 + int((frac + '00')[:2])


def dollars(c: int) -> str:
    return f'${c // 100:,}.{c % 100:02d}'


def amount(c: int) -> str:
    """For a command line: 42.17 (no $, no commas)."""
    return f'{c // 100}.{c % 100:02d}'


def clock(ts) -> str:
    try:
        t = time.localtime(float(ts))
    except (TypeError, ValueError):
        return 'an unknown time'
    return f"{t.tm_hour % 12 or 12}:{t.tm_min:02d}{'am' if t.tm_hour < 12 else 'pm'}"


def norm(label) -> str:
    """Button words, normalised: 'Place order  $42.17' -> 'place order'."""
    s = str(label or '').lower().replace('&', ' & ').replace('’', "'")
    s = MONEY_ANY.sub(' ', s)
    s = re.sub(r"[^a-z&' ]+", ' ', s).replace("'", '')
    return re.sub(r'\s+', ' ', s).strip()


def public_page(url) -> str:
    """Scheme, host and path only: queries can carry private values."""
    try:
        u = urlsplit(str(url or ''))
        host = u.hostname or ''
        if u.port:
            host += f':{u.port}'
        return f'{u.scheme}://{host}{u.path}' if u.scheme else str(url or '')[:120]
    except ValueError:
        return '[unreadable]'


# ---- booking checks (booking.py, this folder) -------------------------------------------------------------------------

_BOOKING: dict = {}


def booking_module(path: Path | str | None = None):
    """booking.py from this folder, loaded once by path (no import path needed in the browser child). Loaded only when a
    booking store is judged, so a problem in it can never touch an ordinary store's order; for a booking store any error
    refuses the press (fail closed)."""
    import importlib.util
    p = Path(path or os.environ.get('BUY_BOOKING_PATH') or HERE / 'booking.py')
    mod = _BOOKING.get(str(p))
    if mod is None:
        spec = importlib.util.spec_from_file_location('_zeus_purchase_lock_booking', p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BOOKING[str(p)] = mod
    return mod


# ---- the store table --------------------------------------------------------------------------------------------------

class Stores:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else stores_path()
        data = json.loads(self.path.read_text(encoding='utf-8'))
        g = data['generic']
        self.strong = [re.compile(p, re.I) for p in g['commit_strong']]
        self.weak = [re.compile(p, re.I) for p in g['commit_weak']]
        self.attr_rx = re.compile(g['commit_attr'], re.I)
        self.generic_urls = [re.compile(p, re.I) for p in g['commit_url']]
        self.generic_totals = list(g['total_labels'])
        self.stores = {}
        for sid, s in data['stores'].items():
            self.stores[sid] = dict(s, id=sid, urls=[re.compile(p, re.I) for p in s.get('commit_url', [])])
        self.airports = dict(g.get('airports') or {})
        self._total_rx = {}
        self._booking = {}

    # booking stores
    def is_booking(self, sid: str | None) -> bool:
        """A store with a "booking" block: its yes must carry the trip and the page's trip must match it."""
        return bool(sid in self.stores and self.stores[sid].get('booking'))

    def booking(self, sid: str | None) -> dict | None:
        """The store's booking settings, compiled (booking.prepare). None for an ordinary store. Raises when the
        settings or booking.py cannot be used (callers refuse the press)."""
        if not self.is_booking(sid):
            return None
        if sid not in self._booking:
            self._booking[sid] = booking_module().prepare(self.stores[sid]['booking'], self.airports)
        return self._booking[sid]

    # which store
    def for_host(self, host: str) -> str | None:
        host = (host or '').lower().rstrip('.')
        for sid, s in self.stores.items():
            for h in s['hosts']:
                if host == h or host.endswith('.' + h):
                    return sid
        return None

    def for_name(self, name: str) -> str | None:
        n = re.sub(r'\s+', ' ', str(name or '').strip().lower())
        for sid, s in self.stores.items():
            if n == sid or n in [a.lower() for a in s.get('aliases', [])] or n == s['name'].lower():
                return sid
        return None

    def name(self, sid: str | None) -> str:
        return self.stores[sid]['name'] if sid in self.stores else 'this store'

    def orders_url(self, sid: str | None) -> str:
        return self.stores[sid].get('orders_url', '') if sid in self.stores else ''

    def words_for(self, sid: str) -> set:
        """Words that name a store in an ask ('Walmart', "Sam's Club", 'Amazon')."""
        s = self.stores.get(sid) or {}
        return {norm(a) for a in [sid, s.get('name', '')] + list(s.get('aliases', [])) if norm(a)}

    # what a button is
    def is_strong(self, labels) -> bool:
        return any(rx.fullmatch(n) for n in map(norm, labels or []) if n for rx in self.strong)

    def is_weak(self, labels) -> bool:
        return any(rx.fullmatch(n) for n in map(norm, labels or []) if n for rx in self.weak)

    def is_commit_attr(self, attrs) -> bool:
        return bool(attrs) and bool(self.attr_rx.search(str(attrs)))

    # which page
    def commit_url(self, sid: str | None, path: str) -> bool:
        rules = self.stores[sid]['urls'] if sid in self.stores else self.generic_urls
        return any(rx.search(path or '/') for rx in rules)

    def any_commit_url(self, url: str) -> bool:
        u = urlsplit(str(url or ''))
        sid = self.for_host(u.hostname or '')
        if sid=='trip-hotel' and not reservation_adapter().hotel_path(u.path): sid=None
        return self.commit_url(sid, u.path) or (sid is None and any(rx.search(u.path or '/') for rx in self.generic_urls))

    def read_total(self, text: str, sid: str | None) -> int | None:
        """The order total on the page: the store's own label first, then the generic ones; the LAST place a label is
        followed (same line or up to two lines on) by a money amount. None when no label has an amount."""
        labels = (list(self.stores[sid].get('total_labels', [])) if sid in self.stores else []) + self.generic_totals
        for label in labels:
            rx = self._total_rx.get(label)
            if rx is None:
                rx = re.compile(r'(?<![a-z])' + label + r'(?![a-z])[^$\n]{0,60}(?:\n[^$\n]{0,60}){0,2}?'
                                r'\$\s?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)', re.I)
                self._total_rx[label] = rx
            hits = list(rx.finditer(text or ''))
            if hits:
                return cents(hits[-1].group(1))
        return None


# ---- grants -----------------------------------------------------------------------------------------------------------

class Grants:
    """buy-grants.json in the life folder. Every change happens under an exclusive file lock and is written to a temp
    file, fsynced and renamed into place, so a crash leaves either the old or the new file, never half of one."""

    def __init__(self, life: Path):
        self.life = Path(life)
        self.path = self.life / GRANTS_FILE

    @contextlib.contextmanager
    def locked(self, wait: float = 5.0):
        import fcntl
        self.life.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.life / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TimeoutError('the grants file is locked by another step')
                    time.sleep(0.02)
            yield
        finally:
            os.close(fd)

    def load(self) -> dict:
        """A missing file is an empty list; a file that does not parse raises (callers fail closed)."""
        if not self.path.exists():
            return {'version': 1, 'grants': []}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('grants'), list):
            raise ValueError('buy-grants.json has an unexpected shape')
        return data

    def save(self, data: dict):
        tmp = self.path.with_name(self.path.name + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        try:
            dfd = os.open(self.life, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    @staticmethod
    def move(g: dict, state: str, by: str, note: str = '', now: float | None = None, **extra):
        now = time.time() if now is None else now
        g['state'] = state
        g.update(extra)
        g.setdefault('history', []).append({'state': state, 'at': now, 'by': by, 'note': note})

    def sweep(self, data: dict, now: float) -> bool:
        """Approved past its 2 hours -> expired. `ordering` that nobody finished in 10 minutes -> unknown."""
        changed = False
        for g in data['grants']:
            if g.get('state') == 'approved' and now > float(g.get('expires_at') or 0):
                self.move(g, 'expired', 'sweep', 'the yes ran out after 2 hours', now)
                changed = True
            elif g.get('state') == 'ordering' and now - float(g.get('pressed_at') or g.get('at') or 0) > STUCK_ORDERING:
                self.move(g, 'unknown', 'sweep', 'Place order was being pressed and nothing reported back', now)
                changed = True
        return changed

    def current(self, now: float | None = None) -> dict:
        """Load and sweep under the lock (writes only when the sweep changed something)."""
        now = time.time() if now is None else now
        with self.locked():
            data = self.load()
            if self.sweep(data, now):
                self.save(data)
            return data

    def claim(self, gid: str, now: float | None = None, **extra) -> dict | None:
        """approved -> ordering, written and fsynced. None if it is no longer approved (another step took it)."""
        now = time.time() if now is None else now
        with self.locked():
            data = self.load()
            self.sweep(data, now)
            for g in data['grants']:
                if g.get('id') == gid and g.get('state') == 'approved':
                    self.move(g, 'ordering', 'purchase lock', 'about to press Place order once', now, pressed_at=now, **extra)
                    self.save(data)
                    return dict(g)
            self.save(data)
        return None

    def finish(self, gid: str, state: str, note: str, from_states=('ordering',), by: str = 'purchase lock', **extra) -> bool:
        with self.locked():
            data = self.load()
            for g in data['grants']:
                if g.get('id') == gid and g.get('state') in from_states:
                    self.move(g, state, by, note, **extra)
                    self.save(data)
                    return True
        return False


def latest(grants: list, store: str | None = None, states=None) -> dict | None:
    rows = [g for g in grants if (store is None or g.get('store') == store) and (states is None or g.get('state') in states)]
    return max(rows, key=lambda g: float(g.get('at') or 0)) if rows else None


# ---- the log ----------------------------------------------------------------------------------------------------------

def write_log(life: Path, row: dict):
    try:
        life.mkdir(parents=True, exist_ok=True)
        row = {'at': time.time(), 'version': VERSION, **row}
        with open(life / LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
    except OSError:
        pass


# ---- what the model reads ---------------------------------------------------------------------------------------------
# Plain words. Each may be repeated to the owner as is.

def settle_rule(st: Stores, sid: str | None) -> str:
    where = st.orders_url(sid)
    page = f'the {st.name(sid)} orders page ({where})' if where else f'the {st.name(sid)} orders page'
    return (f'Never press Place order again. Open {page} and read it. If the order is there, run: zeus-buy placed {sid or "<store>"} '
            f'<order number>. If it is not there, run: zeus-buy notplaced {sid or "<store>"} and tell the owner it did not go through.')


BOOKING_REASONS = ('purchase_needs_his_yes', 'raw_needs_his_yes', 'total_above_his_yes', 'already_ordered',
                   'booking_details_differ', 'booking_details_unreadable', 'booking_needs_details')


def booking_message(st: Stores, reason: str, *, store=None, total=None, grant=None, recent=None, booking_page=None,
                    diffs=None) -> str | None:
    """The texts at a booking store: the ask names the trip (date in words, flights, airports, passengers, total,
    fare and refund words) and the command carries the trip. None = use the ordinary text."""
    if reason not in BOOKING_REASONS:
        return None
    cfg = st.booking(store)
    B = booking_module()
    name, btn = st.name(store), cfg['button']
    g = grant or {}
    want = g.get('booking') or {}
    page_trip = B.from_page(booking_page) if booking_page else {}
    trip = B.merged(page_trip, want)                      # what the page shows, the recorded trip filling the gaps
    ask_total = total if total is not None else (int(g['total_cents']) if g.get('total_cents') else None)
    ask = B.ask_line(cfg, name, trip, ask_total)
    cmd_total = amount(ask_total) if ask_total is not None else '<total>'
    cmd = (f'zeus-buy yes {store} {cmd_total} "{B.what(cfg, trip)}" "<their exact words>" {B.cmd_flags(cfg, trip)}')
    then = f'then press {btn} again with click_element.'
    if reason == 'purchase_needs_his_yes':
        text = ("Not booked. Booking needs the owner's yes to this exact trip first: the date, flights, airports, passengers and "
                'total. Before you ask, take out anything they did not ask for: EarlyBird, upgrades, trip insurance or other '
                'add-ons the site pre-selected (if the site will not let you remove one, say so in the ask). Ask them in one '
                f'line, for example: "{ask}" When they say yes, run: {cmd}, {then}')
        if recent:
            text += (f' Note: a {name} booking was already pressed at {clock(recent.get("pressed_at") or recent.get("at"))} '
                     f'({recent.get("what") or "earlier booking"}, now {recent.get("state", "").replace("_", " ")}). Make sure '
                     'this is not the same trip twice.')
        return text
    if reason == 'raw_needs_his_yes':
        return ("Not booked. Booking needs the owner's yes to this exact trip first, and then a click on the "
                f'{btn} button itself (click_element), never a key, a script or raw browser input. Ask them in one line, for '
                f'example: "{ask}" When they say yes, run: {cmd}, then press {btn} with click_element.')
    if reason == 'total_above_his_yes':
        return (f"Not booked. The trip total on the page is {dollars(total)}, but the owner's yes covers up to "
                f"{dollars(int(g['total_cents']))} ({g.get('what') or 'this trip'}). Do not change the trip to make it fit. "
                f'Tell them the new total in one line and ask again, for example: "{ask}" When they say yes, run: {cmd}, {then}')
    if reason == 'already_ordered':
        when = clock(g.get('pressed_at') or g.get('at'))
        yes = (f" The owner's yes covered up to {dollars(int(g['total_cents']))} for {g.get('what') or 'this trip'}." if g else '')
        where = st.orders_url(store)
        page = f"the {cfg.get('trips_page_name') or name + ' trips page'} ({where})" if where else f'the {name} trips page'
        return (f'Not booked again. {btn} was already pressed for this {name} trip at {when}.{yes} It may have gone through. '
                f'Never press {btn} again. Open {page} and read it. If the trip is there, run: zeus-buy placed {store} '
                f'<confirmation number>. If it is not there, run: zeus-buy notplaced {store} and tell the owner it did not go through.')
    yes_was = B.describe(cfg, want) if want else (g.get('what') or 'the trip they approved')
    if reason == 'booking_details_differ':
        return (f"Not booked. This {name} page does not match the owner's yes: {'; '.join(diffs or ['a detail differs'])}. Their yes "
                'covers only the trip they were asked about. Ask them again in one line with what this page shows, for example: '
                f'"{ask}" When they say yes, run: {cmd}, {then} If the page is wrong rather than their yes, go back and pick the '
                f'trip they said yes to ({yes_was}).')
    if reason == 'booking_details_unreadable':
        return (f'Not booked. The purchase lock could not read the trip on this page (date, flight, airports, passengers), so '
                f"it cannot check it against the owner's yes ({yes_was}, up to {dollars(int(g['total_cents']))}). Go to the final "
                f'{name} review page that shows the flights and the passenger names, then press {btn} there with '
                'click_element. Do not try another way.')
    if reason == 'booking_needs_details':
        return (f"Not booked. The owner's yes for {name} (up to {dollars(int(g['total_cents']))}, {g.get('what') or 'a trip'}) was "
                'recorded without the trip details (date, flights, airports, passengers), and a booking needs them. Ask them '
                f'again in one line, for example: "{ask}" When they say yes, run: {cmd}, {then}')
    return None


def message(st: Stores, reason: str, *, store=None, total=None, grant=None, host='', recent=None, why='', token='',
            booking_page=None, diffs=None) -> str:
    if getattr(st, 'is_booking', None) and st.is_booking(store):
        try:
            text = booking_message(st, reason, store=store, total=total, grant=grant, recent=recent,
                                   booking_page=booking_page, diffs=diffs)
            if text:
                return text
        except Exception:
            if reason.startswith('booking_'):           # a refusal still refuses; only the wording falls back
                return (f'Not booked ({reason}). The trip on this page could not be checked against the owner\'s yes. Ask them '
                        'again in one line with the date, flights, airports, passengers and total, and record their yes with '
                        'zeus-buy yes and those details.')
    name = st.name(store)
    g = grant or {}
    his = f"the owner's yes covers up to {dollars(int(g['total_cents']))} ({g.get('what') or 'this order'})" if g else ''
    ask_total = dollars(total) if total is not None else '$<total>'
    cmd_total = amount(total) if total is not None else '<total>'
    if reason == 'purchase_needs_his_yes':
        text = ('Not placed. Placing an order needs the owner\'s yes to this store and total first. Before you ask, take out anything they '
                'did not ask for: memberships, free trials, subscriptions, protection plans, tips or add-ons the store pre-selected '
                '(if the store will not let you remove one, say so in the ask). Ask them in one line, for example: '
                f'"{name} order is {ask_total} for <what it is>, place it?" When they say yes, run: zeus-buy yes {store or "<store>"} '
                f'{cmd_total} "<what it is>" "<their exact words>", then press Place order again with click_element.')
        if recent:
            text += (f' Note: an order at {name} was already pressed at {clock(recent.get("pressed_at") or recent.get("at"))} '
                     f'({recent.get("what") or "earlier order"}, now {recent.get("state", "").replace("_", " ")}). Make sure this is not the same order twice.')
        return text
    if reason == 'raw_needs_his_yes':
        return ('Not placed. Placing an order needs the owner\'s yes to this store and total first, and then a click on the Place order '
                'button itself (click_element), never a key, a script or raw browser input. Ask them in one line, for example: '
                f'"{name} order is {ask_total} for <what it is>, place it?" When they say yes, run: zeus-buy yes {store or "<store>"} '
                f'{cmd_total} "<what it is>" "<their exact words>", then press Place order with click_element.')
    if reason == 'total_above_his_yes':
        return (f'Not placed. The order total on the page is {dollars(total)}, but {his}. Do not change the cart to make it fit. '
                f'Tell them the new total in one line and ask again, for example: "{name} total is now {dollars(total)}, not '
                f'{dollars(int(g["total_cents"]))}. Place it?" When they say yes, run: zeus-buy yes {store} {amount(total)} '
                f'"{g.get("what") or "<what it is>"}" "<their exact words>", then press Place order again with click_element.')
    if reason == 'total_unreadable':
        return (f'Not placed. The purchase lock could not read the order total on this page, so it cannot check it against '
                f'the owner\'s yes ({dollars(int(g["total_cents"]))} at {name}). Go to the normal checkout review page where the '
                'order total is shown, then press Place order there with click_element. Do not try another way.')
    if reason == 'store_mismatch':
        return (f"Not placed. The owner's yes is for {st.name(g.get('store'))} (up to {dollars(int(g['total_cents']))}, "
                f"{g.get('what') or 'that order'}), but this page is {name}. A yes covers one store only. Ask them about "
                f'this store first, in one line, with its total.')
    if reason == 'store_not_on_list':
        known = ', '.join(s['name'] for s in st.stores.values())
        return (f'Not placed. This page ({host or "this site"}) is not one of the stores the purchase lock knows ({known}), '
                'so nothing can be bought here. Tell the owner in one line where you were trying to buy and what, and stop.')
    if reason == 'already_ordered':
        when = clock(g.get('pressed_at') or g.get('at'))
        yes = (f" The owner's yes covered up to {dollars(int(g['total_cents']))} for {g.get('what') or 'this order'}." if g else '')
        return (f'Not placed again. Place order was already pressed for this {name} order at {when}.{yes} It may have gone '
                'through. ' + settle_rule(st, store))
    if reason == 'place_order_by_click_only':
        return ('Not placed. On a checkout page, Place order can only be pressed with click_element("<selector of the Place '
                'order button>") (or click_at_xy on it), so the purchase lock can check the store, the total and that it '
                'happens once. Keys, scripts and raw browser input are not allowed here. Nothing was pressed.')
    if reason == 'one_click_only':
        return 'Not placed. Press Place order with one single left click (clicks=1). Nothing was pressed.'
    if reason == 'checkout_script_not_read_only':
        return (f'Not run. On a checkout or store page, scripts may only read the page. This script has code that could press '
                f'a button without showing it (it uses "{token}"), so it did not run. Read the page with a plain script (no click, '
                'submit, events, eval, timers, fetch or ["..."] lookups) and press buttons with click_element.')
    if reason == 'booking_script_changes_page':
        return (f'Not run. On the {name} purchase page, scripts may only read the page. This script changes what the page '
                f'shows (it uses "{token}"), and the purchase lock checks the trip by reading the page, so it did not run. '
                'Type into boxes with fill_input, press buttons with click_element, and read the page with a plain script.')
    if reason == 'not_owner_turn':
        return ("Not placed. Only the owner's own chat turn can press Place order, Pay or Buy now; a background job, a "
                'helper or anyone else never can. Nothing was pressed. Tell the owner in one line that the order is ready '
                'and waits for their yes in your chat.')
    if reason == 'target_unknown':
        return ('Not done. On this checkout page the purchase lock could not tell what this click would hit, so it did not '
                'send it. Press buttons with click_element("<selector>") so the lock can read the button. If this is Place '
                "order, it also needs the owner's yes first (zeus-buy yes); ask them in one line.")
    if reason == 'page_unreadable':
        return ('Not done. The purchase lock could not read the page to check this step (it may still be loading), so '
                'it did not send it. Wait for the page to load and try once more. If it keeps happening on a store page, '
                'tell the owner in one line and ask before going on.')
    if reason == 'lock_error':
        return (f'Not placed. The purchase lock hit a problem ({why or "unknown"}), so it blocks Place order and Pay buttons '
                'until it is fixed. Everything else in the browser still works. Tell the owner in one line that the purchase lock '
                'needs a look; do not try another way.')
    return f'Not placed ({reason}).'


def pass_message(st: Stores, store: str, total: int, grant: dict) -> str:
    if st.is_booking(store):
        try:
            cfg, B = st.booking(store), booking_module()
            btn = cfg['button']
            return (f"{btn} was pressed once for {st.name(store)} (page total {dollars(total)}, the owner's yes up to "
                    f"{dollars(int(grant['total_cents']))}; the trip on the page matched their yes: "
                    f"{B.describe(cfg, grant.get('booking') or {})}). Now read the confirmation page. If you see a "
                    f'confirmation number, run: zeus-buy placed {store} <confirmation number>. If the page is unclear, open '
                    f'{st.orders_url(store) or "the trips page"} and read it. Never press {btn} again for this trip.')
        except Exception:
            pass
    return (f'Place order was pressed once for {st.name(store)} (page total {dollars(total)}, the owner\'s yes up to '
            f'{dollars(int(grant["total_cents"]))}). Now read the confirmation page. If you see an order number, run: '
            f'zeus-buy placed {store} <order number>. If the page is unclear, open {st.orders_url(store) or "the orders page"} '
            'and read it. Never press Place order again for this order.')


# ---- reading the page -------------------------------------------------------------------------------------------------
# One extra read per guarded action. It never changes the page. `[onclick]` below is a selector, not a click.

PROBE = r"""/*purchase-lock probe*/(()=>{const A=__ARGS__;
const t=s=>String(s==null?'':s).replace(/\s+/g,' ').trim().slice(0,160);
const vis=e=>{try{const r=e.getBoundingClientRect(),c=getComputedStyle(e);return r.width>0&&r.height>0&&c.visibility!=='hidden'&&c.display!=='none'}catch(_){return false}};
const by=id=>{const n=id&&document.getElementById(id);return n?n.innerText:''};
const labels=e=>{if(!e||!e.getAttribute)return [];const o=[e.innerText,e.value,e.getAttribute('aria-label'),e.title,e.getAttribute('alt')];
 const lb=e.getAttribute('aria-labelledby');if(lb)o.push(lb.split(/\s+/).map(by).join(' '));
 if(e.labels)for(const l of e.labels)o.push(l.innerText);
 const p=e.parentElement;if(p&&(p.innerText||'').length<=80)o.push(p.innerText);
 return o.map(t).filter(Boolean)};
const attrs=e=>e&&e.getAttribute?t([e.id,e.getAttribute('name'),typeof e.className==='string'?e.className:'',e.getAttribute('data-automation-id'),e.getAttribute('data-testid'),e.getAttribute('data-test'),e.getAttribute('data-qa')].join(' ')):'';
const CL='button,a[href],input[type=submit],input[type=button],input[type=image],[role=button],[role=link],[onclick]';
const desc=e=>e&&e.tagName?{tag:e.tagName.toLowerCase(),type:(e.getAttribute('type')||'').toLowerCase(),labels:labels(e),attrs:attrs(e),editable:!!e.isContentEditable}:null;
const PRE=/order|buy|pay|purchase|book|reserv|submit|confirm|complete|checkout/i;
const buttons=[];for(const e of document.querySelectorAll(CL)){if(buttons.length>=300)break;const ls=labels(e),a=attrs(e);if((PRE.test(ls.join(' '))||PRE.test(a))&&vis(e))buttons.push({labels:ls.slice(0,5),attrs:a})}
let hit=null;if(A.xy){const h=document.elementFromPoint(A.xy[0],A.xy[1]);if(h){const c=h.closest(CL)||h;hit={self:desc(h),clickable:desc(c),frame:/^(IFRAME|FRAME|EMBED|OBJECT)$/.test(h.tagName)}}}
const f=document.activeElement;
const NF=/first|given|middle|last|family|surname|passenger|travel+er|name/i;
const SF=/card|cc-|cvv|cvc|csc|secur|passw|e-?mail|phone|mobile|address|street|city|state|zip|postal|country|promo|coupon|gift|account|rapid|member|known|ktn|redress|number|birth|dob|search|emergency|contact|billing|company|user|login/i;
const fields=[];for(const e of document.querySelectorAll('input')){if(fields.length>=40)break;const ty=(e.getAttribute('type')||'text').toLowerCase();if(ty!=='text')continue;
 const lab=(e.labels?Array.from(e.labels).map(l=>l.innerText).join(' '):'')+' '+(e.getAttribute('aria-labelledby')||'').split(/\s+/).map(by).join(' ');
 const own=[e.getAttribute('autocomplete')||'',lab,e.getAttribute('aria-label')||'',e.getAttribute('placeholder')||'',e.getAttribute('name')||'',e.id||''].join(' ');
 const p=e.parentElement,par=p&&(p.innerText||'').length<=80?p.innerText:'';
 if(!NF.test(own+' '+par)||SF.test(own)||!vis(e))continue;
 fields.push({ac:t(e.getAttribute('autocomplete')),lab:t(lab),aria:t(e.getAttribute('aria-label')),ph:t(e.getAttribute('placeholder')),name:t(e.getAttribute('name')),id:t(e.id),parent:t(par),value:t(e.value)})}
return JSON.stringify({href:location.href,text:(document.body?document.body.innerText:'').slice(0,300000),buttons,hit,focus:f&&f!==document.body?desc(f):null,fields});
})()"""


RESERVATION_STORES = ('trip-hotel', 'opentable')

def reservation_adapter():
    global _reservation_adapter
    if '_reservation_adapter' not in globals():
        import importlib.util
        spec = importlib.util.spec_from_file_location('_zeus_reservations', Path(__file__).with_name('reservation_adapter.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _reservation_adapter = mod
    return _reservation_adapter

def probe_script(xy=None) -> str:
    return PROBE.replace('__ARGS__', json.dumps({'xy': xy}))


def page_facts(st: Stores, probe: dict) -> dict:
    href = str(probe.get('href') or '')
    u = urlsplit(href)
    host = (u.hostname or '').lower()
    sid = st.for_host(host)
    if sid=='trip-hotel' and not reservation_adapter().hotel_path(u.path): sid=None
    text = str(probe.get('text') or '')
    buttons = probe.get('buttons') or []
    strong_btn = any(st.is_strong(b.get('labels')) or st.is_commit_attr(b.get('attrs')) for b in buttons)
    total = st.read_total(text, sid)
    url_commit = st.commit_url(sid, u.path)
    commit_page = bool(url_commit or (strong_btn and total is not None))
    if sid in RESERVATION_STORES:
        final = reservation_adapter().FINALS
        commit_page = commit_page or any(final.fullmatch(label.strip()) for b in buttons for label in (b.get('labels') or [])[:1])
    return {'href': href, 'page': public_page(href), 'host': host, 'store': sid, 'total': total, 'strong_button': strong_btn,
            'commit_page': commit_page, 'url_commit': url_commit, 'dialog': probe.get('dialog'),
            'hot': bool(commit_page or (sid and strong_btn)),
            'text': text, 'fields': probe.get('fields') or []}     # for the booking check; never logged


def click_kind(st: Stores, facts: dict, hit: dict | None) -> str | None:
    """'click' (a purchase button), 'frame' (a box from another site on a checkout page) or None."""
    if not hit:
        return None
    if hit.get('frame'):
        return 'frame' if facts['commit_page'] else None
    c, s = hit.get('clickable') or {}, hit.get('self') or {}
    labels = list(c.get('labels') or []) + list(s.get('labels') or [])
    attrs = f"{c.get('attrs') or ''} {s.get('attrs') or ''}"
    if facts['store'] in RESERVATION_STORES and (any(reservation_adapter().FINALS.fullmatch(v.strip()) for v in labels) or
            (facts['commit_page'] and c.get('type') in ('submit','image'))):
        return 'click'
    if (st.is_strong(labels) or st.is_commit_attr(attrs)) and (facts['store'] or facts['commit_page']):
        return 'click'
    if st.is_weak(labels) and facts['commit_page']:
        return 'click'
    if facts['commit_page'] and c.get('type') in ('submit', 'image') and not any(norm(x) for x in labels):
        return 'click'          # an unlabeled submit control on a checkout page: fail closed
    return None


def key_name(key) -> str:
    k = str(key if key is not None else 'Enter')
    return k if len(k) == 1 else k.lower()


def key_is_press(key, focus: dict | None) -> bool:
    k = key_name(key)
    if k in ENTER_KEYS:
        return True
    if k in SPACE_KEYS:
        f = focus or {}
        typing = (f.get('tag') == 'input' and f.get('type') in TEXT_INPUTS) or f.get('tag') == 'textarea' or f.get('editable')
        return not typing
    return False


def risky_cdp(method: str, params: dict) -> str:
    """'' = harmless; 'press' = raw input or a script that presses; 'hidden' = a script that could hide a press."""
    m = str(method or '')
    p = params if isinstance(params, dict) else {}
    if m.startswith('Input.'):
        if m == 'Input.dispatchMouseEvent':
            return '' if p.get('type') in ('mouseMoved', 'mouseWheel') else 'press'
        if m == 'Input.dispatchKeyEvent':
            k = key_name(p.get('key') or p.get('code') or '')
            txt = str(p.get('text') or p.get('unmodifiedText') or '')
            return 'press' if k in ENTER_KEYS or k in SPACE_KEYS or k in ('enter', 'space', 'numpadenter') or txt in ('\r', '\n', ' ') \
                or p.get('windowsVirtualKeyCode') in (13, 32) else ''
        if m in ('Input.insertText', 'Input.imeSetComposition'):
            return 'press' if re.search(r'[\r\n]', str(p.get('text') or '')) else ''
        if m in ('Input.setIgnoreInputEvents', 'Input.setInterceptDrags', 'Input.cancelDragging'):
            return ''
        return 'press'
    if m in ('Runtime.evaluate', 'Runtime.callFunctionOn', 'Runtime.compileScript', 'Debugger.evaluateOnCallFrame',
             'Page.addScriptToEvaluateOnNewDocument'):
        src = str(p.get('expression') or p.get('functionDeclaration') or p.get('source') or '')
        if RAW_PRESS.search(src):
            return 'press'
        return 'hidden' if RAW_HIDDEN.search(src) else ''
    if m == 'Runtime.runScript':
        return 'hidden'
    if m == 'Page.handleJavaScriptDialog':
        return 'press' if p.get('accept', True) else ''
    if m in ('Target.sendMessageToTarget',):
        return 'press'
    return ''




# fill_input()'s own last step: it tells the page the box changed. Input and change events never press a button.
FILL_EVENTS = re.compile(r"""\(\(\)=>\{const e=document\.querySelector\("(?:[^"\\]|\\.)*"\);if\(!e\)return;"""
                         r"""e\.dispatchEvent\(new Event\('input',\{bubbles:true\}\)\);"""
                         r"""e\.dispatchEvent\(new Event\('change',\{bubbles:true\}\)\);\}\)\(\);""")


def request_risk(method: str, params: dict) -> str:
    """risky_cdp() for one browser request, plus the ways a request can run page code that risky_cdp does not look at:
    a javascript: address, DOM writes (an inserted onerror= handler runs code) and the debugger."""
    m = str(method or '')
    p = params if isinstance(params, dict) else {}
    if m in ('Runtime.evaluate', 'Runtime.callFunctionOn') and FILL_EVENTS.fullmatch(str(p.get('expression') or '').strip()):
        return ''
    if m == 'Page.navigate' and str(p.get('url') or '').strip().lower().startswith('javascript:'):
        return 'press'
    if m.startswith('Debugger.') or (DOM_WRITE_CDP.search(m) and m != 'DOM.setFileInputFiles'):
        return 'hidden'
    return risky_cdp(m, p)


# ---- code that could reach the browser around the fence -----------------------------------------------------------------
# browser_exec code is Python. The fence sits in front of the harness's one pipe; these are the ways around it.
# Checked by the gateway on Zeus's code and by the browser child on agent_helpers.py. Strings are never looked at
# (JavaScript inside js("...") is the fence's job), only Python names, attributes and imports.

AROUND_MODULES = {'browser_harness', 'importlib', 'inspect', 'gc', 'ctypes', 'socket', 'ssl', 'websocket', 'websockets',
                  'subprocess', 'multiprocessing', 'asyncio', 'aiohttp', 'pty', 'runpy', 'code', 'codeop', 'pickle',
                  'marshal', 'types', 'builtins', 'playwright', 'pyppeteer', 'pychrome', 'selenium', 'browser_use'}
AROUND_NAMES = {'getattr', 'setattr', 'delattr', 'vars', 'globals', 'locals', 'eval', 'exec', 'compile', '__import__',
                'breakpoint', 'ipc'}
AROUND_ATTRS = {'modules', 'system', 'popen', 'execv', 'execve', 'execl', 'execlp', 'execvp', 'spawnv', 'spawnl',
                'posix_spawn', 'fork', 'helpers', 'ipc'}


def reaches_around(code: str) -> str:
    """'' when the code only uses the browser through its helpers; else the name that could skip the fence."""
    import ast
    try:
        tree = ast.parse(code or '')
    except (SyntaxError, ValueError):
        return ''                       # it cannot run at all (the whole step is compiled first)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split('.')[0] in AROUND_MODULES:
                    return 'import ' + a.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or '').split('.')[0] in AROUND_MODULES:
                return 'from ' + ('.' * node.level) + (node.module or '') + ' import'
        elif isinstance(node, ast.Name):
            if node.id in AROUND_NAMES or node.id.startswith('_plk') or node.id == '_send':
                return node.id
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith('_') or node.attr in AROUND_ATTRS:
                return '.' + node.attr
    return ''


# ---- the fence (in the browser child, in front of browser_harness.helpers._send) -------------------------------------

def _xy(params: dict) -> tuple:
    return (params.get('x'), params.get('y'))


class Fence:
    """Every request a browser_exec step sends to the browser passes here first (browser_harness.helpers._send is the
    one pipe click_at_xy, js, cdp, press_key, fill_input and the rest use). A request that cannot press anything goes
    straight through. One that could is judged against the page it would land on, read by this fence first."""

    def __init__(self, settings: dict | None, send):
        self.settings = dict(settings or {})
        self.life = life_home(self.settings)
        self.grants = Grants(self.life)
        self.may_press = self.settings.get('may_press') is True      # only the owner's own chat turn
        self.send = send                                             # the harness's own pipe
        self._stores = None
        self.held = None            # a purchase press let through, until its mouseReleased has been sent
        self.harmless_xy = None     # the last harmless mousePressed point: its mouseReleased needs no second look
        self._exit_hook = False

    @property
    def st(self) -> Stores:
        if self._stores is None:
            self._stores = Stores(stores_path(self.settings))
        return self._stores

    def mode(self) -> str:
        return mode(self.life)

    def log(self, row: dict):
        write_log(self.life, row)

    def say(self, text: str):
        try:
            print('[purchase lock] ' + text, flush=True)
        except Exception:
            pass

    # reading (the fence's own reads go straight to the pipe)
    def tab(self) -> dict:
        try:
            return dict(self.send({'meta': 'current_tab'}) or {})
        except Exception:
            return {}

    def read(self, session_id=None, xy=None) -> dict:
        dialog = self.send({'meta': 'pending_dialog'}).get('dialog')
        if dialog:
            d = dialog if isinstance(dialog, dict) else {}
            return {'href': self.tab().get('url', ''), 'text': '', 'buttons': [], 'hit': None, 'focus': None,
                    'dialog': {'type': str(d.get('type') or 'dialog'), 'message': str(d.get('message') or '')[:300]}}
        r = self.send({'method': 'Runtime.evaluate', 'params': {'expression': probe_script(xy), 'returnByValue': True},
                       'session_id': session_id})
        value = ((r.get('result') or {}).get('result') or {}).get('value')
        if not isinstance(value, str):
            raise ValueError('the page did not answer the read')
        return json.loads(value)

    def booking_page(self, facts) -> bool:
        """A booking store's checkout page (where the trip reader decides)."""
        return bool(facts and facts.get('commit_page') and facts.get('store') and self.st.is_booking(facts['store']))

    def trip_for_text(self, sid, facts):
        """The page's trip at a booking store, only to fill in the example ask. Never decides anything; None on any
        problem (the refusal still happens, with placeholders)."""
        try:
            if not self.st.is_booking(sid):
                return None
            B = booking_module()
            return B.read_page(self.st.booking(sid), facts.get('text') or '', facts.get('fields') or [], B.today())
        except Exception:
            return None

    # judging
    def kind_of(self, method: str, params: dict, page: dict, facts: dict, session_id=None):
        """None (not a purchase step), 'click', 'frame', 'raw', 'hidden', 'rewrite' or 'unknown_target'."""
        hot = facts['hot'] or bool(facts['dialog'] and (facts['store'] or facts['url_commit']
                                                         or DIALOG_WORDS.search(str(facts['dialog'].get('message')))))
        if method == 'Input.dispatchMouseEvent':
            if facts['dialog']:
                return 'raw' if hot else None
            hit = page.get('hit')
            kind = click_kind(self.st, facts, hit)
            if kind is None and facts['commit_page'] and not (hit and (hit.get('clickable') or hit.get('self'))):
                return 'unknown_target'             # nothing readable under the pointer on a checkout page
            return kind
        risk = request_risk(method, params)
        if risk == 'press' and method == 'Input.dispatchKeyEvent':
            key = params.get('key') or params.get('code') or params.get('text') or 'Enter'
            if not key_is_press(key, page.get('focus')) and params.get('windowsVirtualKeyCode') != 13:
                risk = ''                           # a space typed into a text box
        if not risk and method in ('Runtime.evaluate', 'Runtime.callFunctionOn') and (
                self.booking_page(facts) or (facts['store'] in RESERVATION_STORES and facts['commit_page'])):
            write = RAW_WRITE.search(str(params.get('expression') or params.get('functionDeclaration') or ''))
            if write:
                return 'rewrite'
        if risk and not hot and session_id:
            hot = self.other_tabs_hot()         # a step aimed at another tab or a frame: any store tab counts
        if not hot or not risk:
            return None
        return 'hidden' if risk == 'hidden' else 'raw'

    def other_tabs_hot(self) -> bool:
        res = self.send({'method': 'Target.getTargets', 'params': {}, 'session_id': None}).get('result') or {}
        for t in res.get('targetInfos') or []:
            url = str(t.get('url') or '')
            if t.get('type') == 'page' and (self.st.for_host(urlsplit(url).hostname or '') or self.st.any_commit_url(url)):
                return True
        return False

    def judge(self, method: str, params: dict, session_id) -> dict:
        mouse = method == 'Input.dispatchMouseEvent'
        base = {'action': f"{method}:{params.get('type')}" if mouse or method == 'Input.dispatchKeyEvent' else method}
        try:
            page = self.read(session_id, list(_xy(params)) if mouse else None)
        except Exception as error:
            return self.refuse({**base, 'error': type(error).__name__}, 'page_unreadable')
        facts = page_facts(self.st, page)
        kind = self.kind_of(method, params, page, facts, session_id)
        base.update(store=facts['store'], page=facts['page'], page_total_cents=facts['total'], commit_page=facts['commit_page'])
        hit = page.get('hit') or {}
        if hit:
            c = hit.get('clickable') or hit.get('self') or {}
            base['button'] = (c.get('labels') or [''])[0][:60]
        if kind is None:
            return {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': bool(facts['commit_page'])}
        base['kind'] = kind
        expr = str(params.get('expression') or params.get('functionDeclaration') or '')
        if kind == 'hidden':
            token = RAW_HIDDEN.search(expr) or DOM_WRITE_CDP.search(method)
            return self.refuse(base, 'checkout_script_not_read_only', token=token.group(0) if token else method)
        if kind == 'rewrite':
            token = RAW_WRITE.search(expr)
            return self.refuse(base, 'booking_script_changes_page', reason_code='checkout_script_not_read_only',
                               token=token.group(0).strip() if token else '')
        if kind == 'unknown_target':
            return self.refuse(base, 'target_unknown')
        if not self.may_press:
            return self.refuse(base, 'not_owner_turn')
        one_left_click = (mouse and int(params.get('clickCount', 1) or 1) == 1 and str(params.get('button', 'left')) == 'left')
        sid = facts['store']
        if sid in RESERVATION_STORES:
            return self.judge_reservation(base, kind, one_left_click, page)
        if sid is None:
            return self.refuse(base, 'store_not_on_list', host=facts['host'])
        now = time.time()
        rows = self.grants.current(now)['grants']
        unsettled = latest(rows, sid, UNSETTLED)
        if unsettled:
            return self.refuse(base, 'already_ordered', grant=unsettled)
        live = [g for g in rows if float(g.get('expires_at') or 0) > now]
        mine = latest(live, sid, ('approved',))
        if mine is None:
            other = latest(live, None, ('approved',))
            if other is not None:
                return self.refuse(base, 'store_mismatch', grant=other)
            recent = latest([g for g in rows if now - float(g.get('pressed_at') or 0) < RECENT_PLACED], sid, ('placed', 'placed_unconfirmed'))
            return self.refuse(base, 'raw_needs_his_yes' if kind == 'raw' else 'purchase_needs_his_yes', recent=recent,
                               reason_code='purchase_needs_his_yes', booking_page=self.trip_for_text(sid, facts))
        base['grant'] = mine['id']
        base['grant_total_cents'] = int(mine['total_cents'])
        if kind == 'raw':
            return self.refuse(base, 'place_order_by_click_only', grant=mine)
        if not one_left_click:
            return self.refuse(base, 'one_click_only', grant=mine)
        total = facts['total']
        if total is None:
            return self.refuse(base, 'total_unreadable', grant=mine)
        if total > int(mine['total_cents']):
            return self.refuse(base, 'total_above_his_yes', grant=mine, booking_page=self.trip_for_text(sid, facts))
        if kind == 'frame':                     # a box from another site (card details): typing there spends nothing
            return {**base, 'verdict': 'allow', 'reason': 'frame_click_with_his_yes', 'log': True}
        if self.st.is_booking(sid):
            # A booking yes binds the trip. Errors here propagate: the caller refuses the press.
            cfg, B = self.st.booking(sid), booking_module()
            trip = B.read_page(cfg, facts.get('text') or '', facts.get('fields') or [], B.today())
            base['booking_page'] = B.summary(trip)
            want = mine.get('booking')
            if not want:
                return self.refuse(base, 'booking_needs_details', grant=mine, booking_page=trip)
            base['booking_want'] = {k: v for k, v in want.items() if not str(k).startswith('_')}
            res = B.compare(cfg, want, trip)
            if res['unreadable']:
                return self.refuse(base, 'booking_details_unreadable', grant=mine, booking_page=trip)
            if res['diffs']:
                base['differs'] = res['fields']
                base['diffs'] = res['diffs']
                return self.refuse(base, 'booking_details_differ', grant=mine, booking_page=trip, diffs=res['diffs'])
            base['booking_matches'] = True
        return {**base, 'verdict': 'press', 'reason': 'owners_yes_matches', 'grant_row': mine, 'log': True}

    def judge_reservation(self, base: dict, kind, one_left_click: bool, page: dict) -> dict:
        """Trip.com hotels and OpenTable: the approval lives in reservation_adapter's own store, claimed here."""
        held = {**base, 'verdict': 'refuse', 'reason': 'reservation_approval', 'log': True,
                'message': 'Reservation held. Open the exact review page and verify the current owner approval.'}
        if kind != 'click' or not one_left_click or self.mode() != 'enforce':
            return held
        try:
            path = self.life.parent.parent / 'state' / 'reservations'
            adapter = reservation_adapter()
            sandbox = None if path.resolve() == (adapter.PROFILE / 'state' / 'reservations').resolve() else path.parent.parent
            key = adapter.gate(path, self.tab()['targetId'], page, True, sandbox)
        except Exception:
            return held
        return {**base, 'verdict': 'press', 'reason': 'reservation_matched', 'reservation_key': key, 'log': True}

    def refuse(self, base: dict, which: str, reason_code: str | None = None, **kw) -> dict:
        text = message(self.st, which, store=base.get('store'), total=base.get('page_total_cents'), **kw)
        return {**base, 'verdict': 'refuse', 'reason': reason_code or which, 'message': text, 'log': True}

    def broken(self, method: str, error: BaseException) -> dict:
        """The fence itself failed (bad store table, unreadable grants, a bug): this request could press, so it is refused."""
        why = f'{type(error).__name__}: {str(error)[:120]}'
        return {'action': method, 'error': why, 'verdict': 'refuse', 'reason': 'lock_error', 'log': True,
                'message': message(self.st if self._stores else _NoStores(), 'lock_error', why=type(error).__name__)}

    # acting
    def __call__(self, req, *args, **kwargs):
        go = lambda: self.send(req, *args, **kwargs)
        if not isinstance(req, dict) or not req.get('method'):
            return go()                                  # daemon housekeeping (tabs, sessions, dialogs, events)
        method = str(req['method'])
        params = req.get('params') if isinstance(req.get('params'), dict) else {}
        mouse = method == 'Input.dispatchMouseEvent' and params.get('type') in ('mousePressed', 'mouseReleased')
        expr = str(params.get('expression') or params.get('functionDeclaration') or '')
        if not mouse and not request_risk(method, params) and not RAW_WRITE.search(expr):
            return go()
        m = self.mode()
        if m == 'off':
            return go()
        if mouse and params.get('type') == 'mouseReleased':
            if self.held and self.held['xy'] == _xy(params):
                return self.release(go)
            if self.harmless_xy == _xy(params):
                self.harmless_xy = None
                return go()
        try:
            d = self.judge(method, params, req.get('session_id'))
        except Exception as error:
            d = self.broken(method, error)
        row = {k: v for k, v in d.items() if k not in ('grant_row', 'log', 'message')}
        row['mode'] = m
        if d['verdict'] == 'allow':
            if mouse and params.get('type') == 'mousePressed':
                self.harmless_xy = _xy(params)
            if d.get('log'):
                self.log(row)
            return go()
        if d['verdict'] == 'refuse':
            if m == 'shadow':
                self.log({**row, 'verdict': 'would_refuse'})
                return go()
            self.log(row)
            self.say(d['message'])
            raise RuntimeError('[purchase lock] ' + d['message'])
        return self.press(go, params, d, row)

    def press(self, go, params: dict, d: dict, row: dict):
        """The one press the owner's yes covers: claimed on disk (fsync) before any input, then sent once."""
        if d.get('reservation_key'):
            self.held = {'xy': _xy(params), 'reservation': d['reservation_key'], 'row': row}
        else:
            g = d['grant_row']
            if self.grants.claim(g['id'], page_total_cents=d.get('page_total_cents'), page=d.get('page')) is None:
                again = self.refuse({k: d[k] for k in ('action', 'store', 'page', 'page_total_cents', 'commit_page', 'kind') if k in d},
                                    'already_ordered', grant=latest(self.grants.load()['grants'], d.get('store')) or g)
                self.log({**{k: v for k, v in again.items() if k not in ('log', 'message')}, 'mode': row.get('mode')})
                self.say(again['message'])
                raise RuntimeError('[purchase lock] ' + again['message'])
            self.held = {'xy': _xy(params), 'grant': g, 'd': d, 'row': row}
        if not self._exit_hook:                          # a press whose release never comes is still a press
            import atexit
            atexit.register(self.settle)
            self._exit_hook = True
        result = go()
        if params.get('type') == 'mouseReleased':
            self.settle()
        return result

    def release(self, go):
        try:
            return go()
        finally:
            self.settle()

    def settle(self):
        """After the press: the yes is spent (placed_unconfirmed) and Zeus is told to read the store, never press again."""
        held, self.held = self.held, None
        if not held:
            return
        if held.get('reservation'):
            self.log({**held['row'], 'verdict': 'pass', 'pressed': 1})
            self.say('The reservation button was pressed once. Read the provider confirmation page before anything else.')
            return
        g, d = held['grant'], held['d']
        self.grants.finish(g['id'], 'placed_unconfirmed', 'Place order was pressed once; the store has not been read yet')
        self.log({**held['row'], 'verdict': 'pass', 'pressed': 1})
        self.say(pass_message(self.st, d['store'], int(d['page_total_cents']), g))


class _NoStores:
    """Stand-in for message() when the store table itself could not be loaded."""
    stores = {}

    def name(self, sid):
        return 'this store'

    def orders_url(self, sid):
        return ''


# ---- arming the fence in a browser_exec step --------------------------------------------------------------------------

CLICK_ELEMENT_JS = ("(()=>{const e=document.querySelector(%s);if(!e)return null;e.scrollIntoView({block:'center'});"
                    "const b=e.getBoundingClientRect();return [b.left+b.width/2,b.top+b.height/2]})()")


def arm(namespace: dict, settings: dict | None = None) -> Fence:
    """Called by the prelude the gateway puts in front of Zeus's browser_exec code. Puts the Fence in front of the
    harness pipe and adds click_element(selector). Raises when it cannot (the prelude then stops the step)."""
    helpers = sys.modules['browser_harness.helpers']
    workspace_helpers = Path(getattr(helpers, 'AGENT_WORKSPACE', '') or '.') / 'agent_helpers.py'
    if workspace_helpers.is_file():
        around = reaches_around(workspace_helpers.read_text(encoding='utf-8', errors='replace'))
        if around:
            raise PermissionError(f'agent_helpers.py in the browser workspace uses "{around}", which could skip the '
                                  'purchase lock. Take that out of agent_helpers.py.')
    send = helpers._send
    if isinstance(send, Fence):
        return send
    fence = Fence(settings, send)
    fence.st                                            # load the store table now: a broken table stops the step here
    helpers._send = fence

    def click_element(selector):
        """Click the middle of the element `selector` (one left click, through the purchase lock)."""
        xy = namespace['js'](CLICK_ELEMENT_JS % json.dumps(selector))
        if not xy:
            raise RuntimeError(f'click_element: no element for {selector!r}')
        namespace['click_at_xy'](xy[0], xy[1])

    namespace.setdefault('click_element', click_element)
    try:                                                # heartbeat: proof the lock armed itself in a real browser step
        beat = Path(fence.life) / 'buy-lock-heartbeat.json'
        tmp = beat.with_name('.buy-lock-heartbeat.json.tmp')
        tmp.write_text(json.dumps({'armed_at': time.time(), 'pid': os.getpid(), 'version': VERSION,
                                   'may_press': fence.may_press}))
        os.replace(tmp, beat)
    except Exception:
        pass
    return fence

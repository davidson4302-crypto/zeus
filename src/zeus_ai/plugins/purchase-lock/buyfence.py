"""buyfence: Zeus's purchase lock, inside the browser child.

What it does. Zeus drives its own Chrome through browser_exec. With action receipts on, every click, key, raw script
and raw CDP call goes through ActionReceipts.run_action (Hermes tools/browser_use_actions.py). install() wraps that
one choke point. Before any input reaches the page, code (never the model) decides:

  * Is this a purchase step? A click on a Place order / Buy now / Pay now style button (on one of the owner's stores, or on
    any checkout page), or on a checkout page: the Enter key, raw CDP input, a script that clicks or submits.
  * If it is, is there a grant? A grant is the owner's real yes to THIS store and a total, recorded by `zeus-buy yes`
    (their own message, read by code). No grant: refused, nothing is sent to the page.
  * With a grant: the order total is read off the page. Unreadable, or above their yes, or a different store: refused.
  * Pass: the grant is marked `ordering` on disk (fsync) BEFORE input, the click is dispatched exactly once, and the
    grant becomes `placed_unconfirmed`. Any later purchase step at that store is refused `already_ordered` and told to
    read the store's orders page, never to click again.
  * Raw input (keys, scripts, raw CDP) can never place an order, even with a grant: it cannot be tied to one button and
    one press. Only click_element / click_at_xy on the button itself can.

Every decision on a purchase step or a checkout page is appended to buy-log.jsonl in the life folder.
Switch files in the life folder: buy-lock-off (do nothing), buy-lock-shadow (log, refuse nothing). None = enforce.
If anything here errors, purchase steps are refused (fail closed for money) and everything else goes ahead.

A second, deeper belt: browser_harness.helpers._send (the one pipe all harness CDP traffic uses in this child) is
wrapped too, so code that skips the receipts layer (`from browser_harness import helpers; helpers.click_at_xy(...)`)
still cannot send raw input to a store or checkout page.

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

VERSION = 'purchase-lock-2.2'
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
    if reason == 'raw_browser_input':
        return ('Not done. A store or checkout page is open, and this step sends raw input to the browser outside the purchase '
                'lock. On these pages press buttons with click_element(...) only; Place order also needs the owner\'s yes first '
                '(zeus-buy yes). Do not try another way.')
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


# ---- the fence --------------------------------------------------------------------------------------------------------

class Fence:
    def __init__(self, settings: dict | None = None):
        self.settings = dict(settings or {})
        self.life = life_home(self.settings)
        self.grants = Grants(self.life)
        self._stores = None
        self._local = threading.local()

    # small helpers
    @property
    def st(self) -> Stores:
        if self._stores is None:
            self._stores = Stores(stores_path(self.settings))
        return self._stores

    def mode(self) -> str:
        return mode(self.life)

    @contextlib.contextmanager
    def permit(self):
        self._local.depth = getattr(self._local, 'depth', 0) + 1
        try:
            yield
        finally:
            self._local.depth -= 1

    def permitted(self) -> bool:
        return getattr(self._local, 'depth', 0) > 0

    @staticmethod
    @contextlib.contextmanager
    def ctx(ar, value: dict):
        prev = getattr(ar, '_plk_ctx', None)
        ar._plk_ctx = value
        try:
            yield
        finally:
            ar._plk_ctx = prev

    def log(self, row: dict):
        write_log(self.life, row)

    def say(self, text: str):
        try:
            print('[purchase lock] ' + text, flush=True)
        except Exception:
            pass

    # reading
    def probe(self, ar, xy=None) -> dict:
        info = ar.raw['page_info']()
        if isinstance(info, dict) and info.get('dialog'):
            d = info['dialog'] if isinstance(info['dialog'], dict) else {}
            return {'href': (getattr(ar, 'last_state', {}) or {}).get('url', ''), 'text': '', 'buttons': [], 'hit': None,
                    'focus': None, 'dialog': {'type': str(d.get('type') or 'dialog'), 'message': str(d.get('message') or '')[:300]}}
        raw = ar.raw['js'](probe_script(xy))
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})

    def other_tabs_hot(self, cdp) -> bool:
        """Is any page tab on one of the stores or on a checkout URL? (used when a raw step names another tab)"""
        res = cdp('Target.getTargets') or {}
        for t in res.get('targetInfos') or []:
            if t.get('type') == 'page':
                url = str(t.get('url') or '')
                if self.st.for_host(urlsplit(url).hostname or '') or self.st.any_commit_url(url):
                    return True
        return False

    # judging
    def classify(self, ar, action: str, ctx: dict, target) -> tuple:
        """-> (kind, facts, extra). kind: None (not a purchase step), 'click', 'frame', 'raw', 'hidden', or
        ('target', reason) when the click target itself cannot be resolved."""
        kind_ctx = ctx.get('kind') or action
        if action == 'click':
            resolved = target() if target else None
            if resolved is None:
                xy = ctx.get('xy')
            elif resolved.get('refused'):
                return ('target', resolved['refused']), None, {'resolved': resolved}
            else:
                xy = [resolved.get('x'), resolved.get('y')]
            p = self.probe(ar, xy)
            facts = page_facts(self.st, p)
            if facts['dialog']:
                return ('raw' if (facts['store'] or facts['url_commit']) else None), facts, {}
            kind = click_kind(self.st, facts, p.get('hit'))
            return kind, facts, {'hit': p.get('hit')}
        if action in ('press_key', 'dispatch_key', 'type_text', 'raw_script', 'raw_cdp'):
            p = self.probe(ar)
            facts = page_facts(self.st, p)
            hot = facts['hot'] or (facts['dialog'] and (facts['store'] or facts['url_commit']))
            if action in ('press_key', 'dispatch_key'):
                key = ctx.get('key') if kind_ctx == action else None       # unknown key: judged as Enter (fail closed)
                press = key_is_press(key if key is not None else 'Enter', p.get('focus'))
                return ('raw' if press and hot else None), facts, {}
            if action == 'type_text':
                txt = ctx.get('text')
                return ('raw' if hot and (txt is None or re.search(r'[\r\n]', str(txt))) else None), facts, {}
            if action == 'raw_script':
                expr = ctx.get('expression')
                if expr is None:
                    return ('raw' if hot else None), facts, {}
                if RAW_PRESS.search(str(expr)):
                    risk = 'press'
                elif RAW_HIDDEN.search(str(expr)):
                    risk = 'hidden'
                else:
                    risk = ''
                if risk and ctx.get('target_id') and not hot:
                    hot = self.other_tabs_hot(ar.raw['cdp'])
                token = (RAW_PRESS.search(str(expr)) or RAW_HIDDEN.search(str(expr)))
                if not risk and (self.booking_page(facts) or facts['store'] in RESERVATION_STORES and facts['commit_page']):
                    write = RAW_WRITE.search(str(expr))
                    if write:
                        return 'rewrite', facts, {'token': write.group(0).strip()}
                return ({'press': 'raw', 'hidden': 'hidden'}.get(risk) if hot else None), facts, {'token': token.group(0) if token else ''}
            # raw_cdp
            method = ctx.get('method')
            if method is None:
                return ('raw' if hot else None), facts, {}
            risk = risky_cdp(method, ctx.get('params') or {})
            if not risk and (self.booking_page(facts) or facts['store'] in RESERVATION_STORES and facts['commit_page']):
                src = str((ctx.get('params') or {}).get('expression') or (ctx.get('params') or {}).get('functionDeclaration') or '')
                write = DOM_WRITE_CDP.search(str(method)) or RAW_WRITE.search(src)
                if write:
                    return 'rewrite', facts, {'token': write.group(0).strip()}
            if risk and ctx.get('session_id') and not hot:
                hot = self.other_tabs_hot(ar.raw['cdp'])
            if risk == 'hidden':
                src = str((ctx.get('params') or {}).get('expression') or (ctx.get('params') or {}).get('functionDeclaration') or '')
                token = RAW_HIDDEN.search(src)
                return ('hidden' if hot else None), facts, {'token': token.group(0) if token else method}
            return ('raw' if risk and hot else None), facts, {}
        return None, None, {}

    def judge(self, ar, action: str, ctx: dict, target) -> dict:
        kind, facts, extra = self.classify(ar, action, ctx, target)
        base = {'action': action}
        if isinstance(kind, tuple):                     # the click target itself: same answer the receipts layer gives
            return {**base, 'verdict': 'refuse', 'reason': kind[1], 'lock': False, 'resolved': extra.get('resolved') or {}}
        if facts is None:
            return {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': False}
        base.update(store=facts['store'], page=facts['page'], page_total_cents=facts['total'], commit_page=facts['commit_page'])
        hit = (extra.get('hit') or {}) if isinstance(extra, dict) else {}
        if hit:
            c = hit.get('clickable') or hit.get('self') or {}
            base['button'] = (c.get('labels') or [''])[0][:60]
        if facts['store'] in RESERVATION_STORES and kind is not None:
            rejected = {**base, 'verdict':'refuse','reason':'reservation_approval','log':True,'lock':True,
                        'message':'Reservation held. Open the exact review page and verify the current owner approval.'}
            if kind != 'click' or action != 'click' or int(ctx.get('clicks',1)) != 1 or ctx.get('button','left') != 'left' or self.mode()!='enforce':
                return rejected
            try:
                tab = ar.raw['current_tab']()['targetId']
                resolved = target() if target else None
                if not resolved or resolved.get('refused'):
                    return rejected
                p = self.probe(ar,[resolved['x'],resolved['y']])
                path = self.life.parent.parent/'state'/'reservations'
                key = reservation_adapter().gate(path,tab,p,False)
                return {**base,'verdict':'reservation','reservation_key':key,'reason':'reservation_matched','log':True}
            except Exception:
                return rejected
        if kind is None:
            return {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': bool(facts['commit_page'])}
        base['kind'] = kind
        if kind == 'hidden':
            return self.refuse(base, 'checkout_script_not_read_only', token=extra.get('token', ''))
        if kind == 'rewrite':
            return self.refuse(base, 'booking_script_changes_page', reason_code='checkout_script_not_read_only',
                               token=extra.get('token', ''))
        sid = facts['store']
        if sid is None:
            return self.refuse(base, 'store_not_on_list', host=facts['host'])
        now = time.time()
        data = self.grants.current(now)
        rows = data['grants']
        unsettled = latest(rows, sid, UNSETTLED)
        if unsettled:
            return self.refuse(base, 'already_ordered', grant=unsettled)
        mine = latest([g for g in rows if float(g.get('expires_at') or 0) > now], sid, ('approved',))
        if mine is None:
            other = latest([g for g in rows if float(g.get('expires_at') or 0) > now], None, ('approved',))
            if other is not None:
                return self.refuse(base, 'store_mismatch', grant=other)
            recent = latest([g for g in rows if now - float(g.get('pressed_at') or 0) < RECENT_PLACED], sid, ('placed', 'placed_unconfirmed'))
            return self.refuse(base, 'raw_needs_his_yes' if kind == 'raw' else 'purchase_needs_his_yes', recent=recent,
                               reason_code='purchase_needs_his_yes', booking_page=self.trip_for_text(sid, facts))
        base['grant'] = mine['id']
        base['grant_total_cents'] = int(mine['total_cents'])
        if kind == 'raw':
            return self.refuse(base, 'place_order_by_click_only', grant=mine)
        if action == 'click' and (int(ctx.get('clicks', 1) or 1) != 1 or str(ctx.get('button', 'left')) != 'left'):
            return self.refuse(base, 'one_click_only', grant=mine)
        total = facts['total']
        if total is None:
            return self.refuse(base, 'total_unreadable', grant=mine)
        if total > int(mine['total_cents']):
            return self.refuse(base, 'total_above_his_yes', grant=mine, booking_page=self.trip_for_text(sid, facts))
        if kind == 'frame':
            return {**base, 'verdict': 'allow', 'reason': 'frame_click_with_his_yes', 'log': True}
        if self.st.is_booking(sid):
            # A booking yes binds the trip. Errors here propagate: judge_when_broken refuses the press.
            cfg, B = self.st.booking(sid), booking_module()
            page = B.read_page(cfg, facts.get('text') or '', facts.get('fields') or [], B.today())
            base['booking_page'] = B.summary(page)
            want = mine.get('booking')
            if not want:
                return self.refuse(base, 'booking_needs_details', grant=mine, booking_page=page)
            base['booking_want'] = {k: v for k, v in want.items() if not str(k).startswith('_')}
            res = B.compare(cfg, want, page)
            if res['unreadable']:
                return self.refuse(base, 'booking_details_unreadable', grant=mine, booking_page=page)
            if res['diffs']:
                base['differs'] = res['fields']
                base['diffs'] = res['diffs']
                return self.refuse(base, 'booking_details_differ', grant=mine, booking_page=page, diffs=res['diffs'])
            base['booking_matches'] = True
        return {**base, 'verdict': 'pass', 'reason': 'owners_yes_matches', 'grant_row': mine, 'log': True}

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

    def refuse(self, base: dict, which: str, reason_code: str | None = None, **kw) -> dict:
        sid = base.get('store')
        text = message(self.st, which, store=sid, total=base.get('page_total_cents'), **kw)
        return {**base, 'verdict': 'refuse', 'reason': reason_code or which, 'message': text, 'lock': True, 'log': True}

    def judge_when_broken(self, ar, action: str, ctx: dict, target, error: BaseException) -> dict:
        """The fence itself failed (bad store table, unreadable grants, a bug). Purchase steps are refused; anything
        that is plainly not one goes ahead. Plain patterns only, nothing loaded from files."""
        why = f'{type(error).__name__}: {str(error)[:120]}'
        base = {'action': action, 'error': why}
        refuse = {**base, 'verdict': 'refuse', 'reason': 'lock_error', 'lock': True, 'log': True,
                  'message': message(self.st if self._stores else _NoStores(), 'lock_error', why=type(error).__name__)}
        if action not in ('click', 'press_key', 'dispatch_key', 'type_text', 'raw_script', 'raw_cdp'):
            return {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': True}
        try:
            xy = ctx.get('xy')
            if action == 'click' and target is not None:
                r = target()
                if r.get('refused'):
                    return {**base, 'verdict': 'refuse', 'reason': r['refused'], 'lock': False, 'resolved': r}
                xy = [r.get('x'), r.get('y')]
            p = self.probe(ar, xy if action == 'click' else None)
            href = str(p.get('href') or '')
            buttons = p.get('buttons') or []
            if (urlsplit(href).hostname or '').lower() in ('opentable.com','www.opentable.com') or ((urlsplit(href).hostname or '').lower() in ('trip.com','www.trip.com') and '/hotel' in urlsplit(href).path.lower()):
                return refuse
            strongish = re.compile(r'place\s*(?:your\s*)?order|buy\s*(?:it\s*)?now|pay\s*now|complete\s*(?:your\s*)?(?:purchase|booking)|submit\s*order|'
                                   r'confirm\s*(?:and|&)\s*(?:pay|book)|book\s*(?:it\s*|this\s*)?(?:now|trip|flights?)|^\s*(?:purchase|pay|buy|book)\s*$', re.I)
            # Each label is tested on its own: a one-word "Purchase" button whose labels come as ["Purchase", "Purchase"]
            # would never match ^purchase$ if they were joined first.
            checkout = bool(re.search(r'/checkout|/purchase|/payment|/gp/buy/|/buy/', urlsplit(href).path or '', re.I)) or \
                any(strongish.search(str(x)) for b in buttons for x in (b.get('labels') or []))
            if action == 'click':
                hit = p.get('hit') or {}
                labels = list((hit.get('clickable') or {}).get('labels') or []) + list((hit.get('self') or {}).get('labels') or [])
                return refuse if (any(strongish.search(str(x)) for x in labels) or (hit.get('frame') and checkout)) else {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': True}
            if not checkout:
                return {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': True}
            if action in ('press_key', 'dispatch_key'):
                return refuse if key_is_press(ctx.get('key', 'Enter'), p.get('focus')) else {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': True}
            if action == 'raw_script':
                expr = str(ctx.get('expression') or 'click')
                return refuse if (RAW_PRESS.search(expr) or RAW_HIDDEN.search(expr)) else {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': True}
            if action == 'raw_cdp':
                return refuse if risky_cdp(ctx.get('method') or 'Input.dispatchMouseEvent', ctx.get('params') or {}) else {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': True}
            return refuse
        except Exception:
            # Cannot even read the page: a raw press could still land (raw CDP does not need the page). Refuse the
            # kinds of step that can press something; the receipts layer would fail a click on an unreadable page anyway.
            if action == 'raw_script' and not (RAW_PRESS.search(str(ctx.get('expression') or 'click')) or RAW_HIDDEN.search(str(ctx.get('expression') or ''))):
                return {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': True}
            if action == 'raw_cdp' and not risky_cdp(ctx.get('method') or 'Input.dispatchMouseEvent', ctx.get('params') or {}):
                return {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': True}
            if action in ('press_key', 'dispatch_key') and not key_is_press(ctx.get('key', 'Enter'), None):
                return {**base, 'verdict': 'allow', 'reason': 'not_a_purchase_step', 'log': True}
            return refuse

    # acting
    def refused_record(self, ar, action: str, d: dict):
        before = ar.observe()
        record = ar.begin(action, before)
        record.update({k: v for k, v in (d.get('resolved') or {}).items() if k in ('target', 'blocker')})
        if d.get('lock'):
            record['purchase_lock'] = {k: d.get(k) for k in ('reason', 'store', 'page', 'page_total_cents', 'grant', 'grant_total_cents', 'kind')}
            if d.get('differs'):
                record['purchase_lock']['differs'] = d.get('differs')
            record['message'] = d.get('message')
            self.say(d.get('message') or '')
        return ar.complete(record, before, before, 'refused', d['reason'])

    def run_action(self, ar, orig, action, effect, check=None, target=None, raw=False):
        m = self.mode()
        call = lambda: orig(ar, action, effect, check=check, target=target, raw=raw)
        ctx = getattr(ar, '_plk_ctx', None) or {}
        with self.permit():
            if m == 'off':
                return call()
            try:
                d = self.judge(ar, action, ctx, target)
            except Exception as error:
                d = self.judge_when_broken(ar, action, ctx, target, error)
            row = {k: v for k, v in d.items() if k not in ('grant_row', 'log', 'lock', 'resolved')}
            row['mode'] = m
            if d['verdict'] == 'allow':
                if d.get('log'):
                    self.log(row)
                return call()
            if d['verdict'] == 'refuse':
                if not d.get('lock'):                       # target missing/covered: the receipts layer's own refusal
                    return self.refused_record(ar, action, d)
                if m == 'shadow':
                    self.log({**row, 'verdict': 'would_refuse'})
                    return call()
                self.log(row)
                return self.refused_record(ar, action, d)
            if d['verdict']=='reservation':
                return reservation_adapter().locked_click(self,ar,call,action,d)
            return self.place_once(ar, call, action, d, row)

    def place_once(self, ar, call, action, d, row):
        g = d['grant_row']
        claimed = self.grants.claim(g['id'], page_total_cents=d.get('page_total_cents'), page=d.get('page'))
        if claimed is None:                              # another step took this yes a moment ago
            latest_row = latest(self.grants.load()['grants'], d.get('store'))
            dd = self.refuse({k: d[k] for k in ('action', 'store', 'page', 'page_total_cents', 'commit_page', 'kind') if k in d},
                             'already_ordered', grant=latest_row or g)
            self.log({**{k: v for k, v in dd.items() if k not in ('log', 'lock')}, 'mode': row.get('mode')})
            return self.refused_record(ar, action, dd)
        fired = [0]
        real = ar.dispatch_click

        def once(*a, **k):
            if fired[0]:
                raise RuntimeError('purchase lock: Place order is pressed once only')
            fired[0] += 1
            return real(*a, **k)

        ar.dispatch_click = once
        result = None
        try:
            result = call()
            return result
        finally:
            try:
                del ar.dispatch_click
            except AttributeError:
                pass
            if fired[0]:
                self.grants.finish(g['id'], 'placed_unconfirmed', 'Place order was pressed once; the store has not been read yet')
                self.log({**row, 'verdict': 'pass', 'pressed': 1})
                text = pass_message(self.st, d['store'], int(d['page_total_cents']), g)
                self.say(text)
                if isinstance(result, dict):
                    result['purchase_lock'] = {'reason': 'owners_yes_matches', 'store': d['store'], 'grant': g['id'],
                                               'page_total_cents': d['page_total_cents'], 'pressed': 1}
                    result['message'] = text
                    try:
                        ar.save()                       # the receipt file on disk carries the note too
                    except Exception:
                        pass
            else:
                self.grants.finish(g['id'], 'approved', 'no input was sent (the click did not go ahead), the yes still holds')
                self.log({**row, 'verdict': 'released', 'pressed': 0})

    # the deeper belt: the harness IPC pipe in this child
    def guard_ipc(self):
        h = sys.modules.get('browser_harness.helpers')
        if h is None or getattr(getattr(h, '_send', None), '_purchase_lock', False):
            return False
        orig_send = h._send
        fence = self

        def _send(req, *args, **kwargs):
            if fence.permitted() or not isinstance(req, dict) or not req.get('method'):
                return orig_send(req, *args, **kwargs)
            risk = risky_cdp(req.get('method'), req.get('params') or {})
            if not risk:
                return orig_send(req, *args, **kwargs)
            m = fence.mode()
            if m == 'off':
                return orig_send(req, *args, **kwargs)
            with fence.permit():
                try:
                    hot, facts = fence.hot_for_ipc(orig_send, req.get('session_id'))
                except Exception as error:
                    hot, facts = True, {'error': type(error).__name__}
            if not hot:
                return orig_send(req, *args, **kwargs)
            row = {'action': 'raw_ipc', 'method': req.get('method'), 'verdict': 'refuse', 'reason': 'raw_browser_input',
                   'store': facts.get('store'), 'page': facts.get('page'), 'mode': m}
            if m == 'shadow':
                fence.log({**row, 'verdict': 'would_refuse'})
                return orig_send(req, *args, **kwargs)
            fence.log(row)
            text = message(fence.st, 'raw_browser_input')
            fence.say(text)
            raise RuntimeError(text)

        _send._purchase_lock = True
        _send.__wrapped__ = orig_send
        h._send = _send
        return True

    def hot_for_ipc(self, send, session_id) -> tuple:
        dialog = send({'meta': 'pending_dialog'}).get('dialog')
        if dialog:
            return True, {'dialog': True}
        r = send({'method': 'Runtime.evaluate', 'params': {'expression': probe_script(None), 'returnByValue': True},
                  'session_id': session_id})
        value = ((r.get('result') or {}).get('result') or {}).get('value')
        facts = page_facts(self.st, json.loads(value) if isinstance(value, str) else {})
        return facts['hot'], facts


class _NoStores:
    """Stand-in for message() when the store table itself could not be loaded."""
    stores = {}

    def name(self, sid):
        return 'this store'

    def orders_url(self, sid):
        return ''


# ---- install into ActionReceipts --------------------------------------------------------------------------------------

def install(ActionReceipts, settings: dict | None = None) -> Fence:
    """Wrap ActionReceipts so every input action is judged first. All wrappers are built before any is assigned, so a
    failure here leaves the class untouched (the caller then installs the stand-alone fail-safe)."""
    existing = getattr(ActionReceipts, '_purchase_lock', None)
    if existing is not None:
        return existing
    fence = Fence(settings)
    fence.st                                            # load the store table now: a broken table fails here, loudly
    orig = {n: getattr(ActionReceipts, n) for n in ('run_action', 'click_element', 'click_at_xy', 'js', 'cdp', 'install')}

    def run_action(self, action, effect, *, check=None, target=None, raw=False):
        return fence.run_action(self, orig['run_action'], action, effect, check=check, target=target, raw=raw)

    def click_element(self, selector):
        with fence.ctx(self, {'kind': 'click', 'selector': selector}):
            return orig['click_element'](self, selector)

    def click_at_xy(self, x, y, button='left', clicks=1):
        with fence.ctx(self, {'kind': 'click', 'xy': [x, y], 'button': button, 'clicks': clicks}):
            return orig['click_at_xy'](self, x, y, button, clicks)

    def js(self, expression, *args, **kwargs):
        tid = kwargs.get('target_id', args[0] if args else None)
        with fence.ctx(self, {'kind': 'raw_script', 'expression': expression, 'target_id': tid}):
            return orig['js'](self, expression, *args, **kwargs)

    def cdp(self, method, *args, **kwargs):
        sid = kwargs.get('session_id', args[0] if args else None)
        params = {k: v for k, v in kwargs.items() if k not in ('session_id', '_response_timeout')}
        with fence.ctx(self, {'kind': 'raw_cdp', 'method': method, 'params': params, 'session_id': sid}):
            return orig['cdp'](self, method, *args, **kwargs)

    def make_ctx(name, args, kwargs):
        if name == 'press_key':
            return {'kind': name, 'key': kwargs.get('key', args[0] if args else None)}
        if name == 'dispatch_key':
            return {'kind': name, 'key': kwargs.get('key', args[1] if len(args) > 1 else 'Enter')}
        return {'kind': name, 'text': kwargs.get('text', args[0] if args else None)}

    def install_(self, namespace):
        orig['install'](self, namespace)
        for name in ('press_key', 'dispatch_key', 'type_text'):
            if name in namespace:
                inner = namespace[name]

                def wrapped(*a, _inner=inner, _name=name, **k):
                    with fence.ctx(self, make_ctx(_name, a, k)):
                        return _inner(*a, **k)
                namespace[name] = wrapped

    ActionReceipts.run_action = run_action
    ActionReceipts.click_element = click_element
    ActionReceipts.click_at_xy = click_at_xy
    ActionReceipts.js = js
    ActionReceipts.cdp = cdp
    ActionReceipts.install = install_
    ActionReceipts._purchase_lock = fence
    try:
        fence.guard_ipc()
    except Exception as error:                          # the deeper belt is extra; the main fence is in place
        write_log(fence.life, {'action': 'install', 'verdict': 'note', 'reason': 'ipc_guard_not_installed', 'error': repr(error)[:200]})
    try:                                                # heartbeat: proof the lock armed itself in a real browser step
        beat = Path(fence.life) / 'buy-lock-heartbeat.json'
        tmp = beat.with_name('.buy-lock-heartbeat.json.tmp')
        tmp.write_text(json.dumps({'armed_at': time.time(), 'pid': os.getpid(), 'version': VERSION}))
        os.replace(tmp, beat)
    except Exception:
        pass
    return fence

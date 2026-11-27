"""relogin: a store sign-in lapsed in Zeus's own Chrome, so Zeus signs itself back in, once, where no code is asked.

The rule: when a store sign-in lapses in Zeus's own Chrome, Zeus may sign itself back in, ONLY where no phone or email
code is asked; it never enters a texted or emailed code without the owner; money, carts, orders and anything beyond
signing in still need the owner's yes. The logins live in the Mac login Keychain, service "zeus-login:<store>"
(saved with the zeus-login tool), and one is read only at the moment of signing in.

When: signedin.check has just read a store's account page and it was a sign-in page (a lapse), and the switch file
`relogin-on` is in the life folder. Without the switch this file is never imported and nothing changes.

What it does, at most once per store per day (the owner's date), and only outside quiet hours on the owner's clock (a
store may text the owner a code the moment their email is typed, and nothing reaches them at night):
  1. The attempt is written down before anything happens, so a run that breaks halfway is never tried again that day.
  2. A NEW background tab of Zeus's Chrome opens the store's account page (signed out, it lands on the sign-in page).
  3. Each step, code asks the page yes/no facts only (is there an email box, a password box, a code box; which of a
     few fixed warning phrases it shows; its host). It never reads what is in any box.
  4. It clicks the email box (a real mouse event), checks the box that now has focus is an email box on one of the
     store's own hosts over https, types the email one key at a time (real key events) and presses Enter. The same
     for the password, which is read from the Keychain only now, at that step. If after Enter on the password the
     same form is still there with no message from the store for a few seconds, the form's own button ("sign in",
     "log in", "continue", or its submit button; only inside that form) is clicked once (some stores keep the form up after Enter).
  5. Its tab is closed, and a fresh read of the account page (signedin.check, the existing classify) decides. Signed
     in: the lapse closes and no ask is ever sent. Anything else: the lapse goes on exactly as before, and the one
     ask texts the owner to sign in.

Where it stops at once (types nothing more, closes its tab, never tries again that day):
  a code box, or code words with no email or password box ("we sent a code")    code_step
  a robot check (puzzle, captcha, "press and hold", "verify you are human")       robot_check
  a locked or "unusual activity" notice                                          locked_notice
  an account picker ("choose an account")                                       account_picker
  a host that is not the store's own (the table's hosts, plus EXTRA_HOSTS)   wrong_host
  the password box back again after the password was sent                        password_not_accepted
  the page changed between the check and Enter                                   page_changed
  anything it does not recognise, a page that does not load, too many steps     unrecognised_page and others
There are no retries, ever. It never opens a cart, an order or a setting: it only types into the two sign-in boxes,
presses Enter, and at most once clicks the sign-in form's own button. It cannot do more: the line to Chrome refuses
every command but the seven below, every page script but the five below, and any key event carrying more than one
character. On any stop after typing, one picture of the page is saved (relogin-shots/<store>-<time>.png in the life
folder, the newest 10 kept; the browser shows the password box as dots) and named in the record, with the store's
own message near the form if it showed one ("store_said"; the owner's email is replaced by the words "your email").

The password: read with `security find-generic-password -s zeus-login:<store> -w` straight into a bytearray (never
on a command line, never a whole Python string), typed one character per key event into the checked password box
only, and the bytearray is zeroed the moment typing ends. It is never logged, printed, written to a file or put in a
report. Python cannot promise memory is wiped; single characters are the only copies that ever exist apart.

Every attempt is two lines in relogin.jsonl in the life folder ("started", "finished"): the store, the time, the
outcome, which boxes were typed into (by name), where it ended (host only) and why it stopped. Never a password, a
username or a full address.

Real Chrome and the real Keychain are used only from the live life folder (or with LIFE_WATCH_REAL_READS=1, the same
rehearsal door watches.py has). Anywhere else, without a stand-in, nothing happens at all.

By hand (a one-store trial; works without the switch file):  python3 -I -B relogin.py try <store>
"""
from __future__ import annotations

import base64
import codecs
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import watches as W  # noqa: E402
import settings  # noqa: E402

SWITCH = 'relogin-on'
LOG = 'relogin.jsonl'
LOCK = 'relogin.lock'
_RELOGIN = settings.get('stores.relogin', ['walmart', 'amazon', 'samsclub', 'southwest', 'ralphs'])
STORES = tuple(s.strip().lower() for s in (_RELOGIN.split(',') if isinstance(_RELOGIN, str) else _RELOGIN) if str(s).strip())   # the Keychain items zeus-login saves
SERVICE = 'zeus-login:'
EXTRA_HOSTS = {'ralphs': ('login.kroger.com',)}                      # a store whose sign-in page lives on its parent company's host
SECURITY = '/usr/bin/security'
DAY_START, DAY_END = settings.QUIET_END, settings.QUIET_START     # the owner's clock: a store may text a code once the email is typed
MAX_STEPS = 4                  # pages with a box to type into, in one attempt
LOAD_WAIT = 20.0               # seconds for a page to show its boxes
QUIET = 4.0                    # a loaded page with no box, unchanged this long, is taken as it is
AFTER_SUBMIT = 1.5             # Enter, then at least this long before the page is looked at again
POLL = 0.5
TOTAL = 150.0                  # the whole attempt, tab open to tab closed
SUBMIT_WAIT = 5.0              # after Enter on the password: the form still there, no message, this long -> its own button, once
SHOT_WAIT = 10.0               # a screenshot that takes longer is given up on
SHOTS = 'relogin-shots'        # in the life folder: one picture of the page for each stop after typing, the newest 10 kept
MAX_SHOTS = 10
SAID_MAX = 300                 # characters of a store's own message kept in the record
KEYCHAIN_WAIT = 20.0           # a Keychain read that hangs (a locked Keychain asking on screen) is given up on
CHROME = None                  # tests: a function () -> a stand-in line to Chrome with call(method, params, session) and close()
SECRETS = None                 # tests: a function (store, password: bool) -> bytearray | None

CODE_WORDS = ('verification code', 'one-time code', 'one time code', 'one-time passcode', 'enter the code', 'enter code', 'we sent a code',
              'sent a code', 'sent you a code', 'security code', 'passcode', 'text you a code', 'email you a code', 'we texted',
              'we emailed', 'approve the notification', 'check your phone', 'two-step verification', '2-step verification',
              "verify it's you", 'verify it’s you')
ROBOT_WORDS = ('press & hold', 'press and hold', 'captcha', 'are you a robot', 'verify you are human', "confirm you're human",
               'not a robot', 'solve this puzzle', 'enter the characters you see', 'type the characters')
LOCKED_WORDS = ('unusual activity', 'suspicious activity', 'account is locked', 'account has been locked', 'account locked',
                'temporarily locked', 'too many attempts', 'too many failed', 'account has been disabled', 'account is disabled',
                'account is on hold', 'password reset required', 'must reset your password', 'need to reset your password')
PICKER_WORDS = ('choose an account', 'select an account', 'choose account', 'select account', 'which account', 'switch accounts',
                'pick an account')

# What a box is, said once and used by every page script. Nothing here reads what is in a box.
_JS = ("const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);"
       " return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !el.disabled; };"
       " const meta = el => [el.name, el.id, el.autocomplete, el.getAttribute('aria-label'), el.placeholder].join(' ').toLowerCase();"
       " const isPass = el => el.type === 'password';"
       " const isCode = el => !isPass(el) && (el.autocomplete === 'one-time-code' || /(^|[^a-z])(otp|code|passcode|pin)([^a-z]|$)/.test(meta(el)));"
       " const isUser = el => !isPass(el) && !isCode(el) && ['email', 'text', 'tel'].includes(el.type)"
       " && (el.type === 'email' || /user|email|login|signin|phone|mobile|account/.test(meta(el)));"
       " const boxes = [...document.querySelectorAll('input')].filter(vis);"
       " const first = test => boxes.find(test) || null;")
# The four page scripts, the only ones the line to Chrome will run.
FACTS = ("(() => { " + _JS +
         " const words = (document.body ? document.body.innerText : '').toLowerCase().slice(0, 50000);"
         " const hit = list => list.filter(w => words.includes(w));"
         " const robotFrame = [...document.querySelectorAll('iframe')].filter(vis).map(f => String(f.src || '').toLowerCase())"
         ".some(s => /captcha|arkoselabs|funcaptcha|perimeterx/.test(s) && !/size=invisible/.test(s));"
         " const near = first(isPass) || first(isUser); const scope = (near && near.closest('form')) || document.body;"
         " const notes = scope ? [...scope.querySelectorAll('[role=alert], [aria-live=assertive], [aria-live=polite], [class*=error i], [id*=error i], [class*=alert i]')]"
         ".filter(e => !['INPUT', 'SELECT', 'TEXTAREA'].includes(e.tagName) && vis(e)).map(e => (e.innerText || '').trim().replace(/\\s+/g, ' ')).filter(x => x) : [];"
         " const said = [...new Set(notes)].filter((x, i, all) => !all.some((y, j) => j !== i && y.length > x.length && y.includes(x))).join(' | ').slice(0, %d);"
         " return JSON.stringify({ready: document.readyState, host: location.hostname, secure: location.protocol === 'https:', said: said,"
         " user_box: !!first(isUser), pass_box: !!first(isPass), code_box: !!first(isCode), robot_frame: robotFrame,"
         " code: hit(%s), robot: hit(%s), locked: hit(%s), picker: hit(%s)}); })()"
         % ((SAID_MAX,) + tuple(json.dumps(list(x)) for x in (CODE_WORDS, ROBOT_WORDS, LOCKED_WORDS, PICKER_WORDS))))
_BOX = ("(() => { " + _JS + " const el = first(%s); if (!el) return 'null'; el.scrollIntoView({block: 'center'}); el.focus();"
        " const r = el.getBoundingClientRect(); return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2}); })()")
BOX_USER, BOX_PASS = _BOX % 'isUser', _BOX % 'isPass'
FOCUSED = ("(() => { " + _JS + " const el = document.activeElement;"
           " const kind = !el || el.tagName !== 'INPUT' || !vis(el) ? 'other' : isPass(el) ? 'password' : isUser(el) ? 'user' : 'other';"
           " if (kind !== 'other' && el.select) el.select();"
           " return JSON.stringify({host: location.hostname, secure: location.protocol === 'https:', kind: kind}); })()")
# The sign-in form's own button: inside the form that holds the password box, its text "sign in", "log in" or "continue",
# else the form's submit button. Only if the point clicked really is that button (nothing laid over it). Never outside the form.
BOX_SUBMIT = ("(() => { " + _JS + " const pw = first(isPass); const form = pw && pw.closest('form'); if (!form) return 'null';"
              " const cands = [...form.querySelectorAll('button, input[type=submit]')].filter(vis);"
              " const words = b => (b.tagName === 'BUTTON' ? (b.innerText || '') : '').trim().toLowerCase();"
              " const btn = cands.find(b => ['sign in', 'log in', 'login', 'continue'].includes(words(b))) || cands.find(b => b.getAttribute('type') === 'submit');"
              " if (!btn) return 'null'; btn.scrollIntoView({block: 'center'}); const r = btn.getBoundingClientRect();"
              " const x = r.left + r.width / 2, y = r.top + r.height / 2; const hit = document.elementFromPoint(x, y);"
              " if (!hit || !(hit === btn || btn.contains(hit)) || !form.contains(btn)) return 'null';"
              " return JSON.stringify({x: x, y: y}); })()")
SCRIPTS = frozenset((FACTS, BOX_USER, BOX_PASS, FOCUSED, BOX_SUBMIT))
KEY_FIELDS = frozenset(('type', 'text', 'unmodifiedText', 'key', 'code', 'windowsVirtualKeyCode', 'nativeVirtualKeyCode'))
MOUSE_FIELDS = frozenset(('type', 'x', 'y', 'button', 'clickCount'))

# The plain words for each way an attempt ends (the by-hand command and the lead's reading of relogin.jsonl).
SAID = {'signed_in': 'signed back in by itself', 'already_signed_in': 'was signed in already; nothing was typed',
        'code_step': 'the store asked for a code sent to your phone or email, so it stopped',
        'robot_check': 'the store showed a robot check, so it stopped', 'locked_notice': 'the store showed a locked or unusual-activity notice, so it stopped',
        'account_picker': 'the store asked which account to use, so it stopped', 'wrong_host': 'the page was not on the store\'s own sign-in site, so nothing was typed there',
        'password_not_accepted': 'the store did not take the password, so it stopped (no second try)',
        'page_changed': 'the page changed under it before Enter, so it stopped', 'unrecognised_page': 'it did not recognise the page, so it stopped',
        'page_did_not_load': 'the sign-in page did not load, so it stopped', 'too_many_steps': 'the sign-in took more steps than expected, so it stopped',
        'still_signed_out': 'it typed the login but the account page still shows signed out', 'could_not_tell': 'it typed the login but could not tell afterwards whether it worked',
        'no_login_saved': 'no login for this store could be read from the Keychain', 'chrome_unreachable': "Zeus's own Chrome could not be reached",
        'tried_today': 'it already tried this store today', 'outside_hours': 'it only tries between 9am and 9pm',
        'not_live': 'this is not the live life folder, so nothing was opened', 'busy': 'another sign-in was running', 'log_unreadable': 'its own record could not be read, so it did not try',
        'not_a_store': 'that is not one of the relogin stores'}

_BUSY = False


def on(life) -> bool:
    return (Path(life) / SWITCH).exists()


def _real(life) -> bool:
    """Real Chrome and the real Keychain only from the live life folder, or on purpose for a rehearsal."""
    try:
        live = Path(life).resolve() == W.LIVE_HOME.resolve()
    except OSError:
        live = False
    return live or os.environ.get('LIFE_WATCH_REAL_READS') == '1'


def allowed_hosts(site: str, entry: dict) -> tuple:
    """The only hosts a sign-in box may be typed into: the store's own hosts from the table, plus its sign-in host."""
    extra = entry.get('signin_hosts') or []
    extra = [extra] if isinstance(extra, str) else list(extra)
    return tuple(sorted({str(h).lower().strip('.') for h in list(entry.get('hosts') or []) + extra + list(EXTRA_HOSTS.get(site, ())) if str(h).strip('. ')}))


def host_ok(host, hosts) -> bool:
    host = str(host or '').lower().rstrip('.')
    return bool(host) and any(host == h or host.endswith('.' + h) for h in hosts)


def permitted(method: str, params) -> bool:
    """Everything the line to Chrome may carry. Anything else is refused before it is sent."""
    p = params if isinstance(params, dict) else {}
    if method == 'Target.createTarget':
        return set(p) <= {'url', 'background'} and str(p.get('url') or '').startswith('https://')
    if method in ('Target.attachToTarget', 'Target.closeTarget'):
        return set(p) <= {'targetId', 'flatten'}
    if method == 'Runtime.evaluate':
        return set(p) <= {'expression', 'returnByValue'} and p.get('expression') in SCRIPTS
    if method == 'Page.captureScreenshot':
        return set(p) <= {'format', 'fromSurface'} and p.get('format') == 'png'
    if method == 'Input.dispatchMouseEvent':
        return set(p) <= MOUSE_FIELDS and p.get('type') in ('mouseMoved', 'mousePressed', 'mouseReleased')
    if method == 'Input.dispatchKeyEvent':
        return set(p) <= KEY_FIELDS and p.get('type') in ('keyDown', 'keyUp') and len(str(p.get('text') or '')) <= 1
    return False


# ---- the Keychain ------------------------------------------------------------------------------------------------------

def _zero(buf):
    if isinstance(buf, bytearray):
        for i in range(len(buf)):
            buf[i] = 0


def _keychain(store: str, password: bool):
    """One read of the store's Keychain item -> bytearray, or None. password=False: the item's attributes (for the
    account name); True: the password (`-w`), read straight into a bytearray. The service name is the only thing on
    the command line."""
    if SECRETS is not None:
        return SECRETS(store, password)
    try:
        p = subprocess.Popen([SECURITY, 'find-generic-password', '-s', SERVICE + store] + (['-w'] if password else []),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None

    def give_up():                                  # the whole process group, so nothing it started keeps the pipe open
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    killer = threading.Timer(KEYCHAIN_WAIT, give_up)
    killer.start()
    buf, n = bytearray(16384), 0
    try:
        with memoryview(buf) as mv:
            while n < len(buf):
                got = p.stdout.readinto(mv[n:])
                if not got:
                    break
                n += got
        code = p.wait()
    finally:
        killer.cancel()
        p.stdout.close()
    if code != 0 or n == 0 or n >= len(buf):
        _zero(buf)
        return None
    out = buf[:n]
    _zero(buf)
    while out and out[-1] in (10, 13):
        out.pop()
    return out or None


def _account(store: str) -> str:
    """The email or username saved for this store ('' when none can be read). Not a secret, but never logged either."""
    raw = _keychain(store, False)
    if not raw:
        return ''
    try:
        m = re.search(r'"acct"<blob>="([^"\n]+)"', raw.decode('utf-8', 'replace'))
    finally:
        _zero(raw)
    return m.group(1) if m else ''


def _chars(secret: bytearray):
    """The password one character at a time, decoded byte by byte: no whole copy of it is ever made."""
    dec = codecs.getincrementaldecoder('utf-8')()
    for b in secret:
        ch = dec.decode(bytes((b,)))
        if ch:
            yield ch


# ---- the line to Chrome --------------------------------------------------------------------------------------------------

class _Line:
    """Chrome's control port on this Mac, through pageread's own small websocket, limited by permitted()."""

    def __init__(self, cdp: str, deadline: float):
        import pageread as PR
        host, port = PR._local(cdp.rstrip('/'))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(f'http://{host}:{port}/json/version', method='GET'), timeout=5) as r:
            ws_url = str(json.loads(r.read(200_000)).get('webSocketDebuggerUrl') or '')
        if PR._local(ws_url)[1] != port:
            raise ValueError('Chrome named a control line that is not its own')
        self.deadline, self.n = deadline, 0
        self.ws = PR._Socket(ws_url, max(1.0, deadline - time.monotonic()))

    def call(self, method: str, params: dict, session: str = '') -> dict:
        if not permitted(method, params):
            raise PermissionError('not part of signing in')
        self.n += 1
        mine = self.n
        self.ws.send(json.dumps({'id': mine, 'method': method, 'params': params, **({'sessionId': session} if session else {})}))
        while True:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('Chrome took too long')
            self.ws.settimeout(left)
            msg = json.loads(self.ws.recv())
            if msg.get('id') == mine:
                if 'error' in msg:
                    raise RuntimeError('Chrome refused a command')      # Chrome's own words are not kept: they could echo what was sent
                return msg.get('result') or {}

    def close(self):
        self.ws.close()


class _Tab:
    """The one tab an attempt opens. Every command goes through permitted() here too, whatever line is underneath."""

    def __init__(self, line, deadline: float):
        self.line, self.deadline, self.target, self.session = line, deadline, None, ''

    def call(self, method: str, params: dict, in_tab: bool = True) -> dict:
        if not permitted(method, params):
            raise PermissionError('not part of signing in')
        if time.monotonic() > self.deadline:
            raise TimeoutError('the sign-in took too long')
        return self.line.call(method, params, self.session if in_tab else '') or {}

    def open(self, url: str):
        self.target = self.call('Target.createTarget', {'url': url, 'background': True}, in_tab=False).get('targetId')
        if not self.target:
            raise RuntimeError('Chrome did not open a tab')
        self.session = self.call('Target.attachToTarget', {'targetId': self.target, 'flatten': True}, in_tab=False).get('sessionId') or ''
        if not self.session:
            raise RuntimeError('Chrome did not let the tab be used')

    def run(self, script: str):
        got = self.call('Runtime.evaluate', {'expression': script, 'returnByValue': True})
        try:
            out = json.loads((got.get('result') or {}).get('value') or 'null')
        except (TypeError, ValueError):
            return None
        return out if isinstance(out, dict) else None

    def settle(self, before, wait: float):
        """The page's facts once it has settled. After a submit (before = the facts then) it first waits for the page
        to change. A page with a box or a warning is settled when two looks agree; one with neither when it has not
        changed for QUIET seconds. At the end of `wait`, whatever the last look said."""
        end = min(self.deadline - 2, time.monotonic() + wait)
        if before is not None:
            time.sleep(AFTER_SUBMIT)
        last, since = None, time.monotonic()
        while True:
            f = self.run(FACTS)
            if f != last:
                last, since = f, time.monotonic()
            elif f and f.get('ready') == 'complete' and f != before:
                busy = f.get('user_box') or f.get('pass_box') or f.get('code_box') or stop_reason(f, None)
                if busy or time.monotonic() - since >= QUIET:
                    return f
            if time.monotonic() >= end:
                return f
            time.sleep(POLL)

    def fill(self, kind: str, chars, hosts, typed: list, label: str) -> str:
        """Click the box, check the box that has focus is that kind of box on one of the store's hosts over https,
        then type. -> '' or why it stopped (nothing typed then)."""
        spot = self.run(BOX_PASS if kind == 'password' else BOX_USER)
        if not spot or not all(isinstance(spot.get(k), (int, float)) for k in ('x', 'y')):
            return 'unrecognised_page'
        for step in ('mouseMoved', 'mousePressed', 'mouseReleased'):
            self.call('Input.dispatchMouseEvent', {'type': step, 'x': spot['x'], 'y': spot['y'], 'button': 'left' if step != 'mouseMoved' else 'none',
                                                   'clickCount': 1 if step != 'mouseMoved' else 0})
        why = self.focused(kind, hosts)
        if why:
            return why
        typed.append(label)
        for ch in chars:
            self.call('Input.dispatchKeyEvent', {'type': 'keyDown', 'text': ch, 'unmodifiedText': ch, 'key': ch})
            self.call('Input.dispatchKeyEvent', {'type': 'keyUp', 'key': ch})
        return ''

    def focused(self, kind: str, hosts) -> str:
        f = self.run(FOCUSED)
        if not f:
            return 'unrecognised_page'
        if not f.get('secure') or not host_ok(f.get('host'), hosts):
            return 'wrong_host'
        return '' if f.get('kind') == kind else 'unrecognised_page'

    def enter(self, kind: str, hosts) -> str:
        """Enter, only while the box just typed into still has focus on the same kind of host."""
        if self.focused(kind, hosts):
            return 'page_changed'
        for step in ('keyDown', 'keyUp'):
            self.call('Input.dispatchKeyEvent', {'type': step, 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13,
                                                 **({'text': '\r'} if step == 'keyDown' else {})})
        return ''

    def submit(self) -> bool:
        """Click the sign-in form's own button once (BOX_SUBMIT finds it, or says there is none). -> clicked?"""
        spot = self.run(BOX_SUBMIT)
        if not spot or not all(isinstance(spot.get(k), (int, float)) for k in ('x', 'y')):
            return False
        for step in ('mouseMoved', 'mousePressed', 'mouseReleased'):
            self.call('Input.dispatchMouseEvent', {'type': step, 'x': spot['x'], 'y': spot['y'], 'button': 'left' if step != 'mouseMoved' else 'none',
                                                   'clickCount': 1 if step != 'mouseMoved' else 0})
        return True

    def shot(self):
        """One picture of the page as it stands (the browser shows a password box as dots). -> PNG bytes or None."""
        if not self.session:
            return None
        try:
            self.deadline = time.monotonic() + SHOT_WAIT
            if hasattr(self.line, 'deadline'):
                self.line.deadline = self.deadline
            data = base64.b64decode(self.call('Page.captureScreenshot', {'format': 'png', 'fromSurface': True}).get('data') or '', validate=True)
            return data if data.startswith(b'\x89PNG') else None
        except Exception:
            return None

    def close(self):
        try:
            if self.target:
                self.deadline = max(self.deadline, time.monotonic() + 3)    # the tab this attempt opened is always closed again
                if hasattr(self.line, 'deadline'):
                    self.line.deadline = max(self.line.deadline, self.deadline)
                self.call('Target.closeTarget', {'targetId': self.target}, in_tab=False)
        except Exception:
            pass
        try:
            self.line.close()
        except Exception:
            pass


def _line(deadline: float):
    if CHROME is not None:
        return CHROME()
    import pageread as PR
    return _Line(PR.CDP, deadline)


def stop_reason(f, hosts) -> str:
    """What on this page means stop. hosts=None: the host is not judged (used while waiting for a page to settle)."""
    if not isinstance(f, dict) or f.get('ready') != 'complete':
        return 'page_did_not_load'
    if hosts is not None and (not f.get('secure') or not host_ok(f.get('host'), hosts)):
        return 'wrong_host'
    if f.get('robot') or f.get('robot_frame'):
        return 'robot_check'
    if f.get('locked'):
        return 'locked_notice'
    if f.get('picker'):
        return 'account_picker'
    if f.get('code_box') or (f.get('code') and not (f.get('user_box') or f.get('pass_box'))):
        return 'code_step'
    return ''


def _sign_in(site: str, entry: dict, rec: dict) -> tuple:
    """The steps in the tab. -> ('check', '') when the account page should now be read, or ('stopped', why)."""
    hosts = allowed_hosts(site, entry)
    url = str(entry.get('signin_url') or entry.get('account_url') or '')
    if not url.startswith('https://') or not host_ok(urllib.parse.urlsplit(url).hostname, hosts):
        return 'stopped', 'wrong_host'
    user = _account(site)
    if not user:
        return 'stopped', 'no_login_saved'
    deadline = time.monotonic() + TOTAL
    try:
        tab = _Tab(_line(deadline), deadline)
    except Exception:
        return 'stopped', 'chrome_unreachable'
    secret = None

    def note(f):
        """The store's own message near the form, as it said it (the owner's email, if it repeats it, is not kept)."""
        said = str((f or {}).get('said') or '').strip() if isinstance(f, dict) else ''
        if said:
            rec['store_said'] = re.sub(re.escape(user), 'your email', said, flags=re.I)[:SAID_MAX]
    try:
        tab.open(url)
        typed_user = typed_pass = clicked = False
        before = None
        for _ in range(MAX_STEPS):
            f = tab.settle(before, LOAD_WAIT)
            rec['host'] = (f or {}).get('host') or None
            note(f)
            why = stop_reason(f, hosts)
            if why:
                return 'stopped', why
            if f.get('pass_box'):
                if typed_pass:
                    return 'stopped', 'password_not_accepted'
                if f.get('user_box') and not typed_user:
                    why = tab.fill('user', user, hosts, rec['typed'], 'username')
                    if why:
                        return 'stopped', why
                    typed_user = True
                secret = _keychain(site, True)            # the password is read only now, at the step that types it
                if not secret:
                    return 'stopped', 'no_login_saved'
                try:
                    why = tab.fill('password', _chars(secret), hosts, rec['typed'], 'password')
                finally:
                    _zero(secret)
                if why:
                    return 'stopped', why
                typed_pass, kind = True, 'password'
            elif f.get('user_box'):
                if typed_user:
                    return 'stopped', 'unrecognised_page'         # the email page came back
                why = tab.fill('user', user, hosts, rec['typed'], 'username')
                if why:
                    return 'stopped', why
                typed_user, kind = True, 'user'
            else:
                if typed_user and not typed_pass:
                    return 'stopped', 'unrecognised_page'         # the email was taken and then no password box: not a plain sign-in
                return 'check', ''
            why = tab.enter(kind, hosts)
            if why:
                return 'stopped', why
            if kind == 'password' and not clicked:
                # Enter may not submit on some sites. Still the same form, the password box, no message from the store,
                # a few seconds on: click the form's own button, once per attempt, then judge as before.
                g = tab.settle(f, SUBMIT_WAIT)
                note(g)
                if (isinstance(g, dict) and g.get('ready') == 'complete' and g.get('pass_box') and not g.get('said')
                        and g.get('host') == f.get('host') and not stop_reason(g, hosts)):
                    clicked = True
                    rec['submit_clicked'] = tab.submit()
            before = f
        return 'stopped', 'too_many_steps'
    except PermissionError:
        return 'stopped', 'unrecognised_page'
    except Exception:
        return 'stopped', 'chrome_unreachable' if tab.target is None else 'unrecognised_page'
    finally:
        _zero(secret)
        if rec['typed']:
            rec['png'] = tab.shot()          # kept only if the attempt ends in a stop (attempt decides)
        tab.close()


# ---- the record: relogin.jsonl -------------------------------------------------------------------------------------------

def _day(now_dt: datetime) -> str:
    return now_dt.astimezone(W.PACIFIC).date().isoformat()


def _rows(life):
    """Every line of relogin.jsonl, or None when it is there but cannot be read (then no attempt is made)."""
    p = Path(life) / LOG
    if not p.exists():
        return []
    try:
        return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        return None


def _write(life, row: dict):
    with open(Path(life) / LOG, 'a') as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        fh.flush()
        os.fsync(fh.fileno())


def _owner_hour(life, now_dt: datetime) -> int:
    try:
        import ownertime
        z = ownertime.zone(now_dt.timestamp(), life=life)
    except Exception:
        z = W.PACIFIC
    return now_dt.astimezone(z).hour


def attempt(site: str, entry: dict, life, now_dt: datetime, use=None, by: str = 'engine') -> dict:
    """One attempt for a store whose sign-in page was just seen. -> {'outcome', 'stop', 'typed', 'host'}.
    'skipped' (nothing written, nothing opened): not one of the relogin stores, not the live folder, inside quiet
    hours on the owner's clock, already tried today, another attempt running, or its own record unreadable."""
    global _BUSY
    life = Path(life)
    skip = lambda why: {'outcome': 'skipped', 'stop': why, 'typed': [], 'host': None, 'store_said': None, 'shot': None}
    if site not in STORES or not isinstance(entry, dict):
        return skip('not_a_store')
    if (CHROME is None or SECRETS is None) and not _real(life):
        return skip('not_live')                     # outside the live folder, real Chrome and the real Keychain are never touched
    if not DAY_START <= _owner_hour(life, now_dt) < DAY_END:
        return skip('outside_hours')
    if _BUSY:
        return skip('busy')
    life.mkdir(parents=True, exist_ok=True)
    with open(life / LOCK, 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return skip('busy')
        rows = _rows(life)
        if rows is None:
            return skip('log_unreadable')
        day = _day(now_dt)
        if any(isinstance(r, dict) and r.get('store') == site and r.get('day') == day and r.get('event') == 'started' for r in rows):
            return skip('tried_today')
        _write(life, {'at': W._iso(now_dt), 'day': day, 'store': site, 'event': 'started', 'by': by})
        _BUSY = True
        t0 = time.monotonic()
        rec = {'typed': [], 'host': None, 'store_said': None, 'submit_clicked': None, 'png': None}
        try:
            try:
                outcome, stop = _sign_in(site, entry, rec)
            except Exception:
                outcome, stop = 'stopped', 'unrecognised_page'
            if outcome == 'check':
                import signedin as S
                status = S.check(site, life, now_dt, use, force=True)       # the existing classify, on a fresh read of the account page
                if status == 'signed_in':
                    outcome, stop = ('signed_in' if 'password' in rec['typed'] else 'already_signed_in'), None
                else:
                    outcome = 'stopped'
                    stop = 'could_not_tell' if status == 'unknown' and rec['typed'] else 'still_signed_out' if 'password' in rec['typed'] else 'unrecognised_page'
        finally:
            _BUSY = False
        shot = None
        if outcome == 'stopped' and rec['typed'] and rec['png']:
            try:
                shot = _save_shot(life, site, rec['png'])
            except OSError:
                shot = None
        rec['png'] = None
        row = {'at': W._iso(datetime.now(W.PACIFIC)), 'day': day, 'store': site, 'event': 'finished', 'outcome': outcome, 'stop': stop,
               'typed': rec['typed'], 'host': rec['host'], 'submit_clicked': rec['submit_clicked'], 'store_said': rec['store_said'], 'shot': shot,
               'seconds': round(time.monotonic() - t0, 1)}
        _write(life, row)
        return {k: row[k] for k in ('outcome', 'stop', 'typed', 'host', 'store_said', 'shot')}


def _save_shot(life: Path, site: str, png: bytes) -> str:
    """relogin-shots/<store>-<time>.png in the life folder; only the newest MAX_SHOTS are kept. -> its path in the folder."""
    folder = life / SHOTS
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(W.PACIFIC).strftime('%Y%m%d-%H%M%S')
    name, n = f'{site}-{stamp}.png', 1
    while (folder / name).exists():
        n += 1
        name = f'{site}-{stamp}-{n}.png'
    (folder / name).write_bytes(png)
    shots = sorted(folder.glob('*.png'), key=lambda q: (q.stat().st_mtime, q.name))
    for old in shots[:-MAX_SHOTS]:
        try:
            old.unlink()
        except OSError:
            pass
    return f'{SHOTS}/{name}'


def after_lapse(site: str, entry: dict, life, now_dt: datetime, use=None) -> bool:
    """signedin.check's hook (switch file relogin-on): a read just found this store's sign-in page. True only when a
    fresh read after signing in shows the account signed in (the lapse is then closed, so no ask goes to the owner).
    Never raises: on any trouble the lapse goes on as before."""
    try:
        return on(life) and attempt(site, entry, life, now_dt, use)['outcome'] in ('signed_in', 'already_signed_in')
    except Exception:
        return False


def main(argv) -> int:
    """By hand: python3 -I -B relogin.py try <store>. Reads the account page first; only a sign-in page leads to an
    attempt. Prints one plain line."""
    if len(argv) != 2 or argv[0] != 'try':
        print('usage: relogin.py try <' + '|'.join(STORES) + '>')
        return 2
    import signedin as S
    life, site = Path(S.LIFE), argv[1].strip().lower()
    entry = S.sites(life).get(site)
    if site not in STORES or not entry or not S.usable(entry):
        print(f'{site}: not one of the relogin stores, or not set up in the sign-in table. Nothing was done.')
        return 2
    name = entry.get('name') or site
    now_dt = datetime.now(W.PACIFIC)
    finished = lambda rows: [r for r in rows if isinstance(r, dict) and r.get('store') == site and r.get('event') == 'finished']
    before = len(_rows(life) or [])
    status = S.check(site, life, now_dt, force=True)          # with relogin-on present this may itself make the attempt
    new = finished((_rows(life) or [])[before:])
    if new:
        out = new[-1]
    elif status == 'signed_in':
        print(f'{name} is signed in already. Nothing was typed.')
        return 0
    elif status == 'signed_out':
        out = attempt(site, entry, life, now_dt, by='by hand')
    else:
        print(f'{name}: could not tell whether it is signed in ({(S._state(life, site) or {}).get("why") or "the page could not be read"}). Nothing was typed.')
        return 1
    said = lambda r: SAID.get(r.get('stop') or r.get('outcome'), r.get('stop') or r.get('outcome'))
    line = f'{name}: {said(out)}.'
    if out.get('outcome') != 'skipped':
        line += f" Typed into: {', '.join(out.get('typed') or []) or 'nothing'}."
        line += f' The store said: "{out["store_said"]}".' if out.get('store_said') else ''
        line += f' Picture of the page: {out["shot"]}.' if out.get('shot') else ''
    elif out.get('stop') == 'tried_today':
        earlier = [r for r in finished(_rows(life) or []) if r.get('day') == _day(now_dt)]
        line += f' Earlier today: {said(earlier[-1])}.' if earlier else ''
    print(line)
    return 0 if out.get('outcome') in ('signed_in', 'already_signed_in') else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

"""handoff_server: the phone side of Zeus's sign-in handoff.

Listens on 127.0.0.1:8851 only. Put an HTTPS reverse proxy or tunnel in front of it that maps
<signin.public_base> (for example https://your-host.example.test/zeus-signin) to this port's /zeus-signin path.
Standard library only: Hermes's python has neither websockets nor aiohttp, so the phone
gets frames by long-poll and sends taps and typing as small POSTs.

  GET  /zeus-signin/<token>          the page. Starts NOTHING (iMessage link previews fetch it).
  POST /zeus-signin/<token>/start    the Start button: shows the ONE handoff tab (a second Start ends the first view)
  GET  /zeus-signin/<token>/frame    the next picture of the tab (long-poll)
  POST /zeus-signin/<token>/input    tap / hold / drag / type / Enter / Delete / Tab / scroll / Back
  POST /zeus-signin/<token>/done     "Done, I'm signed in": the engine's signedin.check(site, force=True) now
  GET  /zeus-signin/ping             "ok" (so the installer can test the public path); says nothing else
  GET  /zeus-signin/know/<token>     the "what Zeus knows" page, with NOTHING personal until Show
  POST /zeus-signin/know/<token>/show   ... its content;  POST .../act  a two-tap button. All in knowspage.py, with its
                                     own session file and switch file knows-page-on (off: these are the plain 404)
Anything else, a wrong token or an expired one: the same plain 404 page.

Guards, all here in code (the words for each note code are in page.html):
  * taps and typing reach the tab only while it is on one of the site's hosts and not on a cart/order/payment/
    checkout page (handoff.Rules.input_ok); elsewhere it is view only
  * before every press, a read-only probe (in an isolated world, so the page cannot fake it) looks at what is under
    the finger: a purchase-lock commit button (Place order, Buy now, ...), a link off the site, a box from a site
    that is not the store or a known robot check -> refused, nothing is sent to the page
  * Enter is refused when it would press a commit button or send a form whose submit button is one
  * the tab may only GO to the site's own non-checkout pages: every main-frame page load is held (Fetch) and an
    address outside that is cancelled; a new window the page opens is closed at once; Back only to an allowed page
  * a page pop-up (alert/confirm) is dismissed, never accepted
Never logged: typed text, tokens, full addresses. Request lines are not logged at all (they carry the token).
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import handoff as H  # noqa: E402
import knowspage as K  # noqa: E402

PREFIX = '/zeus-signin'
PORT = 8851
FRAME_WAIT = 20.0              # a frame request waits this long for something new
IDLE_END = 90                  # no frame request for this long: the view ends (the session stays; Start again)
HOLD_MAX = 15                  # a press held this long with no release is let go
START_PER_SESSION = (8, 600)   # at most 8 Starts per link per 10 minutes
START_GLOBAL = (30, 600)       # at most 30 Starts per 10 minutes, all links together
DONE_WAIT = 60
TEXT_MAX = 256
BODY_MAX = 8192
SHOT_MIN_GAP = 0.15            # pictures: at most about 6 a second
SHOT_BURST = 0.25              # ... this often for 1.5 s after an input or a page change
SHOT_EVERY = 0.7               # ... at least this often while the phone is watching
SHOT_IDLE = 3.0                # ... and only this often when the phone has not asked for 20 s (screen locked)
# Tests only: start with a deliberately wrong mouse mapping, to prove _aim notices and corrects it. Never set in use.
TEST_START_MAPPING = os.environ.get('HANDOFF_TEST_START_MAPPING') or ''
ROUTE = re.compile(r'^' + PREFIX + r'/([A-Za-z0-9_-]{43})(?:/(start|frame|input|done))?/?$')
KEYS = {
    'Enter': [{'type': 'keyDown', 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13, 'text': '\r', 'unmodifiedText': '\r'},
              {'type': 'keyUp', 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13}],
    'Backspace': [{'type': 'rawKeyDown', 'key': 'Backspace', 'code': 'Backspace', 'windowsVirtualKeyCode': 8, 'nativeVirtualKeyCode': 8},
                  {'type': 'keyUp', 'key': 'Backspace', 'code': 'Backspace', 'windowsVirtualKeyCode': 8, 'nativeVirtualKeyCode': 8}],
    'Tab': [{'type': 'rawKeyDown', 'key': 'Tab', 'code': 'Tab', 'windowsVirtualKeyCode': 9, 'nativeVirtualKeyCode': 9},
            {'type': 'keyUp', 'key': 'Tab', 'code': 'Tab', 'windowsVirtualKeyCode': 9, 'nativeVirtualKeyCode': 9}],
}

# What is under a point, and what has focus. Reads only; runs in an isolated world (Page.createIsolatedWorld) so the
# page's own scripts cannot change what it sees. Mirrors the purchase lock's probe (labels, attrs, clickable).
PROBE = H.PROBE_MARK + r"""(()=>{const A=__ARGS__;
const t=s=>String(s==null?'':s).replace(/\s+/g,' ').trim().slice(0,160);
const by=(d,id)=>{const n=id&&d.getElementById(id);return n?n.innerText:''};
const labels=e=>{if(!e||!e.getAttribute)return [];const d=e.ownerDocument;const o=[e.innerText,e.value,e.getAttribute('aria-label'),e.title,e.getAttribute('alt')];
 const lb=e.getAttribute('aria-labelledby');if(lb)o.push(lb.split(/\s+/).map(x=>by(d,x)).join(' '));
 if(e.labels)for(const l of e.labels)o.push(l.innerText);
 const p=e.parentElement;if(p&&(p.innerText||'').length<=80)o.push(p.innerText);
 return o.map(t).filter(Boolean).slice(0,8)};
const attrs=e=>e&&e.getAttribute?t([e.id,e.getAttribute('name'),typeof e.className==='string'?e.className:'',e.getAttribute('data-automation-id'),e.getAttribute('data-testid'),e.getAttribute('data-test'),e.getAttribute('data-qa')].join(' ')):'';
const CL='button,a[href],input[type=submit],input[type=button],input[type=image],[role=button],[role=link],[onclick]';
const FR=/^(IFRAME|FRAME|EMBED|OBJECT)$/;
const desc=e=>e&&e.tagName?{tag:e.tagName.toLowerCase(),type:(e.getAttribute('type')||'').toLowerCase(),labels:labels(e),attrs:attrs(e),editable:!!e.isContentEditable,
 href:(e.tagName==='A'||e.tagName==='AREA')&&e.href?String(e.href).slice(0,600):'',formaction:e.getAttribute('formaction')?String(e.formAction||'').slice(0,600):''}:null;
const deep=(d,x,y)=>{let h=d.elementFromPoint(x,y);for(let i=0;h&&h.shadowRoot&&i<5;i++){const n=h.shadowRoot.elementFromPoint(x,y);if(!n||n===h)break;h=n}return h};
let hit=null;
if(A.xy){let d=document,x=A.xy[0],y=A.xy[1],h=null;const frames=[];
 for(let k=0;k<4;k++){h=deep(d,x,y);if(!h||!FR.test(h.tagName))break;let inner=null;try{inner=h.contentDocument}catch(_){}
  const src=String(h.src||h.data||'').slice(0,600);if(!inner){frames.push({src,same:false});break}
  frames.push({src,same:true});const r=h.getBoundingClientRect();x-=r.left;y-=r.top;d=inner}
 if(h){const c=(h.closest&&h.closest(CL))||h;hit={self:desc(h),clickable:desc(c),frame:FR.test(h.tagName),frames}}}
let f=document.activeElement,fd=document;const ff=[];
for(let k=0;f&&FR.test(f.tagName)&&k<4;k++){let inner=null;try{inner=f.contentDocument}catch(_){}
 const src=String(f.src||'').slice(0,600);if(!inner){ff.push({src,same:false});break}ff.push({src,same:true});fd=inner;f=inner.activeElement}
for(let k=0;f&&f.shadowRoot&&f.shadowRoot.activeElement&&k<5;k++)f=f.shadowRoot.activeElement;
let focus=null,form=[],action='';
if(f&&f!==fd.body&&f!==fd.documentElement){focus=desc(f);const fm=f.form||(f.closest&&f.closest('form'));
 if(fm){action=fm.getAttribute('action')?String(fm.action||'').slice(0,600):'';
  for(const b of fm.querySelectorAll('button,input[type=submit],input[type=image]')){const ty=(b.getAttribute('type')||'').toLowerCase();
   if(b.tagName==='BUTTON'&&ty&&ty!=='submit')continue;form.push({labels:labels(b),attrs:attrs(b)});if(form.length>=20)break}}}
return JSON.stringify({href:location.href,hit,focus,focus_frames:ff,form,form_action:action});
})()"""


# Where does a dispatched mouse event really land? In the sandbox, input sent while Chrome was taking a screenshot of
# the emulated tab landed at exactly half height (fixed by the io lock below). Zeus's Chrome is a normal headed window
# that could not be tested, so as a safety net every press is also aimed: the mouse is moved to the point, the page's own report of where it arrived is read back in the handoff's
# isolated world, and if it is off, the mapping is measured again and the move repeated before anything is pressed.
# The listener only records (the last 8 points) and lives only in the handoff's own world.
AIM_SETUP = H.PROBE_MARK + ("(()=>{if(!window.__zeusHandoffMoves){const a=window.__zeusHandoffMoves=[];"
                            "addEventListener('mousemove',e=>{a.push([e.clientX,e.clientY]);if(a.length>8)a.shift()},true)}"
                            "window.__zeusHandoffMoves.length=0;return 1})()")
AIM_READ = H.PROBE_MARK + "JSON.stringify((window.__zeusHandoffMoves||[]).splice(0))"


def probe_script(xy=None) -> str:
    return PROBE.replace('__ARGS__', json.dumps({'xy': xy}))


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(str(url)).hostname or '')[:120]
    except ValueError:
        return ''


def _allow(stamps: list, limit: int, window: float, now: float) -> bool:
    stamps[:] = [t for t in stamps if now - t < window]
    if len(stamps) >= limit:
        return False
    stamps.append(now)
    return True


# ---- one live view of the handoff tab ----------------------------------------------------------------------------------

class Viewer:
    def __init__(self, hub: 'Hub', session: dict, w: int, h: int):
        self.hub, self.rules = hub, hub.rules
        self.site_id, self.token, self.pub, self.name = session['site'], session['token'], session['id'], session['name']
        self.site = hub.table().get(self.site_id) or {}
        self.vid = secrets.token_urlsafe(12)
        self.w = int(min(max(int(w or 390), 320), 600))
        self.h = int(min(max(int(h or 640), 420), 1100))
        self.cdp = None
        self.sess = ''
        self.target = None
        self.existing = set()
        self.cal = (1.0, 0.0, 1.0, 0.0)    # page x = ax * sent x + bx (and y the same); corrected by _aim
        if TEST_START_MAPPING:
            self.cal = tuple(float(x) for x in TEST_START_MAPPING.split(','))
        self.main_frame = None
        self.cond = threading.Condition()
        self.frame, self.seq = b'', 0
        self.url = ''
        self.note_n, self.note = 0, ''
        self.counts = {'taps': 0, 'drags': 0, 'texts': 0, 'keys': 0, 'scrolls': 0, 'backs': 0, 'refused': 0, 'nav_blocked': 0, 'popups': 0, 'dialogs': 0}
        self.last_poll = time.time()
        self.pressed, self.pressed_at, self.moved = None, 0.0, False
        self.ilock = threading.Lock()
        # Input and screenshots never overlap: while Chrome takes a screenshot of an emulated (phone-sized) tab it changes
        # the emulation for a moment, and an input sent in that moment lands at the wrong place (found in the sandbox:
        # taps at exactly half height).
        self.io = threading.Lock()
        self.ended = None
        self.poke = threading.Event()
        self.burst_until = 0.0
        self.reapply = False           # set when the main page changed: the phone-size emulation is set again
        self.last_shot = b''

    # -- setup --
    def open(self):
        if not self.site:
            raise H.CDPError('unknown site')
        start = self.site.get('start_url') or ''
        if not self.rules.nav_ok(self.site, start)[0]:
            raise H.CDPError('no usable start page')
        c = self.cdp = H.CDP(self.hub.cdp, on_event=self.on_event)
        s = H.Sessions(self.hub.life).get(self.site_id) or {}
        tid = s.get('target_id') if s.get('target_id') in H.page_targets(c) else None
        fresh = tid is None
        if fresh:
            # Blank first, so the address guard is on before the store page loads.
            tid = c.call('Target.createTarget', {'url': 'about:blank', 'background': True}).get('targetId')
            if not tid:
                raise H.CDPError('Chrome did not open a tab')

            def keep(d, tid=tid):
                cur = d['sessions'].get(self.site_id)
                if cur and cur.get('token') == self.token:
                    cur.update(target_id=tid, target_ours=True)
            H.Sessions(self.hub.life).change(keep)
        self.target = tid
        self.sess = c.call('Target.attachToTarget', {'targetId': tid, 'flatten': True}).get('sessionId') or ''
        if not self.sess:
            raise H.CDPError('Chrome did not let the tab be shown')
        # Watch for new windows only now that the tab is known. Every tab that already exists (Zeus's own work among
        # them) is remembered and can never be closed by this view: discovery replays them all as "created".
        self.existing = set(t.get('targetId') for t in c.call('Target.getTargets').get('targetInfos', []))
        c.call('Target.setDiscoverTargets', {'discover': True})
        self._sc('Page.enable')
        self.main_frame = (self._sc('Page.getFrameTree').get('frameTree') or {}).get('frame', {}).get('id')
        self._sc('Fetch.enable', {'patterns': [{'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'}]})
        self._emulate()
        try:
            self._sc('Page.bringToFront', {}, timeout=4)
        except H.CDPError:
            pass
        self.url = self.live_url()
        if fresh or not self.rules.nav_ok(self.site, self.url)[0] or not self.url.startswith('https://'):
            self._sc('Page.navigate', {'url': start}, timeout=20)
            self.url = start
        threading.Thread(target=self._shot_loop, name='handoff-shots', daemon=True).start()

    # -- Chrome's news --
    def on_event(self, method, p, session):
        if method == '_closed':
            self.end('chrome_closed', from_reader=True)
            return
        if session and session != self.sess:
            return
        if method in ('Page.loadEventFired', 'Page.domContentEventFired', 'Page.frameStoppedLoading'):
            self._soon()
        elif method == 'Fetch.requestPaused':
            self._on_request(p)
        elif method == 'Page.frameNavigated':
            fr = p.get('frame') or {}
            if not fr.get('parentId'):
                self.url = str(fr.get('url') or '') + str(fr.get('urlFragment') or '')
                self.reapply = True
                self._wake()
                self._soon()
        elif method == 'Page.navigatedWithinDocument':
            if p.get('frameId') == self.main_frame:
                self.url = str(p.get('url') or '')
                self._wake()
                self._soon()
        elif method == 'Page.javascriptDialogOpening':
            self.cdp.send('Page.handleJavaScriptDialog', {'accept': False}, self.sess)
            self.counts['dialogs'] += 1
            self._set_note('dialog')
        elif method == 'Target.targetCreated':
            # Close a window ONLY when it is a brand-new page that the handoff tab itself opened.
            ti = p.get('targetInfo') or {}
            tid, opener = ti.get('targetId'), ti.get('openerId')
            if (self.target and opener and opener == self.target and tid and tid != self.target and ti.get('type') == 'page'
                    and tid not in self.existing):
                self.cdp.send('Target.closeTarget', {'targetId': ti.get('targetId')})
                self.counts['popups'] += 1
                H.log(self.hub.life, 'popup_closed', site=self.site_id, id=self.pub, host=_host(ti.get('url')))
                self._set_note('popup')
        elif method == 'Target.targetDestroyed':
            if p.get('targetId') == self.target:
                self.end('tab_closed', from_reader=True)
        elif method == 'Target.detachedFromTarget':
            if p.get('sessionId') == self.sess:
                self.end('detached', from_reader=True)

    def _on_request(self, p):
        rid = p.get('requestId')
        url = str((p.get('request') or {}).get('url') or '')
        if p.get('resourceType') == 'Document' and p.get('frameId') == self.main_frame:
            ok, why = self.rules.nav_ok(self.site, url)
            if not ok:
                self.cdp.send('Fetch.failRequest', {'requestId': rid, 'errorReason': 'Aborted'}, self.sess)
                self.counts['nav_blocked'] += 1
                H.log(self.hub.life, 'nav_blocked', site=self.site_id, id=self.pub, host=_host(url), kind=why)
                self._set_note('nav_blocked')
                return
        self.cdp.send('Fetch.continueRequest', {'requestId': rid}, self.sess)

    def _push(self, data: bytes):
        if not data:
            return
        with self.cond:
            self.frame, self.seq = data, self.seq + 1
            self.cond.notify_all()

    def _wake(self):
        with self.cond:
            self.cond.notify_all()

    def _set_note(self, code: str):
        with self.cond:
            self.note_n += 1
            self.note = code
            self.cond.notify_all()

    def _emulate(self):
        self._sc('Emulation.setDeviceMetricsOverride', {'width': self.w, 'height': self.h, 'deviceScaleFactor': 2, 'mobile': False})
        self._sc('Emulation.setFocusEmulationEnabled', {'enabled': True})

    def _emulate_again(self):
        """After the main page changed (above all when it moved to a new page process, as a sign-in that hops to another
        site does), set the phone-size emulation again before the next picture or input. Caller holds the io lock."""
        if self.reapply:
            self.reapply = False
            try:
                self._emulate()
            except H.CDPError:
                self.reapply = True

    def _soon(self):
        """Something happened (an input, a page change): pictures come quickly for a moment."""
        self.burst_until = time.time() + 1.5
        self.poke.set()

    def _shot_loop(self):
        """The pictures: Page.captureScreenshot of exactly the phone-sized viewport, quickly after an input or a page
        change, every 0.7 s otherwise (every 3 s while the phone is not asking). A picture identical to the last one is
        not sent again. No screencast: while Chrome makes a picture of an emulated tab it changes the emulation for a
        moment, and an input sent in that moment lands at the wrong place (the sandbox caught taps at exactly half
        height). A screenshot can be held off while an input runs (the io lock); a screencast frame cannot."""
        last = 0.0
        while not self.ended:
            gap = time.time() - last
            if gap < SHOT_MIN_GAP:
                time.sleep(SHOT_MIN_GAP - gap)
            self.poke.clear()
            try:
                with self.io:
                    self._emulate_again()
                    r = self._sc('Page.captureScreenshot', {'format': 'jpeg', 'quality': 60, 'fromSurface': True, 'captureBeyondViewport': False}, timeout=8)
                data = base64.b64decode(r.get('data') or '')
                if data and data != self.last_shot:
                    self.last_shot = data
                    self._push(data)
            except Exception:
                if self.cdp is None or not self.cdp.alive:
                    return
            last = time.time()
            wait = SHOT_BURST if last < self.burst_until else SHOT_IDLE if last - self.last_poll > 20 else SHOT_EVERY
            self.poke.wait(wait)

    # -- one command to the tab --
    SWAP_ERRORS = ('Not attached to an active page', 'Cannot find context', 'No frame with given id', 'Execution context was destroyed',
                   'Inspected target navigated or closed')

    def _sc(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        """A command to the handoff tab. While Chrome moves the tab to a new page process (a sign-in that hops to another
        site, like a store whose sign-in lives on its parent company's domain) commands fail for a moment; they are tried again for up to 5 seconds."""
        end = time.time() + 5
        while True:
            try:
                return self.cdp.call(method, params or {}, self.sess, timeout=timeout)
            except H.CDPError as e:
                if time.time() < end and self.cdp.alive and not self.ended and any(x in str(e) for x in self.SWAP_ERRORS):
                    time.sleep(0.2)
                    continue
                raise

    def _aim(self, x: float, y: float) -> tuple:
        """Move the mouse to CSS point (x, y) and make sure it arrived there (see AIM_SETUP). -> the coordinates to send
        so a press lands on (x, y). When the page cannot report (the point is over a frame from another site), the
        current mapping is trusted."""
        def plain():
            ax, bx, ay, by = self.cal
            return round((x - bx) / ax, 1), round((y - by) / ay, 1)
        try:
            ctx = self._sc('Page.createIsolatedWorld', {'frameId': self.main_frame, 'worldName': 'zeus-handoff'}).get('executionContextId')
            self._sc('Runtime.evaluate', {'expression': AIM_SETUP, 'contextId': ctx, 'returnByValue': True})

            def read():
                return json.loads((self._sc('Runtime.evaluate', {'expression': AIM_READ, 'contextId': ctx, 'returnByValue': True}).get('result') or {}).get('value') or '[]')

            def land(sx, sy):
                # Chrome hands mouse moves to the page with the next animation frame, not at once: clear what is there,
                # move, then wait (up to half a second) for the page's report of THIS move.
                read()
                self._sc('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': sx, 'y': sy, 'button': 'none', 'buttons': 0})
                for _ in range(20):
                    got = read()
                    if got:
                        return got[-1]
                    time.sleep(0.025)
                return None
            sx, sy = plain()
            c1 = land(sx, sy)
            if c1 is None or (abs(c1[0] - x) <= 1.5 and abs(c1[1] - y) <= 1.5):
                return sx, sy
            step = min(120.0, self.w / 3)
            tx, ty = sx + (step if c1[0] < self.w / 2 else -step), sy + (step if c1[1] < self.h / 2 else -step)
            c2 = land(tx, ty)
            if c2 is None or c2[0] == c1[0] or c2[1] == c1[1]:
                return sx, sy
            ax, ay = (c2[0] - c1[0]) / (tx - sx), (c2[1] - c1[1]) / (ty - sy)
            if not (0.2 < ax < 5 and 0.2 < ay < 5):
                return sx, sy
            self.cal = (ax, c1[0] - ax * sx, ay, c1[1] - ay * sy)
            H.log(self.hub.life, 'calibration', site=self.site_id, id=self.pub,
                  result=f'x={ax:.3f}*sent{self.cal[1]:+.1f} y={ay:.3f}*sent{self.cal[3]:+.1f}')
            sx, sy = plain()
            land(sx, sy)
            return sx, sy
        except (H.CDPError, ValueError, TypeError):
            return plain()

    # -- what the phone sees --
    def mode(self) -> str:
        return 'input' if self.rules.input_ok(self.site, self.url)[0] else 'view'

    def live_url(self) -> str:
        return str((self.cdp.call('Target.getTargetInfo', {'targetId': self.target}).get('targetInfo') or {}).get('url') or '')

    def probe(self, xy=None) -> dict:
        end = time.time() + 5
        while True:                                # a page that is just changing has no context for a moment
            ctx = self._sc('Page.createIsolatedWorld', {'frameId': self.main_frame, 'worldName': 'zeus-handoff'}).get('executionContextId')
            try:
                r = self._sc('Runtime.evaluate', {'expression': probe_script(xy), 'contextId': ctx, 'returnByValue': True})
                break
            except H.CDPError as e:
                if time.time() >= end or not any(x in str(e) for x in self.SWAP_ERRORS):
                    raise
                time.sleep(0.2)
        try:
            return json.loads((r.get('result') or {}).get('value') or '{}')
        except ValueError:
            return {}

    def to_css(self, nx, ny) -> tuple:
        """A point on the phone's picture (0..1 each way) -> CSS pixels in the tab. The viewport is our own override
        (w x h), so it is used directly; the pictures are screenshots of exactly that viewport."""
        vw, vh = float(self.w), float(self.h)
        nx = min(max(float(nx), 0.0), 1.0)
        ny = min(max(float(ny), 0.0), 1.0)
        return round(nx * vw, 1), round(ny * vh, 1)

    # -- input --
    def handle(self, d: dict) -> dict:
        kind = str(d.get('kind') or '')
        with self.ilock, self.io:
            if self.ended:
                return {'ok': False, 'ended': True}
            try:
                self._emulate_again()
                fn = {'down': self._down, 'move': self._move, 'up': self._up, 'text': self._text, 'key': self._key,
                      'scroll': self._scroll, 'back': self._back}.get(kind)
                if fn is None:
                    return {'ok': False, 'note': 'bad'}
                out = fn(d)
                self._soon()
                return out
            except (H.CDPError, PermissionError, KeyError, TypeError, ValueError, OSError):
                return {'ok': False, 'note': 'chrome_down'}

    def _refuse(self, code: str, what: str) -> dict:
        self.counts['refused'] += 1
        H.log(self.hub.life, 'refused', site=self.site_id, id=self.pub, what=what, why=code)
        return {'ok': False, 'note': code}

    def _mouse(self, typ: str, x: float, y: float, **kw):
        """x, y: CSS pixels in the tab (what the probe saw), sent through the measured mapping."""
        ax, bx, ay, by = self.cal
        self._sc('Input.dispatchMouseEvent', {'type': typ, 'x': round((x - bx) / ax, 1), 'y': round((y - by) / ay, 1), **kw})

    def _gate(self, what: str):
        """None when input may reach the tab now, else a refusal. The address is read fresh from Chrome."""
        url = self.live_url()
        self.url = url or self.url
        if not self.rules.input_ok(self.site, url)[0]:
            return self._refuse('view_only', what)
        return None

    def _down(self, d):
        x, y = self.to_css(d['x'], d['y'])
        if self.pressed:
            self._mouse('mouseReleased', *self.pressed, button='left', buttons=0, clickCount=1)
            self.pressed = None
        no = self._gate('tap')
        if no:
            return no
        pr = self.probe([x, y])
        if not self.rules.input_ok(self.site, pr.get('href'))[0]:
            return self._refuse('view_only', 'tap')
        why = self.rules.tap_refusal(self.site, pr)
        if why:
            return self._refuse(why, 'tap')
        sx, sy = self._aim(x, y)
        self._sc('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': sx, 'y': sy, 'button': 'left', 'buttons': 1, 'clickCount': 1})
        self.pressed, self.pressed_at, self.moved = (x, y), time.time(), False
        self.counts['taps'] += 1
        return {'ok': True}

    def _move(self, d):
        if not self.pressed:
            return {'ok': True, 'ignored': True}
        x, y = self.to_css(d['x'], d['y'])
        self._mouse('mouseMoved', x, y, button='left', buttons=1)
        self.moved = True
        return {'ok': True}

    def _up(self, d):
        if not self.pressed:
            return {'ok': True, 'ignored': True}
        px, py = self.pressed
        x, y = self.to_css(d['x'], d['y'])
        if self.rules.input_ok(self.site, self.live_url())[0]:
            if self.rules.tap_refusal(self.site, self.probe([x, y])) == 'commit':
                x, y = px, py                      # never let go over a commit button
        else:
            x, y = px, py                          # the page moved on: let go where the press was
        self._mouse('mouseReleased', x, y, button='left', buttons=0, clickCount=1)
        self.pressed = None
        if self.moved:
            self.counts['drags'] += 1
        return {'ok': True}

    def _text(self, d):
        text = re.sub(r'[\r\n]', '', str(d.get('text') or ''))[:TEXT_MAX]
        if not text:
            return {'ok': True, 'ignored': True}
        no = self._gate('text')
        if no:
            return no
        pr = self.probe(None)
        if not self.rules.input_ok(self.site, pr.get('href'))[0]:
            return self._refuse('view_only', 'text')
        why = H.can_type(pr, self.rules, self.site)
        if why:
            return self._refuse(why, 'text')
        for ch in text:                            # one character at a time, like typing (code boxes move along)
            self._sc('Input.insertText', {'text': ch})
        self.counts['texts'] += 1
        return {'ok': True}

    def _key(self, d):
        key = str(d.get('key') or '')
        if key not in KEYS:
            return self._refuse('bad', 'key')
        no = self._gate('key')
        if no:
            return no
        if key == 'Enter':
            pr = self.probe(None)
            if not self.rules.input_ok(self.site, pr.get('href'))[0]:
                return self._refuse('view_only', 'key')
            why = self.rules.enter_refusal(self.site, pr)
            if why:
                return self._refuse(why, 'key')
        for ev in KEYS[key]:
            self._sc('Input.dispatchKeyEvent', ev)
        self.counts['keys'] += 1
        return {'ok': True}

    def _scroll(self, d):
        """Scrolling is looking, so it works on any page the tab may be on (view-only pages too)."""
        url = self.live_url()
        if not self.rules.nav_ok(self.site, url)[0]:
            return self._refuse('view_only', 'scroll')
        vw, vh = float(self.w), float(self.h)
        dy = min(max(float(d.get('dy') or 0), -1.5), 1.5) * vh
        self._mouse('mouseWheel', round(vw / 2, 1), round(vh / 2, 1), deltaX=0, deltaY=round(dy, 1))
        self.counts['scrolls'] += 1
        return {'ok': True}

    def _back(self, d):
        hist = self._sc('Page.getNavigationHistory')
        i = int(hist.get('currentIndex') or 0)
        entries = hist.get('entries') or []
        if i <= 0 or i - 1 >= len(entries):
            return self._refuse('no_back', 'back')
        e = entries[i - 1]
        if not str(e.get('url') or '').startswith('https://') or not self.rules.nav_ok(self.site, e.get('url'))[0]:
            return self._refuse('no_back', 'back')
        self._sc('Page.navigateToHistoryEntry', {'entryId': e['id']})
        self.counts['backs'] += 1
        return {'ok': True}

    # -- the end --
    def end(self, why: str, from_reader: bool = False):
        with self.cond:
            if self.ended:
                return
            self.ended = why
            self.cond.notify_all()
        self.poke.set()
        t = threading.Thread(target=self._cleanup, args=(why,), name='handoff-cleanup', daemon=True)
        t.start()
        if not from_reader:
            t.join(6)

    def _cleanup(self, why: str):
        c = self.cdp
        if c is not None and c.alive and self.sess:
            if self.pressed:
                try:
                    self._mouse('mouseReleased', *self.pressed, button='left', buttons=0, clickCount=1)
                except Exception:
                    pass
            for m, p in (('Fetch.disable', {}), ('Emulation.clearDeviceMetricsOverride', {}),
                         ('Emulation.setFocusEmulationEnabled', {'enabled': False})):
                try:
                    c.call(m, p, self.sess, timeout=3)
                except Exception:
                    pass
            try:
                c.call('Target.detachFromTarget', {'sessionId': self.sess}, timeout=3)
            except Exception:
                pass
        if c is not None:
            c.close()
        H.log(self.hub.life, 'viewer_end', site=self.site_id, id=self.pub, why=why, **self.counts)


# ---- all sessions ------------------------------------------------------------------------------------------------------

class Hub:
    def __init__(self, life: Path, cdp: str, page: str):
        self.life, self.cdp, self.page = Path(life), cdp, page
        self.rules = H.Rules()
        self.viewers = {}
        self.lock = threading.Lock()
        self.starts, self.starts_all = {}, []
        self.done_locks = {}
        self.bad_count, self.bad_logged = 0, 0.0
        self._table, self._table_at = {}, 0.0
        self.knows = K.Page(self.life)     # the "what Zeus knows" page

    def table(self) -> dict:
        if time.time() - self._table_at > 30:
            self._table, self._table_at = H.site_table(self.life), time.time()
        return self._table

    def session(self, token: str) -> dict | None:
        s = H.Sessions(self.life).find(token)
        if s is None:
            self.bad_count += 1
            if time.time() - self.bad_logged > 60:
                H.log(self.life, 'bad_link', count=self.bad_count)
                self.bad_count, self.bad_logged = 0, time.time()
        return s

    def know_session(self, token: str) -> dict | None:
        """The open "what Zeus knows" session for this token; None (the plain 404) when the switch file
        knows-page-on is not there, or the token is wrong or ended. Counted with the sign-in's wrong links."""
        s = K.find(self.life, token) if K.on(self.life) else None
        if s is None:
            self.bad_count += 1
            if time.time() - self.bad_logged > 60:
                H.log(self.life, 'bad_link', count=self.bad_count)
                self.bad_count, self.bad_logged = 0, time.time()
        return s

    def viewer_for(self, s: dict, vid: str):
        v = self.viewers.get(s['site'])
        if v is not None and v.vid == vid and v.token == s['token'] and not v.ended:
            return v
        return None

    def start(self, s: dict, w, h) -> tuple:
        now = time.time()
        with self.lock:
            if not _allow(self.starts.setdefault(s['id'], []), *START_PER_SESSION, now) or not _allow(self.starts_all, *START_GLOBAL, now):
                H.log(self.life, 'start_refused', site=s['site'], id=s['id'], why='too many starts')
                return 429, {'ok': False, 'note': 'rate'}
            old = self.viewers.pop(s['site'], None)
        if old is not None:
            old.end('second_viewer')
        v = Viewer(self, s, w, h)
        try:
            v.open()
        except Exception as e:
            v.end('open_failed')
            H.log(self.life, 'start_failed', site=s['site'], id=s['id'], error=f'{type(e).__name__}: {str(e)[:100]}')
            return 503, {'ok': False, 'note': 'chrome_down'}
        with self.lock:
            prev = self.viewers.get(s['site'])
            self.viewers[s['site']] = v
        if prev is not None and prev is not v:
            prev.end('second_viewer')
        H.log(self.life, 'start', site=s['site'], id=s['id'], width=v.w, height=v.h)
        return 200, {'ok': True, 'v': v.vid, 'mode': v.mode()}

    def done(self, s: dict) -> dict:
        lock = self.done_locks.setdefault(s['site'], threading.Lock())
        if not lock.acquire(timeout=DONE_WAIT):
            return {'ok': False, 'status': 'busy', 'words': 'Still checking. Give it a moment.', 'closed': False}
        try:
            status, usable = H.check_now(s['site'], self.life, self.cdp)
            words = H.done_words(status, s['name'], usable)
            H.log(self.life, 'done', site=s['site'], id=s['id'], result=status, usable=usable)
            closed = False
            if status == 'signed_in':
                closed = self.finish(s['site'], 'done', 'signed in', token=s['token'])
            return {'ok': True, 'status': status, 'words': words, 'closed': closed}
        finally:
            lock.release()

    def finish(self, site: str, state: str, why: str, token: str | None = None) -> bool:
        """Close a session: token dead, view stopped, the tab this handoff opened closed."""
        before = H.Sessions(self.life).close(site, state, why, token=token)
        with self.lock:
            v = self.viewers.pop(site, None)
        if v is not None:
            v.end(state)
        if before is None:
            return False
        if before.get('target_ours') and before.get('target_id'):
            H.close_tab(self.cdp, before['target_id'])
        H.log(self.life, 'expire' if state == 'expired' else 'close', site=site, id=before.get('id'), why=why)
        return True

    def _engine_says_signed_in(self, site: str, opened_ts: float) -> bool:
        try:
            d = json.loads((self.life / 'signins.json').read_text())
            s = (d.get('sites') or {}).get(site) or {}
            t = datetime.fromisoformat(str(s.get('checked_at'))).timestamp() if s.get('checked_at') else 0
            return s.get('status') == 'signed_in' and not s.get('lapse') and t > opened_ts
        except (OSError, ValueError, TypeError):
            return False

    def reap_once(self, look_engine: bool = False):
        now = time.time()
        try:
            K.expire(self.life, now)       # an ended page link has its token wiped from the file
        except Exception as e:
            H.log(self.life, 'reaper_error', error='knows ' + type(e).__name__)
        sessions = H.Sessions(self.life).load()['sessions']
        for site, s in sessions.items():
            if s.get('state') != 'open':
                continue
            if float(s.get('expires_ts') or 0) <= now:
                self.finish(site, 'expired', 'two hours passed', token=s.get('token'))
            elif look_engine and self._engine_says_signed_in(site, float(s.get('opened_ts') or 0)):
                self.finish(site, 'done', 'the engine saw the sign-in by itself', token=s.get('token'))
        sessions = H.Sessions(self.life).load()['sessions']
        with self.lock:
            views = list(self.viewers.items())
        for site, v in views:
            s = sessions.get(site) or {}
            if v.ended:
                with self.lock:
                    if self.viewers.get(site) is v:
                        self.viewers.pop(site, None)
            elif s.get('state') != 'open' or s.get('token') != v.token:
                with self.lock:
                    if self.viewers.get(site) is v:
                        self.viewers.pop(site, None)
                v.end('session_ended')
            elif now - v.last_poll > IDLE_END:
                with self.lock:
                    if self.viewers.get(site) is v:
                        self.viewers.pop(site, None)
                v.end('idle')
            elif v.pressed and now - v.pressed_at > HOLD_MAX:
                px, py = v.pressed
                v.handle({'kind': 'up', 'x': px / v.w, 'y': py / v.h})

    def reap_loop(self, stop: threading.Event):
        n = 0
        while not stop.wait(3):
            n += 1
            try:
                self.reap_once(look_engine=(n % 10 == 0))
            except Exception as e:
                H.log(self.life, 'reaper_error', error=type(e).__name__)

    def shutdown(self):
        with self.lock:
            views, self.viewers = list(self.viewers.values()), {}
        for v in views:
            v.end('server_stopped')


# ---- HTTP ---------------------------------------------------------------------------------------------------------------

NOT_FOUND = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
             '<meta name="robots" content="noindex"><title>Not available</title><style>body{font:17px/1.4 -apple-system,system-ui,sans-serif;'
             'margin:40px 20px;color:#222;background:#fff}@media(prefers-color-scheme:dark){body{color:#eee;background:#111}}</style></head>'
             "<body><h1 style=\"font-size:22px\">This link isn't working.</h1><p>It may have ended. Ask Zeus for a new one.</p></body></html>")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    hub: Hub


class Handler(BaseHTTPRequestHandler):
    server_version = 'zeus-signin'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):            # request lines carry the token: nothing is written anywhere
        pass

    def log_error(self, fmt, *args):
        pass

    @property
    def hub(self) -> Hub:
        return self.server.hub

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None, csp: str | None = None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Robots-Tag', 'noindex, nofollow')
        self.send_header('Content-Security-Policy', csp or "default-src 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj).encode(), 'application/json')

    def _404(self):
        self._send(404, NOT_FOUND.encode(), 'text/html; charset=utf-8', csp="default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")

    def _route(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == PREFIX + '/ping':
            return 'ping', None, None
        k = K.ROUTE.match(path)
        if k:
            s = self.hub.know_session(k.group(1))
            return ('know_' + (k.group(2) or 'page'), s, k.group(1)) if s is not None else (None, None, None)
        m = ROUTE.match(path)
        if not m:
            return None, None, None
        s = self.hub.session(m.group(1))
        if s is None:
            return None, None, None
        return (m.group(2) or 'page'), s, m.group(1)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            what, s, _ = self._route()
            if what == 'ping':
                return self._send(200, b'ok', 'text/plain; charset=utf-8')
            if what == 'page':
                return self._page(s)
            if what == 'frame':
                return self._frame(s)
            if what == 'know_page':
                return self._know_page(s)
            return self._404()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            what, s, _ = self._route()
            if what not in ('start', 'input', 'done', 'know_show', 'know_act'):
                return self._404()
            if self.headers.get('X-Handoff') != '1' or not str(self.headers.get('Content-Type') or '').startswith('application/json'):
                return self._json(400, {'ok': False, 'note': 'bad'})
            n = int(self.headers.get('Content-Length') or 0)
            if n > BODY_MAX:
                return self._json(413, {'ok': False, 'note': 'bad'})
            try:
                body = json.loads(self.rfile.read(n) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {'ok': False, 'note': 'bad'})
            if what == 'know_show':
                return self._json(*self.hub.knows.show(s))
            if what == 'know_act':
                return self._json(*self.hub.knows.press(s, body))
            if what == 'start':
                code, out = self.hub.start(s, body.get('w'), body.get('h'))
                return self._json(code, out)
            if what == 'done':
                return self._json(200, self.hub.done(s))
            v = self.hub.viewer_for(s, str(body.get('v') or ''))
            if v is None:
                return self._json(409, {'ok': False, 'note': 'moved'})
            return self._json(200, v.handle(body))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _page(self, s: dict):
        nonce = secrets.token_urlsafe(16)
        fill = {'NAME': s['name'], 'UNTIL': H.clock(float(s['expires_ts'])), 'SMS': H.sms_link(s['name']), 'SMS_TEXT': H.sms_text(s['name']), 'NONCE': nonce}
        page = re.sub(r'\{\{(NAME|UNTIL|SMS|SMS_TEXT|NONCE)\}\}', lambda m: html.escape(fill[m.group(1)], quote=True), self.hub.page)
        csp = (f"default-src 'none'; img-src blob:; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self._send(200, page.encode(), 'text/html; charset=utf-8', csp=csp)

    def _know_page(self, s: dict):
        """The page shell. Nothing personal in it (a link preview fetches this); the content comes only from
        POST .../show when the owner taps Show."""
        nonce = secrets.token_urlsafe(16)
        fill = {'UNTIL': H.clock(float(s['expires_ts'])), 'NONCE': nonce}
        page = re.sub(r'\{\{(UNTIL|NONCE)\}\}', lambda m: html.escape(fill[m.group(1)], quote=True), K.shell())
        csp = (f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; "
               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self._send(200, page.encode(), 'text/html; charset=utf-8', csp=csp)

    def _frame(self, s: dict):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        vid = (q.get('v') or [''])[0]
        try:
            after = int((q.get('after') or ['0'])[0])
            seen_note = int((q.get('note') or ['0'])[0])
        except ValueError:
            after, seen_note = 0, 0
        v = self.hub.viewer_for(s, vid)
        if v is None:
            return self._json(409, {'ended': 'moved'})
        v.last_poll = time.time()
        with v.cond:
            v.cond.wait_for(lambda: v.seq > after or v.note_n > seen_note or v.ended, timeout=FRAME_WAIT)
            frame, seq, note_n, note, ended = v.frame, v.seq, v.note_n, v.note, v.ended
        v.last_poll = time.time()
        if ended:
            return self._json(409 if ended == 'second_viewer' else 410, {'ended': ended})
        head = {'X-Seq': seq, 'X-Mode': v.mode(), 'X-Note-N': note_n, 'X-Note': note or ''}
        if seq > after and frame:
            return self._send(200, frame, 'image/jpeg', extra=head)
        return self._send(204, b'', 'text/plain', extra=head)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Zeus phone sign-in handoff server (127.0.0.1 only)')
    ap.add_argument('--port', type=int, default=PORT)
    a = ap.parse_args(argv)
    life = H.life_home()
    cdp = H.cdp_base(life)
    page = (HERE / 'page.html').read_text(encoding='utf-8')
    hub = Hub(life, cdp, page)
    srv = Server(('127.0.0.1', a.port), Handler)
    srv.hub = hub
    stop = threading.Event()
    threading.Thread(target=hub.reap_loop, args=(stop,), name='handoff-reaper', daemon=True).start()

    def bye(*_):
        stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    H.log(life, 'server_start', port=a.port, cdp_port=H.local_hostport(cdp)[1], rules=hub.rules.source)
    print(f'zeus-signin handoff listening on 127.0.0.1:{a.port}', flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        hub.shutdown()
        srv.server_close()
        H.log(life, 'server_stop')
    return 0


if __name__ == '__main__':
    sys.exit(main())

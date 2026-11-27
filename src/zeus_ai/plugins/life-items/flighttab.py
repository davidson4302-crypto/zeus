"""Keep the agent's flight-status checks on an existing tracker. Never run the caller's browser program."""
import ast
import contextlib
import http.client
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit


def _settings():
    mod = sys.modules.get('settings')
    if mod is not None and hasattr(mod, 'OWNER_FIRST'):
        return mod
    spec = importlib.util.spec_from_file_location('settings', Path(__file__).resolve().parents[2] / 'engine' / 'settings.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return sys.modules.setdefault('settings', mod)


settings = _settings()
Zeus = Path(settings.PROFILE_DIR).resolve()          # the agent's own Hermes profile: the hook acts only there
RETRY = ('Use the existing flight tracker with one direct public flight-status URL per leg in sequence. '
         'If no tracker is available, read a public flight-status source without opening a browser tab.')
FLIGHT_TEXT = re.compile(r'flight.?status|flight.?track(?:er|ing)?|flight.?details|track(?:ing)?\b.{0,35}\bflights?\b|\bflights?\b.{0,45}\b(?:arrival|depart|delay|landed|on.time|timelines?|gate|status|tracker|pickup)\b', re.I|re.S)
URL_TEXT = re.compile(r'https?://[^\s\'"`<>\])]+', re.I)
ROUTES = {
    'flightstats.com': r'/v2/flight-(?:tracker|details)/[a-z0-9]{2,3}/\d+[a-z]?(?:/\d+)?/?',
    'flightaware.com': r'/live/flight/[a-z0-9]+(?:/[a-z0-9/_-]+)?/?',
    'flightradar24.com': r'/data/flights/[a-z0-9]+/?',
    'flightview.com': r'/(?:flight-tracker|flighttracker)/[^#]+',
    'aa.com': r'/(?:travelInformation/flights/status(?:/[^#]*)?|tracking/flightStatus(?:/[^#]*)?)',
    'united.com': r'/(?:[a-z]{2}/[a-z]{2}/)?flightstatus/details/[a-z0-9/_-]+',
    'delta.com': r'/flightstatus/[a-z0-9/_-]+',
}


class Hold(ValueError):
    pass


def flight_url(value):
    try:
        u = urlsplit(value)
        if u.scheme != 'https' or u.port not in (None,443) or u.username or u.password:
            return False
        host = (u.hostname or '').removeprefix('www.').removeprefix('www1.')
        return bool(host in ROUTES and re.fullmatch(ROUTES[host], u.path, re.I))
    except (TypeError, ValueError, AttributeError):
        return False


def tab_id(tab):
    return str(tab.get('id') or tab.get('targetId') or tab.get('target_id') or '')


def local_targets():
    conn = http.client.HTTPConnection('127.0.0.1', 9333, timeout=1.5)
    try:
        conn.request('GET', '/json/list')
        result = conn.getresponse()
        return json.loads(result.read(1000000)) if result.status == 200 else []
    finally:
        conn.close()


def select_tab(tabs, preferred, urls):
    candidates = [t for t in tabs if isinstance(t,dict) and t.get('type','page') == 'page'
                  and tab_id(t) and flight_url(t.get('url'))]
    for t in candidates:
        if tab_id(t) == preferred:
            return t
    for url in urls:
        for t in candidates:
            if flight_key(t['url']) == flight_key(url):
                return t
    return min(candidates, key=tab_id, default=None)


def flight_key(url):
    u = urlsplit(url)
    host = (u.hostname or '').removeprefix('www.')
    if host == 'flightstats.com':
        parts = u.path.strip('/').split('/')
        return host, tuple(x.lower() for x in parts[2:4])
    return host, u.path.rstrip('/').lower()



def query_one(path, sql, param):
    if not path.is_file():
        return None
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.3)) as db:
        row = db.execute(sql, (param,)).fetchone()
        return row[0] if row else None


def relevant(home, context, args, seed):
    code, session = str(args.get('code','')), str(args.get('session',''))
    if FLIGHT_TEXT.search(code) or FLIGHT_TEXT.search(session) or any(flight_url(url) for url in URL_TEXT.findall(code)):
        return True
    for key in ('session_id','task_id'):
        sid = str(context.get(key) or '')
        match = re.match(r'^cron_([a-zA-Z0-9-]+)(?:_|$)',sid)
        if match:
            if match[1] == seed.get('watch_job_id'):
                return True
            try:
                jobs = json.loads((home/'cron/jobs.json').read_text()).get('jobs',[])
                job = next((j for j in jobs if j.get('id') == match[1]), {})
                wid = str(job.get('name','')).removeprefix('zeus-work:')
                if wid.startswith('work_'):
                    raw = query_one(home/'plugin-data/zeus-work/state.sqlite3','SELECT payload FROM work WHERE id=?',wid)
                    item = json.loads(raw or '{}')
                    if FLIGHT_TEXT.search(str(item.get('title',''))):
                        return True
            except (OSError, ValueError, sqlite3.Error):
                pass
    sid = str(context.get('session_id') or '')
    if not sid or sid.startswith('cron_'):
        return False
    try:
        # One current owner request in this session. Old flight messages in the shared chat give no authority.
        raw = query_one(home/'state.db', "SELECT content FROM messages WHERE session_id=? AND role='user' AND active=1 AND timestamp>strftime('%s','now')-3600 ORDER BY id DESC LIMIT 1", sid)
        return bool(raw and FLIGHT_TEXT.search(str(raw)[:6000]))
    except (OSError, sqlite3.Error):
        return False


UNKNOWN = object()


def literal(node, env):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id,UNKNOWN)
    if isinstance(node,(ast.List,ast.Tuple)):
        return [literal(n,env) for n in node.elts]
    if isinstance(node,ast.JoinedStr):
        parts = [literal(n,env) for n in node.values]
        return ''.join(map(str,parts)) if UNKNOWN not in parts else UNKNOWN
    if isinstance(node,ast.FormattedValue):
        return literal(node.value,env)
    if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add):
        a,b=literal(node.left,env),literal(node.right,env)
        if isinstance(a,str) and isinstance(b,str):
            return a+b
    if isinstance(node,ast.Subscript):
        a,b=literal(node.value,env),literal(node.slice,env)
        if isinstance(a,(list,tuple)) and isinstance(b,int) and -len(a)<=b<len(a):
            return a[b]
    if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='enumerate' and len(node.args)==1:
        seq=literal(node.args[0],env)
        if isinstance(seq,(list,tuple)):
            return list(enumerate(seq))
    if isinstance(node,ast.Compare) and len(node.ops)==1 and isinstance(node.ops[0],(ast.Eq,ast.NotEq)):
        a,b=literal(node.left,env),literal(node.comparators[0],env)
        if UNKNOWN not in (a,b):
            return (a==b) == isinstance(node.ops[0],ast.Eq)
    return UNKNOWN


def bind(node, value, env):
    if isinstance(node,ast.Name):
        env[node.id]=value
    elif isinstance(node,(ast.Tuple,ast.List)) and isinstance(value,(tuple,list)) and len(node.elts)==len(value):
        for name, v in zip(node.elts,value):
            bind(name,v,env)


UNSUPPORTED = re.compile(r'\b(?:window|globalThis|self)\s*(?:\.\s*open|\[\s*[\'"]open)|Target\s*\.\s*(?:createTarget|closeTarget)|/json/(?:new|close)|\b(?:new_window|open_window|close_tab|activate_tab|ThreadPoolExecutor\s*\(|ProcessPoolExecutor\s*\(|asyncio\s*\.|subprocess\s*\.|os\.system|create_task\s*\(|\.start\s*\(|\.submit\s*\(|\.click\s*\()|\b(?:location(?:\.href)?|window\.location)\s*=(?!=)',re.I)


def plan(code):
    if len(code)>50000 or UNSUPPORTED.search(code):
        raise Hold('This flight check requested a new window, raw action or parallel browser work.')
    tree=ast.parse(code)
    nav={'goto_url','new_tab'}
    actions=[]
    switches=[]
    detail_read = bool(re.search(r'view flight details',code,re.I) and 'querySelectorAll' in code)
    for n in ast.walk(tree):
        if isinstance(n,(ast.AsyncFor,ast.AsyncFunctionDef,ast.Await,ast.FunctionDef,ast.Lambda,ast.While)):
            raise Hold('Use a simple sequential flight lookup.')
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in {'cdp','exec','eval','open','__import__','click_element','press_key','fill_input','close_tab','ensure_real_tab'}:
            if n.func.id=='cdp' and n.args and literal(n.args[0],{})=='Page.reload':
                continue # owned readback below navigates afresh
            raise Hold('This check requested raw browser access or an unsupported helper.')
    budget=1000
    def walk(nodes,env):
        nonlocal budget
        for n in nodes:
            budget-=1
            if budget<0:
                raise Hold('This flight check has too many steps.')
            if isinstance(n,ast.For):
                seq=literal(n.iter,env)
                if not isinstance(seq,(list,tuple)):
                    if any(isinstance(q,ast.Name) and q.id in nav|{'switch_tab'} for q in ast.walk(n)):
                        raise Hold('The destinations in this flight loop could not be resolved.')
                    continue
                for item in seq:
                    bind(n.target,item,env); walk(n.body,env)
                continue
            if isinstance(n,ast.If):
                test=literal(n.test,env)
                if test is UNKNOWN:
                    if detail_read and isinstance(n.test,ast.Name) and n.test.id in env and env[n.test.id] is UNKNOWN:
                        for q in n.body:
                            if any(isinstance(c,ast.Call) and isinstance(c.func,ast.Name) and c.func.id in nav and c.args and isinstance(c.args[0],ast.Subscript) and isinstance(c.args[0].value,ast.Name) and c.args[0].value.id==n.test.id for c in ast.walk(q)):
                                actions.append(('detail',None)); continue
                            walk([q],dict(env))
                        continue
                    if any(isinstance(q,ast.Name) and q.id in nav|{'switch_tab'} for q in ast.walk(n)):
                        raise Hold('The target of this conditional navigation is unclear.')
                    continue
                walk(n.body if test else n.orelse,env); continue
            if isinstance(n,ast.Try):
                walk(n.body,env); walk(n.orelse,env); walk(n.finalbody,env); continue
            if isinstance(n,ast.Assign):
                for name in n.targets:
                    bind(name,literal(n.value,env),env)
            for c in ast.walk(n):
                if not isinstance(c,ast.Call): continue
                names = {c.func.id} if isinstance(c.func,ast.Name) else ({a.id for a in (c.func.body,c.func.orelse) if isinstance(a,ast.Name)} if isinstance(c.func,ast.IfExp) else set())
                if names & nav:
                    arg=c.args[0] if c.args else next((k.value for k in c.keywords if k.arg=='url'),None)
                    url=literal(arg,env)
                    if not isinstance(url,str) or not flight_url(url):
                        raise Hold('The destination is not a direct supported flight-status page.')
                    actions.append(('goto',url))
                if 'switch_tab' in names:
                    arg=c.args[0] if c.args else next((k.value for k in c.keywords if k.arg=='target'),None)
                    target=literal(arg,env)
                    if not isinstance(target,str): raise Hold('Use the exact designated flight tab ID when switching.')
                    switches.append(target)
    walk(tree.body,{})
    if len(actions)>20: raise Hold('The check exceeds twenty sequential flight page reads.')
    return actions,switches


def make_hook(home, current_home, targets=local_targets):
    home=Path(home).resolve()
    def hook(tool_name='', args=None, **context):
        if Path(current_home()).resolve()!=home or home!=Zeus or not isinstance(args,dict): return None
        if tool_name not in {'browser_exec','browser_cdp','terminal','execute_code'}: return None
        command = str(args.get('command') or args.get('code') or '')
        raw_creation = ((tool_name=='browser_cdp' and args.get('method') in {'Target.createTarget','Target.closeTarget','Browser.createTarget'}) or
                        (tool_name in {'terminal','execute_code'} and bool(re.search(r'Target\.createTarget|/json/new|--new-window|window\.open\(',command,re.I))))
        if tool_name!='browser_exec' and not raw_creation: return None
        try:
            seed=json.loads((home/'runtime/flight-tab.json').read_text())
        except (OSError,ValueError): seed={}
        if not relevant(home,context,({**args,'code':str(args)} if raw_creation else args),seed): return None
        if raw_creation:
            return {'action':'block','message':'This flight check attempted to create or close a browser target. '+RETRY}
        try:
            actions,switches=plan(str(args.get('code') or ''))
            tab=select_tab(targets(),seed.get('target_id'),[url for op,url in actions if op=='goto'])
            if not tab: raise Hold('No existing flight tracker tab is available.')
            chosen=tab_id(tab)
            if any(s not in {chosen,seed.get('target_id')} for s in switches):
                raise Hold('A different browser tab was requested.')
            child=Path(__file__).resolve()
            # Pass plain values to the trusted child; the caller's original code is never executed.
            code=("# verified single-tab flight read\nfrom runpy import run_path as _flight_load\n"
                  f"_flight_load({str(child)!r})['read_pages'](globals(), {chosen!r}, {actions!r})")
            return {'action':'modify','args':{'code':code,'session':''}}
        except (Hold,OSError,ValueError,SyntaxError,TypeError,http.client.HTTPException) as err:
            return {'action':'block','message':f'Flight browser step held. {err} {RETRY}'}
    return hook

def same_flight_page(expected, observed):
    from urllib.parse import parse_qs
    if not flight_url(observed) or flight_key(expected)!=flight_key(observed): return False
    def identity(url):
        u = urlsplit(url)
        values = parse_qs(u.query)
        parts = u.path.strip('/').split('/')
        if flight_key(url)[0]=='flightstats.com' and len(parts)==5:
            values.setdefault('flightId',[parts[-1]])
        return values
    a,b=identity(expected),identity(observed)
    if a.get('flightId') and b.get('flightId')==a['flightId']:
        return not any(a.get(k) and b.get(k) and a[k]!=b[k] for k in ('year','month','date'))
    return all(not a.get(k) or a[k]==b.get(k) for k in ('year','month','date','flightId'))


@contextlib.contextmanager
def pinned_transport(target):
    """Route the native actions and the native receipt's own observations to the chosen target."""
    import sys
    helpers = sys.modules.get('browser_harness.helpers')
    if helpers is None or not callable(getattr(helpers, '_send', None)):
        raise Hold('The browser could not bind its flight reads to the selected tab. ' + RETRY)
    forward = helpers._send  # Preserve all installed browser and purchase controls.
    pin = {'session': None, 'connection': lambda: forward({'meta': 'connection_status'})}

    def target_info():
        response = forward({'method': 'Target.getTargetInfo', 'params': {'targetId': target}})
        page = (response.get('result') or {}).get('targetInfo') or {}
        if tab_id(page) != target or not flight_url(page.get('url')):
            raise Hold('The chosen flight tab closed or changed before navigation. ' + RETRY)
        return page

    def checked(req, *args, **kwargs):
        method = req.get('method', '')
        if method == 'Page.navigate':
            if not pin['session'] or not flight_url((req.get('params') or {}).get('url')):
                raise Hold('Could not confirm the target of this flight navigation. ' + RETRY)
            target_info()
        if pin['session']:
            if req.get('meta') == 'current_tab':
                # ActionReceipts.observe reads the identity here, alongside URL and document state.
                page = target_info()
                return {'targetId': target, 'url': page['url'], 'title': page.get('title', '')}
            if method.startswith(('Page.', 'DOM.', 'Accessibility.', 'Runtime.')):
                req = {**req, 'session_id': pin['session']}
        elif method == 'Runtime.evaluate':
            source = (req.get('params') or {}).get('expression', '')
            if source.startswith("if(document.title.startsWith(") and 'document.title.slice(3)' in source:
                # Native switch_tab has an optional decorative write on the previously attached page.
                return {'result': {}}
        return forward(req, *args, **kwargs)

    helpers._send = checked
    try:
        yield pin
    finally:
        helpers._send = forward


def visible_flight(api, session, nodes, source):
    """Read non-ignored screen-reader text in document order and resolve visible provider detail links."""
    from urllib.parse import urljoin
    indexed = {str(n['nodeId']): n for n in nodes if 'nodeId' in n}
    children = {str(c) for n in nodes for c in n.get('childIds', [])}
    todo = [n for n in reversed(nodes) if str(n.get('nodeId')) not in children]
    seen = set()
    lines = []
    detail_links = []
    while todo:
        node = todo.pop()
        key = str(node.get('nodeId'))
        if key in seen:
            continue
        seen.add(key)
        child_ids = node.get('childIds', [])
        todo.extend(indexed[str(i)] for i in reversed(child_ids) if str(i) in indexed)
        if node.get('ignored'):
            continue
        role = node.get('role', {}).get('value', '')
        name = str((node.get('name') or {}).get('value') or '').strip()
        if name and (role == 'StaticText' or (not child_ids and role in {'heading', 'link', 'button', 'status'})):
            if not lines or lines[-1] != name:
                lines.append(name)
        if role != 'link' or not re.fullmatch(r'view\s+flight\s+details', name, re.I):
            continue
        href = next((item.get('value', {}).get('value') for item in node.get('properties', [])
                     if item.get('name') == 'url'), None)
        if not href and node.get('backendDOMNodeId'):
            element = api['cdp']('DOM.describeNode', session_id=session, backendNodeId=node['backendDOMNodeId'])
            attrs = element.get('node', {}).get('attributes', [])
            href = dict(zip(attrs[::2], attrs[1::2])).get('href')
        if href:
            link = urljoin(source, href)
            if not same_flight_page(source, link):
                raise Hold('The provider detail link points at an unverified flight. ' + RETRY)
            if link not in detail_links:
                detail_links.append(link)
    return '\n'.join(lines)[:20000], detail_links


def read_snapshot(api, target, session, expected, valid_target):
    from datetime import datetime, timezone
    def current_entry():
        history = api['cdp']('Page.getNavigationHistory', session_id=session)
        entries = history.get('entries') or []
        index = history.get('currentIndex', -1)
        return entries[index] if isinstance(index, int) and 0 <= index < len(entries) else {}

    before = current_entry()
    if not same_flight_page(expected, before.get('url', '')):
        return None
    nodes = api['cdp']('Accessibility.getFullAXTree', session_id=session).get('nodes', [])
    text, details = visible_flight(api, session, nodes, before['url'])
    after = current_entry()
    tab = valid_target()
    if (not text or before.get('id') != after.get('id') or before['url'] != after.get('url') or
            not same_flight_page(expected, tab.get('url', ''))):
        return None
    return {'target_id': target, 'url': after['url'], 'title': tab.get('title') or after.get('title'),
            'readAt': datetime.now(timezone.utc).isoformat(), 'text': text, 'details': details}


def read_pages(api, target, actions):
    import fcntl
    def valid_target():
        page = next((t for t in api['list_tabs']() if tab_id(t) == target and flight_url(t.get('url'))), None)
        if not page:
            raise Hold('The designated flight tab closed or changed. ' + RETRY)
        return page

    with open(__file__, 'rb') as lease, pinned_transport(target) as pin:
        fcntl.flock(lease, fcntl.LOCK_EX)
        first = valid_target()
        connection = pin['connection']()
        if connection.get('target_id') == target and tab_id(connection.get('page') or {}) == target:
            # The daemon reports the target and session as a pair. Re-selecting it would be a no-op action.
            pin['session'] = connection.get('session_id')
        else:
            pin['session'] = api['switch_tab'](target)
        if not pin['session']:
            raise Hold('The browser could not attach to the designated flight tab. ' + RETRY)
        if not actions or actions[0][0] == 'detail':
            actions = [('goto', first['url']), *actions]
        previous = None
        for action, url in actions:
            valid_target()
            if action == 'detail':
                links = (previous or {}).get('details') or []
                if not links:
                    continue
                url = links[0]
            if not flight_url(url):
                raise Hold('The destination is not a supported public tracker. ' + RETRY)
            api['goto_url'](url)
            api['wait_for_load']()
            end = time.monotonic() + 6
            while True:
                snapshot = read_snapshot(api, target, pin['session'], url, valid_target)
                if snapshot:
                    break
                if time.monotonic() >= end:
                    raise Hold('The provider did not return a current readable page for that flight. ' + RETRY)
                time.sleep(.2)
            previous = snapshot
            print(json.dumps({'target_id': target, 'source': snapshot['url'], 'read_at': snapshot['readAt'],
                              'title': snapshot['title'], 'page_text': snapshot['text']}, ensure_ascii=False))

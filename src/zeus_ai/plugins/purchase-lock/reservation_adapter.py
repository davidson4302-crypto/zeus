#!/usr/bin/env python3
"""Reservation checkpoints for Zeus. Only the native browser purchase lock can use an approval."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

_ENGINE_DIR = Path(__file__).resolve().parents[2] / 'engine'     # zeus_ai/engine, where settings.py lives
try:
    if str(_ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(_ENGINE_DIR))
    import settings as _settings
    PROFILE = Path(_settings.PROFILE_DIR)
    _AGENT_DIR = Path(_settings.HERMES_AGENT_DIR)
    _DEFAULT_ENGINE = Path(_settings.ENGINE_DIR)
except Exception:                                   # a bare browser child without the engine: default layout
    PROFILE = Path.home() / '.hermes/profiles/zeus'
    _AGENT_DIR = Path.home() / '.hermes/hermes-agent'
    _DEFAULT_ENGINE = _ENGINE_DIR
ENGINE = Path(os.environ.get('LIFE_ENGINE_DIR') or _DEFAULT_ENGINE)
PROVIDERS = {'hotel': 'Trip.com', 'table': 'OpenTable'}
HOSTS = {'hotel': {'www.trip.com','trip.com'}, 'table': {'www.opentable.com','opentable.com'}}
FIELDS = ('venue','location','date','party','selection','total','fees','policy','button')
FINALS = re.compile(r'(?:pay(?: now| at hotel)?|book(?: now| and pay| with [\w ]+)?|(?:confirm|complete|make|submit|finish|reserve)(?: (?:your|my|this|the|a))? (?:hotel |table )?(?:reservation|booking|payment)|(?:confirm|book|reserve) (?:and|&) (?:pay|book|reserve)|(?:complete|confirm) purchase|reserve(?: now| table| this table)?|confirm)', re.I)
AMOUNT = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.,])"
CURRENCY = r"(?:USD|EUR|GBP|CAD|AUD|JPY|KRW|CNY|US\$|[$€£¥₩])"
MONEY = re.compile(CURRENCY+r'\s*'+AMOUNT+'|'+AMOUNT+r'\s*'+CURRENCY,re.I)
ABSENT = re.compile(r'\b(?:no charge|no (?:booking |reservation |prepayment |deposit |mandatory |additional )?(?:fees?|charge|deposit|prepayment|payment)(?:s)?|free(?: to reserve)?|included|complimentary)\b',re.I)
SECRET = re.compile(r'@|https?://|(?:\d[ -]?){13,19}\b|\b(?:OTP|password|CVV|CVC)\b',re.I)
UNCERTAIN = re.compile(r'\b(?:unknown|unverified|not shown|not disclosed|TBD|estimated total|excluding (?:fees|tax)|total before tax)\b',re.I)
OBSERVE = """(()=>{const V=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';return JSON.stringify({url:location.href,text:(document.body?.innerText||'').slice(0,300000),buttons:[...document.querySelectorAll('button,a[href],input[type=submit],input[type=button],[role=button]')].filter(e=>V(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true').map(e=>(e.innerText||e.getAttribute('aria-label')||e.value||'').trim()).filter(Boolean)});})()"""

class Hold(ValueError):
    pass

def require(value, reason):
    if not value:
        raise Hold(reason)

def flat(s):
    return ' '.join(s.split())

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def engine(name):
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    return __import__(name)

def origin(url):
    u = urlsplit(url)
    require(u.scheme == 'https' and not u.username and not u.password and u.port in (443,None),'The provider origin is unverified.')
    return u.hostname

def hotel_path(path):
    return bool(re.match(r'^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:hotels?(?:/|$|-)|(?:booking|order|payment)/hotels?(?:/|$))',path,re.I))

def provider(url,kind):
    require(origin(url) in HOSTS[kind], 'This is not the selected provider.')

def store(path):
    path.mkdir(parents=True,mode=0o700,exist_ok=True)
    con = sqlite3.connect(path / 'reservations.sqlite3',timeout=8,isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute('CREATE TABLE IF NOT EXISTS reservations (key TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, slot TEXT, state TEXT NOT NULL)')
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_reservation ON reservations(slot) WHERE status IN ('armed','awaiting_receipt','confirmed')")
    return con

@contextlib.contextmanager
def transaction(path):
    con=store(path)
    try:
        con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()

def item(con,key):
    row=con.execute('SELECT * FROM reservations WHERE key=?',(key,)).fetchone()
    require(row,'Start this reservation first.')
    return dict(row) | {'state':json.loads(row['state'])}

def save(con,row,status,**updates):
    row['status']=status
    row['state'].update(updates)
    con.execute('UPDATE reservations SET status=?,slot=?,state=? WHERE key=?',(status,row.get('slot'),json.dumps(row['state']),row['key']))

def view(row):
    blocked=row['status'] in ('awaiting_receipt','confirmed','stopped')
    st=row['state']
    fields = ['facts','revision','checkpoint','checkpoint_details','receipt','tab','ask','why']
    return {'key':row['key'],'kind':row['kind'],'status':row['status'], **{k:st[k] for k in fields if k in st and (k!='ask' or not blocked)}}

def intent(kind,f):
    return digest([kind]+[flat(f[k]).casefold() for k in ('venue','location','date','party', 'checkout' if kind=='hotel' else 'time')])

def review_lines(text):
    # Cross-sells, footers and recommendation carousels are not evidence about the selection.
    lines=[]
    for raw in text.splitlines():
        v=flat(raw)
        if re.match(r'(?:other (?:hotels|restaurants|rooms|properties)|similar (?:hotels|restaurants|rooms|properties)|recommended for you|you (?:might|may) also like|more places to stay|other options|people also viewed)',v,re.I):
            break
        if v: lines.append(v)
    return lines

def selected_terms(lines,kind,facts):
    # If the provider explicitly labels the merchant, use that field as the authority.
    venue_labels=r'(?:hotel|property|restaurant)(?: name)?'
    labelled=[m.group(1).strip() for line in lines if (m:=re.match(venue_labels+r'\s*:\s*(.+)$',line,re.I))]
    if labelled:
        require({v.casefold() for v in labelled}=={facts['venue'].casefold()},'The selected venue differs from the provider review.')
    total=facts['total'].casefold()
    payable=re.compile(r'\b(?:grand total|total(?: price| for (?:your |the )?(?:stay|reservation)| (?:cost|amount))?|(?:amount|payment) due (?:now|today)|pay (?:now|at (?:the )?property))\b',re.I)
    candidate=[]
    for i,line in enumerate(lines):
        if not payable.search(line) or re.search(r'per night|nightly|subtotal|before tax|excluding',line,re.I):
            continue
        if MONEY.search(line):
            candidate.append(line.casefold())
        elif i+1<len(lines) and MONEY.search(lines[i+1]):
            candidate.append((line+' '+lines[i+1]).casefold())
    if kind=='hotel' or not ABSENT.search(total):
        require(any(total in row for row in candidate),'Use the total for the selected booking, not a per-night or unrelated price.')
        if kind=='hotel':
            strong=[line for line in candidate if re.search(r'grand total|total for (?:the |your )?stay|total price|total cost',line,re.I)]
            if strong:
                require(any(total in line for line in strong),'The amount differs from the selected full-stay total.')
    fee=facts['fees'].casefold()
    for i,line in enumerate(lines):
        # Separately payable fees/deposits must be named even when another fee is waived.
        if not re.search(r'fee|deposit|surcharge|tax|payable|due at (?:the )?property',line,re.I):continue
        detail=line
        if not MONEY.search(detail) and i+1<len(lines) and MONEY.search(lines[i+1]) and re.search(r'mandatory|resort|facility|deposit|no.show',line,re.I):
            detail+=' '+lines[i+1]
        if MONEY.search(detail) and re.search(r'mandatory|resort|facility|no.show|deposit|surcharge|due at (?:the )?property',detail,re.I):
            require(all(m.group(0).casefold() in fee for m in MONEY.finditer(detail)) and
                    any(w in fee for w in ('fee','deposit','surcharge','tax','payable','property')),
                    'The exact fees or deposit must include the separate provider charge.')
        if re.search(r'(?:fees?|tax(?:es)?|charges?) (?:excluded|not included|extra)|excluding (?:fees?|tax)',line,re.I):
            require(False,'The all-in total is unverified. Keep the checkpoint and get the complete price.')

def validate(page,kind,f):
    provider(page['url'],kind)
    if kind=='hotel':require(hotel_path(urlsplit(page['url']).path),'Use the hotel review page for the selected stay.')
    keys=FIELDS+(('checkout',) if kind=='hotel' else ('time',))
    require(set(keys)==set(f), 'Provide these exact provider fields: '+', '.join(keys))
    lines=review_lines(page['text'])
    text=flat(' '.join(lines)).casefold()
    facts={}
    for k in keys:
        v=f[k]
        require(isinstance(v,str) and 0<len(v.strip())<=360 and not SECRET.search(v) and not UNCERTAIN.search(v),
                f'The {k} field is missing, sensitive or uncertain.')
        v=flat(v)
        require(v.casefold() in text or k=='button', f'The current page does not show the quoted {k}.')
        facts[k]=v
    require(FINALS.fullmatch(facts['button']) and [flat(v).casefold() for v in page['buttons']].count(facts['button'].casefold())==1,
            'A unique enabled final reservation control is required.')
    require(MONEY.search(facts['total']) or kind=='table' and ABSENT.search(facts['total']), 'The complete amount due at booking needs source evidence.')
    require(MONEY.search(facts['fees']) or ABSENT.search(facts['fees']), 'Provide the mandatory fees, deposit or explicit included/no-fee language.')
    require(re.search(r'cancel|refund|no[ -]?show|nonreturnable|prepaid|final sale', facts['policy'],re.I), 'The cancellation or no-show terms are missing.')
    require(re.search(r'\d',facts['date']) and re.search(r'\d',facts['party']), 'The dated stay and party size must be explicit.')
    when=facts['checkout' if kind=='hotel' else 'time']
    require(re.search(r'\d',when) and (kind=='table' or flat(when)!=facts['date']), 'Show the table time or distinct checkout date.')
    require(not re.search(r'\b(?:sold out|no longer available|reservation expired|session expired|room is unavailable)\b',text,re.I), 'The selection is unavailable or expired.')
    selected_terms(lines,kind,facts)
    return facts

def revision(page,facts):
    # Never persist redirect tokens or the raw page. Bind even unquoted provider terms and checkout edits.
    return digest([page['url'],flat(page['text']),facts])

def offer(kind,f):
    when=f"{f['date']} to {f['checkout']}" if kind=='hotel' else f"{f['date']} at {f['time']}"
    return (f"{PROVIDERS[kind]}: {f['venue']}, {f['location']}. {when}; {f['party']}; {f['selection']}. "
            f"{f['total']}. {f['fees']}. {f['policy']}. Shall I book this exact {'room' if kind=='hotel' else 'table'}?")

def tabs(sandbox):
    if sandbox:
        return [{'id':k,'url':v['url']} for k,v in json.loads((sandbox/'pages.json').read_text()).items()]
    with urlopen('http://127.0.0.1:9333/json/list',timeout=3) as response:
        return [r for r in json.load(response) if r.get('type')=='page']

def read_page(sandbox,tab,kind):
    if sandbox:
        page=json.loads((sandbox/'pages.json').read_text()).get(tab)
        require(page,'The stored browser tab is unavailable.')
    else:
        entry=next((r for r in tabs(None) if r['id']==tab),None)
        require(entry,'The current browser tab is unavailable.')
        provider(entry['url'],kind)
        endpoint=urlsplit(entry['webSocketDebuggerUrl'])
        require(endpoint.scheme=='ws' and endpoint.hostname in ('127.0.0.1','localhost') and endpoint.port==9333 and endpoint.path==f'/devtools/page/{tab}', 'Unexpected local browser endpoint.')
        ws=engine('pageread')._Socket(entry['webSocketDebuggerUrl'],4)
        try:
            ws.send(json.dumps({'id':1,'method':'Runtime.evaluate','params':{'expression':OBSERVE,'returnByValue':True}}))
            while True:
                msg=json.loads(ws.recv())
                if msg.get('id')==1:
                    page=json.loads(msg['result']['result']['value'])
                    break
        finally:
            ws.close()
    provider(page['url'],kind)
    return page

def trusted_sessions():
    import subprocess
    python=_AGENT_DIR/'venv/bin/python'
    reader="import pathlib,sys,yaml,json; cfg=yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()); print(json.dumps(cfg['plugins']['entries']['zeus-work']['settings']['owner']['owner_ids']))"
    result=subprocess.run([str(python),'-IB','-c',reader,str(PROFILE/'config.yaml')],capture_output=True,timeout=5)
    require(result.returncode==0,'Unable to verify the configured owner identity.')
    owners=set(json.loads(result.stdout))
    with contextlib.closing(sqlite3.connect(f'file:{PROFILE}/state.db?mode=ro',uri=True)) as con:
        return {sid for sid,source,uid in con.execute("SELECT id,source,user_id FROM sessions WHERE chat_type='dm'")
                if f'{source}:{uid}' in owners}

def local_operator_row(profile, raw, session, message_id):
    # Only the authenticated LOCAL transport writes this receipt. Ordinary agent/terminal relays
    # can appear inside the same shared transcript and must not become purchase approval.
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',message_id or '') or raw.lstrip().startswith('['):
        return False
    path=profile/'runtime/shared_conversation_requests'/(message_id+'.json')
    try:
        receipt=json.loads(path.read_text())
    except (OSError,ValueError):
        return False
    source=receipt.get('source')
    if source not in ('cli','gui','tui') or receipt.get('request_id')!=message_id or receipt.get('session_id')!=session:
        return False
    expected=hashlib.sha256(json.dumps([profile.name,raw,source,[]]).encode()).hexdigest()
    return receipt.get('status') in ('queued','running','completed') and receipt.get('fingerprint')==expected

def owner_rows(after,now):
    rows=engine('chats').messages(max(after,now-900),until=now+0.001,path=PROFILE/'state.db',include_terminal=True)
    dm=trusted_sessions()
    allowed=set()
    with contextlib.closing(sqlite3.connect(f'file:{PROFILE}/state.db?mode=ro',uri=True)) as con:
        for r in rows:
            if r['who']!='owner' or r.get('via')!='direct':continue
            source=con.execute('SELECT content,platform_message_id FROM messages WHERE id=? AND session_id=? AND role=?',
                               (r['id'],r['session'],'user')).fetchone()
            if not source:continue
            raw,msg_id=source
            if (r['session'] in dm and not raw.lstrip().startswith('[')) or local_operator_row(PROFILE,raw,r['session'],msg_id):
                allowed.add(r['id'])
    return [r for r in rows if r['who']=='zeus' or r['id'] in allowed]

def owner_yes(sandbox,ask,after,now):
    reader=engine('hisreply')
    rows=(json.loads((sandbox/'messages.json').read_text()) if sandbox else
          owner_rows(after,now))
    rows=sorted((r for r in rows if max(after,now-900)<=r['at']<=now),key=lambda r:(r['at'],r['id']))
    # The offer and reply must be neighbors on the same verified private route.
    # Another question elsewhere can receive its own yes or no without altering this grant.
    conversations={}
    matched=None
    for r in rows:
        history=conversations.setdefault(r['session'],[])
        if r['who']=='owner':
            text=r['text'].strip()
            answer=reader.read(text)
            if matched:
                same=r['session']==matched['session']
                other_question=any(m['who']=='zeus' and '?' in m['text'] and flat(m['text'])!=flat(ask)
                                   for m in history if m['at']>matched['at'])
                global_stop=re.search(r'^(?:stop|cancel|pause|hold) (?:all (?:bookings|reservations|work)|everything)\b',text,re.I)
                direct_stop=(re.match(r'^(?:stop|cancel|hold|wait|don.t book|never mind|nevermind)\b',text,re.I) or
                             answer['answer'] in ('no','changed'))
                names_reservation=re.search(r'\b(?:booking|reservation|room|table|hotel)\b',text,re.I)
                if global_stop or (same and direct_stop and (not other_question or names_reservation)):
                    matched=None
            if (history and history[-1]['who']=='zeus' and flat(history[-1]['text'])==flat(ask)
                    and answer['answer']=='yes' and answer['bare'] and not answer['all']):
                matched=r
        history.append(r)
    if matched:return str(matched['id'])
    raise Hold('No recent owner yes to this exact offer was verified. Use the owner’s private Zeus chat or another verified direct message when this terminal or app cannot verify the owner.')

def funds_and_terms(kind,row,page,facts,tab,now):
    f=validate(page,kind,facts)
    rev=revision(page,f)
    changed=rev!=row['state'].get('revision') or tab!=row['state'].get('tab')
    return f,rev,changed

def run(args,sandbox,body):
    cmd=args.command
    if cmd=='tabs':
        return {'tabs':[{'tab':r['id'],'provider':PROVIDERS[k]} for r in tabs(sandbox) for k,hosts in HOSTS.items() if urlsplit(r['url']).hostname in hosts]}
    path=((sandbox or PROFILE)/'state'/'reservations')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,100}',args.key),'Use the original request ID for the checkpoint.')
    with transaction(path) as con:
        if cmd=='start':
            con.execute("INSERT OR IGNORE INTO reservations VALUES(?,?, 'research',NULL,'{}')",(args.key,args.kind))
        r=item(con,args.key)
        st=r['state']; kind=r['kind']; now=time.time()
        if cmd=='start':
            require(kind==args.kind,'The request ID already belongs to another kind of reservation.')
        if cmd=='stop':
            require(r['status'] not in ('awaiting_receipt','confirmed'),'A provider action may already exist. Reconcile the original reservation.')
            save(con,r,'stopped',why='Stopped by request.')
        if cmd=='pause':
            require(r['status'] not in ('awaiting_receipt','confirmed','stopped'),'This reservation is already submitted or stopped.')
            require(any(e['id']==args.tab for e in tabs(sandbox)),'The named browser tab is unavailable.')
            details={}
            for key,value in body.items():
                require(key in ('venue','location','date','checkout','time','party','selection') and isinstance(value,str) and
                        0<len(value.strip())<=120 and not SECRET.search(value),'Use only non-sensitive request details.')
                details[key]=flat(value)
            save(con,r,'paused',checkpoint=args.reason,checkpoint_details=details or st.get('checkpoint_details',{}),tab=args.tab,ask='',revision='',why='Resume the original provider tab before preparing.')
        if cmd in ('prepare','approve'):
            require(r['status'] not in ('awaiting_receipt','confirmed','stopped'),'Reconcile this reservation. It cannot be submitted again.')
            page=read_page(sandbox,args.tab,kind)
            f,rev,changed=funds_and_terms(kind,r,page,body,args.tab,now)
            if changed:
                r['slot']=intent(kind,f)
                save(con,r,'prepared',facts=f,ask=offer(kind,f),revision=rev,tab=args.tab,prepared=now,checkpoint='final review',why='Ask for approval of the current terms.')
            elif cmd=='approve':
                require(r['status'] in ('prepared','armed'),'Prepare the current offer before approving.')
                if not sandbox:
                    guard=PROFILE/'workspace/life-engine'
                    require(not any((guard/name).exists() for name in ('buy-lock-off','buy-lock-shadow')),'The existing purchase lock is not enforcing.')
                oid=owner_yes(sandbox,st['ask'],st['prepared'],now)
                for other in con.execute("SELECT key FROM reservations WHERE slot=? AND key!=? AND status='armed'",(r['slot'],r['key'])).fetchall():
                    old=item(con,other['key'])
                    if old['state']['expires']<=now:save(con,old,'prepared',why='The unused approval expired.')
                held=con.execute("SELECT key FROM reservations WHERE slot=? AND key!=? AND status IN ('armed','awaiting_receipt','confirmed')",(r['slot'],r['key'])).fetchone()
                require(not held,'An existing reservation already covers this selection. Check that one.')
                save(con,r,'armed',owner=oid,expires=now+120,why='Use the native browser to click the exact displayed control once.')
        if cmd=='receipt':
            require(r['status'] in ('awaiting_receipt','confirmed'),'No submitted reservation is awaiting proof.')
            if r['status']=='confirmed':
                return view(r)
            page=read_page(sandbox,args.tab,kind)
            ref=body.get('reference','')
            require(isinstance(ref,str) and re.fullmatch(r'[A-Za-z0-9-]{4,32}',ref) and not SECRET.search(ref),'A short, non-sensitive provider confirmation reference is required.')
            text=flat(page['text'])
            require(re.search(r'\b(?:confirmation|reservation|booking)\s*(?:number|no\.?|id|code|ref(?:erence)?|#)\s*[:#-]?\s*'+re.escape(ref)+r'\b',text,re.I),'The provider does not show the stated reservation reference.')
            require(re.search(r'\b(?:confirmed|successfully (?:booked|reserved)|reservation is complete)\b',text,re.I) and not re.search(r'\b(?:not (?:yet )?confirmed|unconfirmed|cancelled|canceled|failed|declined|pending confirmation|confirmation pending)\b',text,re.I),'The provider has not confirmed this booking.')
            for key in ('venue','date','checkout' if kind=='hotel' else 'time','party'):
                require(st['facts'][key].casefold() in text.casefold(),f'The confirmation does not match {key}.')
            save(con,r,'confirmed',receipt=ref,checkpoint='provider confirmation',why='Provider readback verified.')
        return view(r)

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sandbox',type=Path)
    commands=p.add_subparsers(dest='command',required=True)
    for cmd in ('start','tabs','resume','pause','prepare','approve','receipt','stop'):
        op=commands.add_parser(cmd)
        if cmd!='tabs':
            op.add_argument('key')
        if cmd=='start':
            op.add_argument('kind',choices=PROVIDERS)
        if cmd in ('prepare','approve','receipt','pause'):
            op.add_argument('tab')
        if cmd=='pause':
            op.add_argument('reason',choices=['sign-in','app-only','unavailable','missing-terms'])
            op.add_argument('--with-details',action='store_true',help='Read non-sensitive request details on stdin')
    args=p.parse_args()
    try:
        s=args.sandbox.resolve() if args.sandbox else None
        if s:
            require((s/'state'/'reservations').resolve().is_relative_to(s) and (s/'pages.json').resolve().is_relative_to(s) and (s/'messages.json').resolve().is_relative_to(s),'Fixture paths must remain inside the fixture directory.')
            require(s!=PROFILE and not s.is_relative_to(PROFILE) and (s/'pages.json').is_file(),'Use a separate fixture directory with pages.json.')
        else:
            require(Path(os.environ.get('HERMES_HOME','/nonexistent')).resolve()==PROFILE.resolve(),'Run from the active Zeus profile, or use --sandbox.')
        body=json.load(sys.stdin) if args.command in ('prepare','approve','receipt') or getattr(args,'with_details',False) else {}
        require(isinstance(body,dict),'Supply a JSON object of review fields.')
        print(json.dumps(run(args,s,body),ensure_ascii=False))
    except (OSError,ValueError,KeyError,sqlite3.Error,ImportError) as err:
        print(json.dumps({'status':'held','reason':str(err) if isinstance(err,Hold) else 'Could not verify reservation evidence ('+type(err).__name__+').'}))
        return 2
    return 0

if __name__=='__main__':
    sys.exit(main())

# These functions are invoked by Zeus's native purchase-lock in the browser child.
def snapshot_from_probe(probe):
    return {'url':probe['href'],'text':probe.get('text',''),'buttons':[b['labels'][0] for b in probe.get('buttons',[]) if b.get('labels')]}

def gate(path,tab,probe,claim,sandbox=None):
    page=snapshot_from_probe(probe)
    hit=(probe.get('hit') or {}).get('clickable') or {}
    labels=[flat(v).casefold() for v in (hit.get('labels') or [])[:1]]
    require(len(labels)==1,'The provider final control cannot be identified.')
    require((path/'reservations.sqlite3').is_file(),'Prepare this reservation and ask the owner first.')
    with transaction(path) as con:
        matches=[]
        for row in con.execute("SELECT key FROM reservations WHERE status='armed'"):
            r=item(con,row['key'])
            st=r['state']
            if st['tab']!=tab or st['expires']<=time.time():
                continue
            facts=st['facts']
            if facts['button'].casefold()!=labels[0] or st['revision']!=revision(page,facts):
                continue
            validate(page,r['kind'],facts)
            if claim:
                oid=owner_yes(sandbox,st['ask'],st['prepared'],time.time())
                require(oid==st['owner'],'Approval changed. Review the current booking.')
            matches.append(r)
        require(len(matches)==1,'There is no single current approval for this exact button, date, price and terms.')
        r=matches[0]
        if claim:
            save(con,r,'awaiting_receipt',why='Submitted or outcome unknown. Read the provider confirmation before any other action.')
        return r['key']

def locked_click(fence, ar, call, action, decision):
    """Consume the yes at the last native dispatch. A lost response leaves an open receipt."""
    initial=ar.dispatch_click
    fired=False
    path=fence.life.parent.parent/'state'/'reservations'
    sandbox=None if path.resolve()==(PROFILE/'state'/'reservations').resolve() else path.parent.parent
    def guarded(x,y,*args,**kwargs):
        nonlocal fired
        require(not fired,'This one-use approval already reached dispatch.')
        tab=ar.raw['current_tab']()['targetId']
        probe=fence.probe(ar,[x,y])
        key=gate(path,tab,probe,True,sandbox)
        require(key==decision['reservation_key'],'The selected reservation changed.')
        fired=True
        return initial(x,y,*args,**kwargs)
    ar.dispatch_click=guarded
    try:
        result=call()
        if isinstance(result,dict) and fired:
            result['purchase_lock']={'reservation':decision['reservation_key'],'pressed':1,'state':'awaiting_receipt'}
            ar.save()
        return result
    finally:
        ar.dispatch_click=initial

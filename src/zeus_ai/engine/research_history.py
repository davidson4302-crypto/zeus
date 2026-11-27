"""Local, bounded links to earlier evidence for hourly research."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path
import json
import re

import settings

DAY = 86400
# The owner's mailboxes by label (settings.OWNER_EMAILS); a row from any other mailbox is never history.
MAILBOXES = set(settings.OWNER_EMAILS)
# Optional words, senders or account numbers whose mail is never used as history (config `research.history_exclude`,
# a list of plain strings; empty by default). They are also left out of the archive search.
_EXCLUDE = settings.get('research.history_exclude', []) or []
HISTORY_EXCLUDE = [str(t).strip() for t in (_EXCLUDE.split(',') if isinstance(_EXCLUDE, str) else _EXCLUDE) if str(t).strip()]
STOP = re.compile('|'.join(r'(?<![\w])' + re.escape(t).replace(r'\ ', r'[\W_]*') + r'(?![\w])' for t in HISTORY_EXCLUDE) if HISTORY_EXCLUDE else r'(?!x)x', re.I)
OWNER_WORDS = {w.lower() for w in str(settings.OWNER_NAME or '').split() if w and settings.OWNER_NAME != 'the owner'} | \
              {str(n).lower() for n in (settings.get('owner.nicknames', []) or [])}
ADDRESS = re.compile(r"[\w.!#$%&'*+/=?^_`{|}~-]+@[a-z\d.-]+\.[a-z]{2,}", re.I)
REFERENCE = re.compile(r'\b(?:reservation|booking|policy|contract|order|claim|invoice|ticket|confirmation)(?:\s+(?:no\.?|number|id|code))?\s*[:#-]?\s+([a-z\d][a-z\d-]{4,30})\b', re.I)
GENERIC = {'gmail.com','outlook.com','icloud.com','yahoo.com','hotmail.com','aol.com','google.com','googlemail.com','composio.dev'}
PLACE = re.compile(r'\b(?:in|to|near|from|for|through|coverage in|travel to)\s+([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,2})\b')
TRAVEL = re.compile(r'\b(?:hotel|trip|travel|border|rental|coverage|insurance|booking|itinerary|airport|flight)\b', re.I)


def allowed(row):
    box = (row.get('mail') or {}).get('mailbox', row.get('mailbox', ''))
    if box and box not in MAILBOXES:
        return False
    return not STOP.search(json.dumps(row, ensure_ascii=False, default=str))


def entities(row):
    title = str(row.get('subject') or row.get('title') or '')
    who = str(row.get('who') or row.get('sender') or '')
    body = ' '.join(str(row.get(k) or '') for k in ('text','description','summary'))[:2000]
    body = body.split('[This email has ')[0]
    terms = set()
    for addr in ADDRESS.findall(who + ' ' + title + ' ' + body):
        email = addr.lower().rstrip('.')
        if email in settings.OWN_ADDRESSES:
            continue
        terms.add(('address', email))
        domain = email.split('@')[-1]
        if domain not in GENERIC:
            terms.add(('vendor', domain))
    for name, _ in getaddresses([who]):
        name = ' '.join(name.split()).strip()
        if re.fullmatch(r"[A-Z][\w'.-]+(?: [A-Z][\w'.-]+){1,3}", name):
            terms.add(('name', name.lower()))
    for match in REFERENCE.finditer(title + ' ' + body):
        token = match[1].lower()
        if any(c.isdigit() for c in token):
            terms.add(('id', token))
    if TRAVEL.search(title + ' ' + body):
        for place in PLACE.findall(title + ' ' + body):
            if place.lower() not in {'the','monday','tuesday','wednesday','thursday','friday','saturday','sunday','your'} | OWNER_WORDS:
                terms.add(('place', place.lower()))
    return terms


def bulk(candidate):
    """The inbox watch already marked this message a newsletter or a promotion (its own code verdict, read from its list)."""
    row = candidate.get('row') or {}
    return row.get('verdict') == 'filtered' and bool(re.search(r'newsletter|bulk|promotion', str(row.get('reason') or ''), re.I))


def matching(new, candidates, *, limit):
    """Only like entities on compatible accounts. A shared provider is a lead, never event identity.

    Newsletters would otherwise crowd out the few places a school, clinic or pharmacy notice competes for, so an older
    message the inbox list marked newsletter or promotion is not history unless it carries the same reservation, order
    or invoice number. Automated senders (school apps, pharmacies, banks) stay history.
    The mailbox wall below keeps mail from one of the owner's mailboxes from being tied to another: what it holds back
    is product or account mail that reaches several addresses, or mail on the owner's own domain, not an outside person
    or a booking."""
    anchors = [(r, entities(r)) for r in new if allowed(r)]
    ranked = []
    for candidate in candidates:
        if not allowed(candidate):
            continue
        found = entities(candidate)
        if not found and not candidate.get('matched_query'):
            continue
        best = 0
        reasons = []
        for anchor, terms in anchors:
            if anchor['ref'] == candidate['ref']:
                continue
            a = (anchor.get('mail') or {}).get('mailbox')
            b = (candidate.get('mail') or {}).get('mailbox')
            if a and b and a != b:
                continue
            common = terms & found
            if candidate.get('matched_query'):
                common.update((k,v) for k,v in terms if k in ('id','place','address') and v==candidate['matched_query'])
            if not common or bulk(candidate) and not any(kind == 'id' for kind,_ in common):
                continue
            score = sum({'id':9,'address':6,'name':5,'vendor':2,'place':3}[kind] for kind,_ in common)
            best = max(best,score)
            reasons.append(anchor['ref'])
        if reasons:
            ranked.append((best, candidate.get('at') or 0, candidate['ref'], candidate, reasons))
    return [(r, refs) for _,_,_,r,refs in sorted(ranked, key=lambda x:x[:3], reverse=True)[:limit]]


def history(world, con, life, new, known, now):
    """Only materialize full mail for a bounded set of related messages; bodies are read from files on demand."""
    if not new or not hasattr(world, 'history_mail'):
        return [], []
    since = now - 90*DAY
    candidates, notes = world.history_mail(since, now, new)
    candidates = [r for r in candidates if r['ref'] not in known]
    contacts = []
    try:
        contact_index = json.loads((Path(life)/'people'/'sources.json').read_text())
    except (OSError,ValueError):
        contact_index = {}
    for slug, person in contact_index.items():
        if not re.fullmatch(r'[\w.-]+',slug) or not isinstance(person,dict):
            continue
        page = Path(life)/'people'/(slug+'.md')
        if not page.is_file() or not since <= page.stat().st_mtime <= now+1:
            continue
        emails = person.get('emails') or []
        if isinstance(emails,dict):
            emails = list(emails)
        contacts.append({'ref':'people:'+slug,'title':person.get('name') or slug,'who':' '.join(str(a) for a in emails),
                         'at':page.stat().st_mtime,'path':str(page),'kind':'people'})
    traced = []
    for row in con.execute('SELECT key,state,payload,updated FROM opps WHERE updated BETWEEN ? AND ? AND json_type(payload,\'$.commitment\')=\'object\' ORDER BY updated DESC',(since,now)):
        data=json.loads(row['payload'])
        traced.append({'kind':'tracking','ref':'commit:'+row['key'],'title':data.get('title') or row['key'],'description':data.get('why') or '',
                       'summary':(data.get('packet') or {}).get('headline') or '', 'status':row['state'],'at':row['updated']})
    older = []
    for r,anchors in matching(new,candidates,limit=3):
        try:
            full=world.history_detail(r)
        except Exception as exc:
            full=None
            notes.append(f'An older related message could not be opened ({type(exc).__name__}).')
        if full and allowed(full):
            older.append((full, anchors))
        elif not full:
            notes.append('Only the subject of an older relevant message was available. Its contents were not used.')
    for r,anchors in matching(new,[r for r in contacts+traced if r['ref'] not in known],limit=6):
        if 'path' in r:
            r['text']=Path(r['path']).read_text()[:5000]
        else:
            r['text']=f"Status: {r['status']}. {r['description']} {r['summary']}"
        if allowed(r):
            older.append((r,anchors))
    return older,notes


# Exclude widely shared wording so an old conversation is only shown for a specific person, place, order or named event.
COMMON = set('about after afternoon asking before booking business change confirmed confirming currently customer date delivery details evening everyone family following from hello hours information know late mail meeting message morning need next notice order owner payment pickup please program reminder reply request reservation saturday schedule service shipment subject sunday thank thanks their there these this those today tomorrow updated update want week weekend with would your monday tuesday wednesday thursday friday november december january february march april june july august september october'.split()) | OWNER_WORDS

def topic_words(text):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9'-]{2,}",text.casefold()) if w not in COMMON]

def related_owner_messages(world, anchors, now):
    """Find the owner's own recent words on the same named subject. Older chat is context, never current authority."""
    topics=[]
    for a in anchors:
        if not allowed(a):
            continue
        title=re.sub(r'^Email from .*?:\s*','',str(a.get('title') or ''))
        body=str(a.get('text') or '')[:2200]
        names=re.findall(r"\b[A-Z][a-zA-Z'-]{2,}(?: +[A-Z][a-zA-Z'-]{2,}){1,3}\b",body)
        terms=set(topic_words(title+' '+' '.join(names)))
        ids={v for k,v in entities(a) if k=='id'}
        if len(terms)>=2 or ids:
            topics.append((a['ref'],terms,ids))
    if not topics:
        return [], []
    words=sorted({w for _,terms,ids in topics for w in terms|ids}, key=lambda w:(-len(w),w))[:40]
    since, until=now-14*DAY, now-DAY
    if hasattr(world,'history_owner'):
        messages, gap=world.history_owner(since,until,words)
    else:
        messages, gap=world.chat(since)
    if gap:
        return [], [gap]
    found=[]
    for message in messages:
        if message.get('who')!='user' or not since <= float(message['at']) < until or not allowed(message):
            continue
        text=message.get('text') or ''
        tokens=set(topic_words(text))
        links=[]
        strength=0
        for ref,terms,ids in topics:
            matched=terms & tokens
            if ids & tokens or len(matched)>=2:
                links.append(ref)
                strength=max(strength,len(matched)+(8 if ids & tokens else 0))
        if links:
            record={'kind':'chat','ref':'chat:'+str(message['id']),'title':'Earlier owner message on this subject',
                    'who':'user','at':message['at'],'text':text}
            found.append((strength,message['at'],record,links))
    return [(r,refs) for _,_,r,refs in sorted(found,key=lambda v:v[:2],reverse=True)[:4]],[]


def put_earlier(folder,ix,parts,record,anchors=()):
    """A compact pointer once in the reading packet, with original reference carried by its citable label."""
    r=record
    kind = r.get('kind','email')
    lab = ix.add('observations.md',kind,r['ref'],r['title'],r.get('fp'),r.get('thing'),extra={k:r[k] for k in ('mail','who','event_facts') if r.get(k) is not None})
    # Large, already-seen indexes belong in the file. Do not duplicate entries inside both model requests.
    ix.lines[-1] = f"- [{lab}] {str(r['title'])[:120]} — `earlier/{lab.partition(':')[2]}.md`"
    ix.source_text[lab]=r.get('text') or ''
    n=lab.partition(':')[2]
    directory=folder/'earlier'
    directory.mkdir(exist_ok=True)
    (directory/(n+'.md')).write_text('\n'.join([lab, r['ref'], str(r.get('when') or r.get('at') or ''), r['title'], '', r.get('text') or '', '',
                                                'Facts: '+json.dumps(r.get('facts') or {},ensure_ascii=False)]))
    title=' '.join(str(r['title']).split())
    who=str(r.get('who') or '')
    if who and title.startswith('Email from '+who+': '):
        title=title[len('Email from '+who+': '):]
    when=str(r.get('when') or '')
    if not when and r.get('at'):
        when=datetime.fromtimestamp(r['at'],timezone.utc).date().isoformat()
    names = getaddresses([who])
    person = (names[0][0] or names[0][1]) if names else who
    parts.append(f"[{lab}] {when[:16]} | {' '.join(person.split())[:38]} | {title[:80]}")
    if anchors:
        labels=[lab2 for lab2,row in ix.rows.items() if row['ref'] in anchors]
        parts[-1]+=' | related to '+', '.join(labels)


def archive_query(anchors, after, before):
    """A single entity-specific provider search covers dates before the local watcher started."""
    for anchor in anchors:
        box=(anchor.get('mail') or {}).get('mailbox')
        if box not in MAILBOXES or not allowed(anchor):
            continue
        terms=entities(anchor)
        # A common address or travel word alone does not justify opening a remote archive.
        precise=sorted((v for k,v in terms if k=='id'))
        if not precise:
            precise=sorted((v for k,v in terms if k=='place')) if TRAVEL.search(str(anchor.get('title') or '')) else []
        if not precise:
            addresses=sorted(v for k,v in terms if k=='address' and v in str(anchor.get('who','')).casefold())
            named=any(k=='name' for k,v in terms)
            substantive=re.search(r'(?i)\b(?:lunch|dinner|follow.?up|quote|agreement|reservation|booking|trip|appointment|contract|order|renewal|invoice|insurance)\b', str(anchor.get('title') or '')+' '+str(anchor.get('text') or '')[:220])
            if addresses and (named or any(k=='vendor' for k,v in terms)) and substantive and not re.search(r'(?i)no.?reply|notification|newsletter|automated|alert|marketing',str(anchor.get('who') or '')):
                precise=addresses[:1]
        if precise:
            atom=precise[0]
            left_out = ''.join(f' -"{t}"' for t in HISTORY_EXCLUDE)
            return box,(f'after:{int(after)} before:{int(before)} "{atom}" -in:spam -in:trash -in:drafts' + left_out)
    return None


def gmail_read(toolkit, account, op, args):
    """Execute only the two read actions needed for an address-checked archive search."""
    import subprocess
    if op not in ('GMAIL_GET_PROFILE','GMAIL_FETCH_EMAILS'):
        raise ValueError('unsupported history lookup')
    result=subprocess.run([toolkit.COMPOSIO,'execute',op,'--account',account,'-d',json.dumps(args)],
                          cwd=toolkit.HOME,env=toolkit._env(),capture_output=True,text=True,
                          stdin=subprocess.DEVNULL,timeout=20)
    if result.returncode:
        raise OSError('archive lookup unavailable')
    obj=json.loads(result.stdout)
    if obj.get('storedInFile') is True:
        p=Path(str(obj.get('outputFilePath') or '')).resolve()
        if p.suffix!='.json' or not p.is_relative_to(toolkit.COMPOSIO_TMP.resolve()) or not p.is_file():
            raise ValueError('unexpected archive response')
        try:
            obj=json.loads(p.read_text())
        finally:
            p.unlink(missing_ok=True)
    if obj.get('successful') is False:
        raise ValueError('archive lookup refused')
    return obj.get('data',obj)

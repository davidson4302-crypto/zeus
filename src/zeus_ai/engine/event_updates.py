"""Bind independent sources only with a shared, occurrence-specific identifier."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _hash(s):
    return hashlib.sha256(s.encode()).hexdigest()[:24]


def _instant(value, params=''):
    if not value:
        return ''
    try:
        if len(value)==8 and value.isdecimal():
            return datetime.strptime(value,'%Y%m%d').date().isoformat()
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if not parsed.tzinfo:
            zone=re.search(r'(?:^|;)TZID="?([^";]+)',params,re.I)
            if not zone:
                return ''
            parsed=parsed.replace(tzinfo=ZoneInfo(zone[1]))
        return parsed.astimezone(timezone.utc).isoformat(timespec='seconds')
    except (ValueError,ZoneInfoNotFoundError):
        return ''


def invites(obs):
    records=[]
    for a in obs.get('attachments') or []:
        if not a.get('read') or not (str(a.get('name','')).lower().endswith('.ics') or a.get('type')=='text/calendar'):
            continue
        data=re.sub(r'\r?\n[ \t]','',str(a.get('text') or ''))
        events=re.findall(r'(?:^|\n)BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT(?:\r?\n|$)',data,re.S|re.I)
        for block in events:
            fields={}
            for line in block.splitlines():
                head,sep,value=line.partition(':')
                if sep:
                    fields[head.partition(';')[0].upper()]=(value.strip(),head)
            uid=fields.get('UID',('',''))[0]
            start=_instant(*fields.get('DTSTART',('','')))
            if not uid or not start:
                continue
            series=_hash(uid)
            recurrence=_instant(*fields.get('RECURRENCE-ID',('',''))) or start
            aliases=['ical:'+series+':'+recurrence[:10]]
            status=fields.get('STATUS',('',''))[0].lower()
            if re.search(r'(?im)^METHOD:CANCEL\s*$',data):
                status='cancelled'
            records.append({'aliases':aliases,'start':start,'end':_instant(*fields.get('DTEND',('',''))),
                            'location':fields.get('LOCATION',('',''))[0], 'status':status})
    return records


def calendar(obs):
    start=_instant(obs.get('start','')) or str(obs.get('start') or '')
    series=str(obs.get('series_key') or '')
    if not re.fullmatch(r'[0-9a-f]{24}',series) or not start:
        return []
    aliases=['ical:'+series+':'+start[:10]]
    aliases.extend('instance:'+v for v in obs.get('instances') or [] if isinstance(v,str) and re.fullmatch(r'[0-9a-f]{24}',v))
    return [{'aliases':aliases,'start':start,'end':_instant(obs.get('end','')) or str(obs.get('end') or ''),
             'location':str(obs.get('location') or ''),'status':str(obs.get('status') or '').lower()}]


def normalized(text):
    return ' '.join(str(text or '').casefold().split())


def state(evidence):
    records=[r for e in evidence for r in e.get('event_facts') or [] if isinstance(r,dict) and r.get('aliases')]
    if not records:
        return {}
    # Multiple events in a calendar clash are not aliases for each other.
    anchor=set(records[0]['aliases'])
    merged={}
    for r in records:
        if not anchor.intersection(r['aliases']):
            return {}
        anchor.update(r['aliases'])
        for k in ('start','end','location','status'):
            val=r.get(k)
            if val and k in merged and normalized(merged[k])!=normalized(val):
                return {}  # conflicting contemporary sources need revalidation
            if val:
                merged[k]=val
    return dict(merged,aliases=sorted(anchor))


def difference(old,current):
    """Calendar minutes matter, even one. The ten-minute noise rule belongs to flight ETAs."""
    if not old or not current or not set(old['aliases']).intersection(current['aliases']):
        return None
    changes=[]
    for k in ('start','end','location','status'):
        if not current.get(k):
            continue
        if not old.get(k):
            if k=='location' or (k=='status' and normalized(current[k]) in ('cancelled','tentative')):
                changes.append(f"{k} is now {current[k]}")
            continue
        if normalized(old[k])!=normalized(current[k]):
            changes.append(f"{k} moved from {old[k]} to {current[k]}")
    return ', '.join(changes)


def grounded_change(prior_text,change,sources):
    if not isinstance(change,dict):
        return ''
    before,after=change.get('before'),change.get('after')
    old,new=normalized(before),normalized(after)
    if len(old)<3 or len(new)<3 or old==new or old not in normalized(prior_text):
        return ''
    source=sources.get(str(change.get('source'))) or ''
    if new not in normalized(source) or new in normalized(prior_text):
        return ''
    return f'{before} → {after}'


def related_mail(old, new):
    """A different thread may cancel an appointment. Match its mailbox, sender, and distinctive subject terms."""
    box=(old.get('mail') or {}).get('mailbox')
    ref=str(new.get('ref') or '').split(':',2)
    if len(ref)!=3 or ref[:2]!=['email',box]:
        return False
    if normalized(old.get('who'))!=normalized(new.get('who')):
        return False
    old_title=str(old.get('title') or '').partition(': ')[2]
    new_title=str(new.get('title') or '').partition(': ')[2]
    words=lambda s:set(re.findall(r'[\w-]{3,}',normalized(s))) - {'the','this','that','with','here','what','from','your','would','about','have','email','sent','reply','updated','new','please','message','will','for','and'}
    return len(words(old_title) & words(new_title+' '+str(new.get('text') or '')[:600])) >= 2


def business_alias(record):
    from research_history import REFERENCE, ADDRESS, GENERIC
    subject = str(record.get('title') or '').partition(': ')[2]
    boxes = ADDRESS.findall(str(record.get('who') or ''))
    if len(boxes)!=1:
        return []
    domain=boxes[0].split('@')[-1].lower()
    if domain in GENERIC:
        return []
    text=subject+' '+str(record.get('text') or '')[:450]
    refs={m[1].lower() for m in REFERENCE.finditer(text) if any(c.isdigit() for c in m[1])}
    if len(refs)!=1:
        return []
    account=(record.get('mail') or {}).get('mailbox')
    if not account:
        return []
    return [{'aliases':['record:'+_hash(account+'|'+domain+'|'+refs.pop())]}]


def new_measured_fact(statement, row_text, delivered):
    """Accept a measured change in a connected thread only if its wording and value occur together in evidence."""
    words = normalized(statement).split()
    source = normalized(row_text)
    previous = normalized(delivered)
    if not re.search(r'\b(?:changed|moved|rose|fell|up|down|reduced|increased|delayed|earlier|later)\b',normalized(statement)):
        return ''
    for i in range(len(words)-2):
        phrase=' '.join(words[i:i+3]).strip('.,?!')
        if re.search(r'\d',phrase) and phrase in source and phrase not in previous:
            return phrase
    return ''

def stored_aliases(evidence):
    """Earlier releases retained source references and sender/title metadata, even before aliases existed."""
    aliases=set()
    for row in evidence:
        ref=str(row.get('ref') or '')
        calendar_id=re.fullmatch(r'calendar:([0-9a-f]{24}):(\d{4}-\d{2}-\d{2})',ref)
        if calendar_id:
            aliases.add('alias:ical:'+calendar_id[1]+':'+calendar_id[2])
        elif row.get('kind')=='email':
            for event in business_alias(row):
                aliases.update('alias:'+a for a in event['aliases'])
    return aliases

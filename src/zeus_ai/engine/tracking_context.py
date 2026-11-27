"""Project active commitments and short-lived interests into the hourly research packet."""
import json
from datetime import datetime

DAY = 86400
INTEREST_DAYS = 14
OPEN = ('pending', 'executing', 'ready', 'surfaced', 'needs_attention')


def timestamp(value):
    if isinstance(value, (float, int)):
        return value
    if not value:
        return 0
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError):
        return 0


def progress_dates(con):
    """An accepted owner change or new verified evidence advances activity. A scheduled check alone cannot."""
    result = {}
    for row in con.execute(
        "SELECT commitment_id,at,actor,kind,before,after FROM commit_events "
        "WHERE actor IN ('owner','chat') OR kind IN ('execution','verification') ORDER BY seq"
    ):
        cid, at, actor, kind, before, after = row
        if actor not in ('owner', 'chat'):
            old = json.loads(before or '{}').get('commitment', {})
            new = json.loads(after or '{}').get('commitment', {})
            if not new.get('evidence_refs') or old.get('evidence_refs') == new['evidence_refs']:
                continue
        result[cid] = max(result.get(cid, 0), timestamp(at))
    return result


def commitments(con):
    events = progress_dates(con)
    rows = con.execute(
        "SELECT key,payload,state,first_seen,surface_at FROM opps WHERE state IN (?,?,?,?,?) "
        "AND json_type(payload,'$.commitment')='object' ORDER BY first_seen DESC", OPEN)
    for row in rows:
        item = json.loads(row['payload'])
        c = item['commitment']
        if c.get('phase') in ('stopped', 'done'):
            continue
        key = row['key']
        activity = max(timestamp(row['first_seen']), events.get(c.get('commitment_id'), 0))
        due = (c.get('next_action') or {}).get('due') or row['surface_at']
        yield {'key': key, 'ref': 'commit:' + key, 'thing': 'commit:' + key,
               'title': item.get('title') or key, 'status': c.get('phase') or row['state'],
               'activity_at': activity, 'updated': activity, 'due': timestamp(due), 'deadline': timestamp(item.get('deadline')),
               'description': item.get('why') or '', 'summary': (item.get('packet') or {}).get('headline') or
               (c.get('next_action') or {}).get('do') or '',
               'fp': str(c.get('revision', '')) + ':' + row['state']}


def classify(row, now, checkpoint):
    due = [t for t in (row.get('deadline'), row.get('due')) if t]
    if any(checkpoint < t <= now for t in due):
        return 'due now'
    if any(now < t <= now + DAY for t in due):
        return 'due soon'
    if row.get('updated', 0) >= now - 3 * DAY:
        return 'recent progress'
    return 'quiet'


def interest_schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS research_interests (source_ref TEXT PRIMARY KEY, '
                'quote TEXT NOT NULL, mentioned REAL NOT NULL, expires REAL NOT NULL, closed_by TEXT)')


def save_interests(con, proposals, chat_sources, now):
    """Only a quotation from a dated owner message may add or settle a passing interest.
    -> {'saved': [chat refs], 'settled': [chat refs], 'rejected': [why, ...]}

    Every refusal carries a reason, so "research proposed none" and "code refused them all" never look the same in the
    run's record. The rule: the owner's own message, dated inside the last 14 days, the owner's exact words."""
    interest_schema(con)
    out = {'saved': [], 'settled': [], 'rejected': []}
    if not isinstance(proposals, list):
        return out
    for p in proposals:
        if not isinstance(p, dict):
            out['rejected'].append('not an interest entry')
            continue
        msg = chat_sources.get(str(p.get('source') or ''))
        quote = str(p.get('quote') or '').strip()
        why = ('the source is not a chat message listed in this run' if not msg else 'the message is not the owner\'s own' if msg['who'] != 'user'
               else 'the quote must be 4 to 300 characters' if not 4 <= len(quote) <= 300 else 'the quote is not in the owner\'s message' if quote not in msg['text']
               else 'the message is not from the last 14 days' if not now - INTEREST_DAYS * DAY < msg['at'] <= now else '')
        if why:
            out['rejected'].append(why)
            continue
        if p.get('settles'):
            # The prior interest is named by its source label in tracking.md, or by the chat reference printed beside it.
            prior = str(p['settles'])
            prior = (chat_sources.get(prior) or {}).get('ref', prior)
            done = con.execute('UPDATE research_interests SET closed_by=? WHERE source_ref=? AND closed_by IS NULL '
                               'AND mentioned<? AND expires>?', (msg['ref'], prior, msg['at'], msg['at'])).rowcount
            out['settled' if done else 'rejected'].append(prior if done else 'no open interest matches what it settles')
        elif con.execute('INSERT OR IGNORE INTO research_interests VALUES (?,?,?,?,NULL)',
                         (msg['ref'], quote, msg['at'], msg['at'] + INTEREST_DAYS * DAY)).rowcount:
            out['saved'].append(msg['ref'])
    return out


def interests(con, now):
    interest_schema(con)
    return con.execute('SELECT source_ref,quote,mentioned,expires FROM research_interests WHERE closed_by IS NULL '
                       'AND mentioned<=? AND expires>? ORDER BY mentioned DESC', (now, now)).fetchall()


def open_threads(con):
    """Old open conversations stay discoverable through their original mailbox/thread identifiers."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='loops'").fetchone():
        return
    for row in con.execute("SELECT id,mailbox,thread_id,subject,what,opened,due FROM loops WHERE state='open'"):
        key, mailbox, thread, subject, what, opened, due = row
        yield {'ref': 'loop:' + key, 'thing': 'thread:' + mailbox + ':' + thread,
               'key': key, 'title': subject or what, 'summary': what, 'status': 'open thread',
               'updated': timestamp(opened), 'due': timestamp(due), 'description': 'Recheck the original mail thread before raising it.'}

def hourly_receipts(con, life, now_dt):
    """Specialty routines produce local inputs for the one hourly finder, behind their original switches."""
    # The one routine here: the store refill offer (orderwatch.refill_offers: the hour's wait and the
    # "did the owner already order" check, held by code).
    import orderwatch
    result = {}
    for name, source, prepare in (('walmart_refill', orderwatch, orderwatch.refill_offers),):
        if source.on(life):
            try:
                prepared = prepare(con, life, now_dt)
            except Exception as error:
                prepared = {'errors': [f'{type(error).__name__}: {str(error)[:120]}']}
            if prepared:
                result[name] = prepared
    return result

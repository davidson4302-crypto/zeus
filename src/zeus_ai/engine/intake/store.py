"""Shared life-event store. One row per real-world thing, updated as new mail about it arrives.

Dedupe: same key (carrier+confirmation, invite uid, pharmacy+rx) -> one event, merged, with a history of sources.
Reconcile: two different flight confirmations whose trips overlap -> both flagged `possible_duplicate_booking`.
A cancelled flight or invite stays in the store with status cancelled (it moots any job built on it).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(key TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT, payload TEXT NOT NULL,
  first_seen REAL NOT NULL, updated REAL NOT NULL, flags TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS sources(key TEXT NOT NULL, message_id TEXT NOT NULL, mailbox TEXT, at REAL NOT NULL,
  change TEXT, PRIMARY KEY(key, message_id));
CREATE TABLE IF NOT EXISTS processed(mailbox TEXT NOT NULL, message_id TEXT NOT NULL, at REAL NOT NULL, class TEXT,
  event_type TEXT, key TEXT, why TEXT, PRIMARY KEY(mailbox, message_id));
CREATE TABLE IF NOT EXISTS unkeyed(message_id TEXT PRIMARY KEY, type TEXT, payload TEXT, at REAL);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(SCHEMA)

    def db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return closing_commit(con)

    # ------------------------------------------------------------------ processed ledger (no double work)
    def seen(self, mailbox, message_id) -> bool:
        with self.db() as db:
            return db.execute('SELECT 1 FROM processed WHERE mailbox=? AND message_id=?', (mailbox, message_id)).fetchone() is not None

    def mark(self, mailbox, message_id, result):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO processed VALUES(?,?,?,?,?,?,?)',
                       (mailbox, message_id, time.time(), result.get('class'), result.get('event_type'), result.get('key'), result.get('why')))

    # ------------------------------------------------------------------ events
    def upsert(self, key, ev, message_id='?', mailbox=None) -> str:
        """-> 'new' | 'updated' | 'same'."""
        if not key:
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO unkeyed VALUES(?,?,?,?)', (message_id, ev.get('type'), json.dumps(ev), time.time()))
            return 'unkeyed'
        now = time.time()
        with self.db() as db:
            row = db.execute('SELECT * FROM events WHERE key=?', (key,)).fetchone()
            if row is None:
                db.execute('INSERT INTO events(key,type,status,payload,first_seen,updated) VALUES(?,?,?,?,?,?)',
                           (key, ev.get('type'), ev.get('status') or ev.get('method'), json.dumps(ev), now, now))
                change = 'new'
            else:
                old = json.loads(row['payload'])
                merged = merge(old, ev)
                change = 'same' if merged == old else 'updated'
                if change == 'updated':
                    db.execute('UPDATE events SET payload=?, status=?, updated=? WHERE key=?',
                               (json.dumps(merged), merged.get('status') or merged.get('method'), now, key))
            db.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?)', (key, message_id, mailbox, now, change))
        if ev.get('type') == 'flight':
            self.reconcile_flights()
        return change

    def all(self, type_=None):
        with self.db() as db:
            q = 'SELECT * FROM events' + (' WHERE type=?' if type_ else '') + ' ORDER BY first_seen'
            rows = db.execute(q, (type_,) if type_ else ()).fetchall()
        return [{**json.loads(r['payload']), '_key': r['key'], '_flags': json.loads(r['flags']), '_status': r['status']} for r in rows]

    def reconcile_flights(self):
        flights = [f for f in self.all('flight') if f.get('status') != 'cancelled']
        spans = {}
        for f in flights:
            times = [s.get('depart_local') for s in f.get('segments') or [] if s.get('depart_local')] + \
                    [s.get('arrive_local') for s in f.get('segments') or [] if s.get('arrive_local')]
            if times:
                spans[f['_key']] = (min(times)[:10], max(times)[:10])
        overlap = set()
        keys = list(spans)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if spans[a][0] <= spans[b][1] and spans[b][0] <= spans[a][1]:
                    overlap |= {a, b}
        with self.db() as db:
            for f in self.all('flight'):
                flags = [x for x in f['_flags'] if x != 'possible_duplicate_booking']
                if f['_key'] in overlap:
                    flags.append('possible_duplicate_booking')
                db.execute('UPDATE events SET flags=? WHERE key=?', (json.dumps(flags), f['_key']))


def merge(old: dict, new: dict) -> dict:
    """Newer mail wins for fields it actually has; a cancellation always wins; segments replace when the new mail has them."""
    out = dict(old)
    for k, v in new.items():
        if k in ('source', 'segments'):
            continue
        if v in (None, [], ''):
            continue
        out[k] = v
    if new.get('segments'):
        out['segments'] = merge_segments(old.get('segments') or [], new['segments'])
        deps = sorted(s['depart_local'] for s in out['segments'] if s.get('depart_local'))
        if deps and new.get('type') == 'flight':
            out['checkin_opens'] = min((s['checkin_opens'] for s in out['segments'] if s.get('checkin_opens')), default=out.get('checkin_opens'))
    if old.get('status') == 'changed' and new.get('status') == 'booked':
        out['status'] = 'changed'  # a later reminder doesn't undo the change
    if old.get('status') == 'cancelled' and new.get('status') not in ('cancelled', None):
        out['status'] = new['status']  # rebooked under the same code
    out['source'] = new.get('source') or old.get('source')
    return out


class closing_commit:
    def __init__(self, con):
        self.con = con

    def __enter__(self):
        return self.con

    def __exit__(self, et, e, tb):
        try:
            if et is None:
                self.con.commit()
            else:
                self.con.rollback()
        finally:
            self.con.close()


def merge_segments(old, new):
    """Update legs that match by flight number (or route), keep legs the new mail doesn't mention."""
    out = [dict(x) for x in old]
    for n in new:
        hit = next((o for o in out if n.get('flight_no') and o.get('flight_no') == n.get('flight_no')), None) or \
            next((o for o in out if (o.get('from'), o.get('to')) == (n.get('from'), n.get('to'))), None)
        if hit:
            hit.update({k: v for k, v in n.items() if v is not None})
        else:
            out.append(dict(n))
    out.sort(key=lambda s: s.get('depart_local') or '')
    return out

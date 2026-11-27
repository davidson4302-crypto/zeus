"""Make sourced renewal decisions available to Zeus's existing research and delivery jobs."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

NOTICE_FIELDS = {'id', 'vendor', 'excerpt', 'amount', 'currency', 'charge_on', 'renewal_on',
                 'cancel_by', 'notice', 'timezone', 'terms', 'field_quotes'}
RENEWAL_WORDS = re.compile(r'\b(?:renew\w*|subscription|membership|auto.?bill\w*|cancell?ation)\b', re.I)
CHARGE_WORDS = re.compile(r'\b(?:renew\w*|subscription|membership|auto.?bill\w*|charge[d]?|bill(?:ed|ing)?)\b', re.I)


def store(life):
    return Path(life) / 'renewals' / 'records.sqlite3'


@lru_cache(maxsize=4)
def skill(life):
    path = Path(life).parent.parent / 'skills/productivity/zeus-renewals/renewals.py'
    spec = importlib.util.spec_from_file_location('zeus_renewals_skill', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(life):
    if not store(life).exists():
        return []
    return skill(Path(life)).execute(store(life), {'op':'list'})['records']


def normalized(s):
    return ' '.join(str(s or '').split()).casefold()


def identity(record):
    return 'renewal:' + record['id']


def fingerprint(record):
    evidence = [record['revision'], record['status'], record.get('decision'), record.get('payment')]
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def public_terms(record):
    # The researcher needs terms and evidence references. Keep private receipt texts in their original record.
    return {k: record.get(k) for k in ('id','vendor','status','charge_on','renewal_on','cancel_by',
            'due_at','notice','amount','currency','terms','conflicts','issues','source_ids')}


def add_records(records, ix, parts, new_ids, con):
    if not records:
        return
    parts.append('# Subscription decisions carried forward')
    for r in records:
        # Closed cycles remain readable by exact ID. Only open decisions belong in a research turn.
        if r['status'] == 'decided':
            continue
        ref, fp = identity(r), fingerprint(r)
        title = f"{r['vendor']} renewal decision"
        label = ix.add('tracking.md', 'renewal', ref, title, fp, ref)
        for source in r['source_ids']:
            ix.renewal_links.setdefault(source, {})[r['id']] = (ref, fp, title, r['vendor'])
        prior = con.execute('SELECT fp FROM seen WHERE id=?', (ref,)).fetchone()
        if not prior or prior['fp'] != fp:
            new_ids.append((ref,fp))
        parts.extend([f'## [{label}] {title}', json.dumps(public_terms(r), ensure_ascii=False), ''])


def collect(life, proposals, ix):
    saved, rejected = [], []
    if not isinstance(proposals, list):
        return saved, rejected
    for entry in proposals:
        try:
            if not isinstance(entry, dict):
                raise ValueError('expected a notice with a source label')
            label = str(entry.get('source') or '').strip('[]')
            row = ix.rows.get(label, {})
            source = ix.renewal_texts.get(label)
            if not source or row.get('kind') != 'email':
                raise ValueError('the notice must come from a newly read email')
            req = {k:entry[k] for k in NOTICE_FIELDS if k in entry}
            quote, vendor = normalized(req.get('excerpt')), normalized(req.get('vendor'))
            if not quote or not any(quote in normalized(part) for part in source):
                raise ValueError('the excerpt did not match the cited source')
            if not vendor or vendor not in normalized(row.get('title')+' '+' '.join(source)) or not CHARGE_WORDS.search(quote):
                raise ValueError('the cited source did not establish a renewal from that provider')
            # A model may quote just the billing sentence while taking the notice period from the next
            # sentence. Use the containing provider paragraph, never a different offer or attachment.
            evidence = req['excerpt']
            for part in source:
                for paragraph in re.split(r'\n\s*\n', part):
                    if quote in normalized(paragraph) and len(paragraph) + len(row['title']) < 4900:
                        evidence = paragraph.strip()
                        break
                else:
                    continue
                break
            req.update(op='notice', source_id=row['ref'], excerpt=row['title']+'\n'+evidence)
            # New mail alone cannot resolve contrary terms. Explicit corrections use the sourced skill.
            # Source references survive a crash between saving terms and the research checkpoint.
            # A retried model reading of the identical source must not start a second subscription cycle.
            matches = [existing for existing in read(life) if normalized(existing['vendor']) == vendor
                       and any(src['source_id'] == row['ref'] and normalized(src['excerpt']) == normalized(req['excerpt'])
                               for src in existing['sources'])]
            if len(matches) == 1:
                r = matches[0]
            elif matches:
                raise ValueError('Multiple renewal records refer to this exact notice; reconcile their identities.')
            else:
                try:
                    r = skill(Path(life)).execute(store(life), req)['record']
                except ValueError as exc:
                    # A model can add a currency code the notice never wrote (e.g. USD for "$14.99"). The skill then
                    # refuses the whole notice, and a mail is shown to research once, so the renewal would be lost with
                    # no record. The skill's own remedy ("omit its code") is applied here: dates, amount and terms are
                    # kept, the guessed code is not.
                    if 'currency' not in req or 'omit its code' not in str(exc):
                        raise
                    req.pop('currency')
                    r = skill(Path(life)).execute(store(life), req)['record']
            ix.renewal_links.setdefault(row['ref'], {})[r['id']] = (identity(r), fingerprint(r), f"{r['vendor']} renewal decision", r['vendor'])
            saved.append({'id':r['id'],'source':row['ref'],'status':r['status'],'due_at':r['due_at']})
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
            rejected.append(str(exc)[:230])
    return saved, rejected


def link_evidence(ix, evidence, title, action):
    if not RENEWAL_WORDS.search(title+' '+action):
        return
    for original in list(evidence):
        links = ix.renewal_links.get(original.get('ref'), {})
        for ref, fp, name, vendor in links.values():
            if len(links) > 1 and normalized(vendor) not in normalized(title+' '+action):
                continue
            if all(e.get('ref') != ref for e in evidence):
                evidence.append({'kind':'renewal','ref':ref,'thing':ref,'fp':fp,'title':name})


def check(life, evidence):
    """Close a stale proposal or hold unreadable terms before dispatch."""
    for e in evidence:
        if e.get('kind') != 'renewal':
            continue
        try:
            r = skill(Path(life)).execute(store(life), {'op':'get', 'id':e['ref'].removeprefix('renewal:')})['record']
        except (OSError, ValueError, KeyError, sqlite3.Error):
            return 'unreadable', 'The saved renewal terms could not be read.'
        if r['status'] == 'decided' or fingerprint(r) != e.get('fp'):
            return 'changed', 'The owner decided or the saved terms changed.'
    return 'same', ''

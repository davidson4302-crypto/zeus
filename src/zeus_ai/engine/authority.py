"""authority: does this step have the owner's yes, and where does that yes come from?

One place answers "is this approved?". The owner's yes is checked where it is given (onesend.approve reads their real
message), a standing OK where it is used (policy.py) and the team rule where a reply is sent (onesend._team_send);
this module writes down WHICH of those a step went out under. A line in memory or in a message saying "the owner
approved this" is never an approval.

resolve(con, life, action) answers for one step that needs the owner's yes (a message to someone outside, a
purchase, a calendar change, a delete). There are exactly three sources, and code checks each against its own record:

  owner_message   the owner's own message, bound to THIS step. The binding is the one onesend.approve makes: it reads
                  their real message from the chat store, reads it alone (hisreply.py), ties it to this one prepared
                  step and writes that on the step's record. resolve finds that record and reads the message again.
  standing_grant  a saved standing OK (grants.json, written by `life grant` against the owner's real words) whose kind
                  of item covers this step's item. A grant for another kind of item covers nothing here.
  team_rule       a send whose every recipient is on the owner's configured team (settings.TEAM) is never held.
                  Team members are internal, never approvers: work a teammate relays carries no authority of its
                  own. The team rule never covers spending, a delete, a calendar change or a sign-in: those keep their
                  own rules.

What is never a source:
- A sentence, anywhere, saying the owner approved ("the owner approved this", "a teammate says they said yes"). It
  is a relayed claim: it is written down as unverified (claims.py, when its switch is on) and authorizes nothing.
- A message from a teammate or another agent, whatever it says.
- A flag a model put on the step ("approved": true, an authority_ref it wrote itself). Ignored, and named in the answer.
- How sure anybody is.

Purchases: the purchase lock (its own plugin) checks the owner's yes to that store and total at the checkout page.
Nothing here reads, writes or replaces that. An answer for a purchase says so in `also`.

Recording: when a step's record moves to "approved" (actions.move), approved() writes the resolved source on that
step's record and on its job (the commitment's authority_ref). The older `life approve` road (no step record) gets
the same through legacy_yes(). Recording never stops or changes a flow: it only writes down what resolve found,
including "none" when it found nothing.

Always on for a real life folder. Sandboxes may point at a pretend team list with LIFE_TEAM_FILE.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import actions as A
import settings

SWITCH = 'authority-on'
LIVE_HOME = Path(settings.get('paths.life_home', str(settings.PROFILE_DIR / 'workspace' / 'life-engine')))   # the real life folder (never the LIFE_HOME env of a sandbox)
KINDS = ('owner_message', 'standing_grant', 'team_rule', 'none')          # the same four commit.py knows
SENDS = ('email_reply', 'send', 'email', 'message', 'text', 'post', 'reply')
SPENDS = ('purchase', 'order', 'booking', 'payment')
OWN_RULES = SPENDS + ('delete', 'calendar_change', 'calendar', 'sign_in')   # the team rule never reaches these
FREE = ('read', 'prepare', 'draft', 'research', 'look_up', 'tell_him', 'note')   # nothing in these needs the owner's yes
FLAGS = ('approved', 'approval', 'authorized', 'authorised', 'authority', 'authority_ref', 'his_yes', 'confirmed', 'approved_by', 'granted', 'ok')
RECENT = 15 * 60            # the older road: the owner's yes must be this fresh (onesend.RECENT)
PURCHASE_NOTE = "the purchase lock still checks the owner's yes to that store and total at checkout; this answer never replaces it"


def _live(life) -> bool:
    try:
        return Path(life).resolve() == LIVE_HOME.resolve()
    except OSError:
        return False


def on(life) -> bool:
    try:
        if life is None:
            return False
        return True   # always on for a real life folder
    except (OSError, TypeError):
        return False


def on_for(con) -> bool:
    try:
        return on(A.home_of(con))
    except Exception:
        return False


def _has_table(con, name: str) -> bool:
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def chat_store(life):
    """The owner's message store: the one named for a sandbox, the real one for the live folder, None otherwise."""
    named = os.environ.get('LIFE_CHAT_DB')
    if named:
        return named
    if _live(life):
        import chats as C
        return C.STORE
    return None


def _his_message(life, message_id, about: float | None = None):
    """-> (the owner's real message with this id, '') | (None, 'missing') | (None, 'unreadable'). Read-only."""
    store = chat_store(life)
    if not store or message_id is None:
        return None, 'unreadable'
    try:
        import chats as C
        lo, hi = (float(about) - 3600, float(about) + 3600) if about else (0.0, None)
        msgs = [m for m in C.messages(lo, hi, path=store) if m['who'] == 'owner']
        if about and not any(str(m['id']) == str(message_id) for m in msgs):
            msgs = [m for m in C.messages(0.0, None, path=store) if m['who'] == 'owner']
    except Exception:
        return None, 'unreadable'
    hit = next((m for m in msgs if str(m['id']) == str(message_id)), None)
    return (hit, '') if hit else (None, 'missing')


def team(life):
    """The owner's team (settings.TEAM) -> [{'name', 'emails'}]. A sandbox may name a pretend list with LIFE_TEAM_FILE
    (a JSON list of members, or {"members": [...]}); the live folder always reads settings. Empty means nobody is team."""
    members = settings.TEAM
    named = os.environ.get('LIFE_TEAM_FILE')
    if named and not _live(life):
        try:
            got = json.loads(Path(named).read_text(encoding='utf-8'))
            members = got.get('members') or got.get('agents') or [] if isinstance(got, dict) else got
        except Exception:
            return None
    return [{'name': str(a.get('name') or ''), 'emails': [str(e) for e in (a.get('emails') or [])]}
            for a in members or [] if isinstance(a, dict) and a.get('name')]


def _team_only(life, to, cc) -> bool:
    """Is every recipient on the owner's team? The same check onesend uses for the team rule."""
    try:
        import onesend as OS
        return bool(OS.team_only({'to': list(to or []), 'cc': list(cc or [])}, life))
    except Exception:
        return False


def relay(life, relayed) -> dict:
    """Work said to come from a teammate. Team members are internal, never approvers, so a relay never carries the
    owner's authority. -> {'ok': False, 'why'}. Kept so callers get one plain answer."""
    return {'ok': False, 'why': "a teammate is never an approver, so relayed work does not carry the owner's yes"}


def grant_for(life, item_key: str):
    """The standing OK that covers this item's kind -> (family, entry) or (None, None). The same match policy.py uses."""
    if not item_key:
        return None, None
    try:
        g = json.loads((Path(life) / 'grants.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, None
    if not isinstance(g, dict):
        return None, None
    import policy as P
    fam = P.family(str(item_key))
    for f, v in g.items():
        if isinstance(v, dict) and (fam == f or fam.startswith(f + ':')):
            return f, v
    return None, None


def _claims_in(act: dict) -> list:
    """Sentences on the step that say somebody approved or permitted it -> [{'text', 'speaker', 'source_ref'}]."""
    out = []
    try:
        K = __import__('claims')
    except Exception:
        return out
    pool = list(act.get('claims') or []) if isinstance(act.get('claims'), (list, tuple)) else []
    pool += [act.get(k) for k in ('note', 'why', 'reason', 'said') if isinstance(act.get(k), str)]
    for c in pool:
        text = c.get('text') if isinstance(c, dict) else c
        if isinstance(text, str) and any(k in ('approval', 'permission') for k in K.asserts(text)):
            out.append({'text': ' '.join(text.split())[:600], 'speaker': str((c.get('speaker') if isinstance(c, dict) else '') or act.get('from') or 'unknown'),
                        'source_ref': str((c.get('source_ref') if isinstance(c, dict) else '') or '')})
    return out


def _keep_relayed(life, found: list) -> list:
    """Each such sentence is written down as a relayed claim (when claims.py is on) and handed back marked unverified."""
    kept = []
    for c in found:
        entry = {'text': c['text'], 'speaker': c['speaker'], 'verified': 'relayed', 'authorizes': False}
        try:
            K = __import__('claims')
            if K.on(life):          # claims.py decides how it is kept: their only if a real message of theirs holds the words, else relayed
                row = K.record(life, c['text'], speaker=c['speaker'], source_ref=c['source_ref'])
                entry.update(speaker=row['speaker'], verified=row['verified'], recorded=row['id'])
        except Exception:
            pass
        kept.append(entry)
    return kept


def needs_yes(kind: str) -> bool:
    """Unknown kinds need their yes: a step is free only when it is one of the kinds that never leave their own side."""
    return str(kind or '') not in FREE


def resolve(con, life, action, now=None) -> dict:
    """Does this step have their yes? `action`: a step record (actions.py) or its id, or a plain description
    {'kind', 'item', 'to', 'cc', 'relayed': {'by', 'message_id'}, 'claims': [...]}.
    -> {'ok': bool, 'needs_yes': bool, 'kind': owner_message | standing_grant | team_rule | none, 'ref': where it is,
        'authority_ref': {'kind', 'ref'} (the shape a commitment keeps), 'why': one plain line, 'proof': how it was
        checked, 'ignored': flags that were on the step and count for nothing, 'relayed': claims of approval found on
        it (kept as unverified), 'also': what else still checks it}
    A record that is in the ledger is read from the ledger: what a caller says about its history is never used."""
    now = float(now or time.time())
    act = dict(action) if isinstance(action, dict) else {'id': str(action)}
    row = None
    if act.get('id') and con is not None and _has_table(con, 'actions'):
        row = A.get(con, str(act['id']))
    details = (row['details'] if row else act.get('details')) or {}
    details = details if isinstance(details, dict) else {}
    kind = str((row or act).get('kind') or '')
    item = str((row or act).get('item') or '')
    out = {'ok': False, 'needs_yes': needs_yes(kind), 'kind': 'none', 'ref': '', 'why': '', 'proof': '', 'also': '',
           'ignored': sorted({k for k in FLAGS if k in act or (row is None and k in details)}), 'relayed': _keep_relayed(life, _claims_in(act))}
    if kind in SPENDS:
        out['also'] = PURCHASE_NOTE

    def done(ok, source='none', ref='', why='', proof=''):
        out.update(ok=bool(ok), kind=source if ok else 'none', ref=ref if ok else '', why=why, proof=proof)
        out['authority_ref'] = {'kind': out['kind'], 'ref': out['ref'][:200]}
        return out

    if not out['needs_yes']:
        return done(True, 'none', '', 'nothing in this step needs their yes')

    reasons = []
    # (a) their own message, bound to this step by the approve road
    if row is not None:
        steps = row.get('history') or []
        yes = next((h for h in reversed(steps) if h.get('state') == 'approved' and h.get('message_id') is not None), None)
        read = [h for h in steps if h.get('note') == 'their reply was read' and h.get('reading') == 'yes']
        if yes is not None:
            if not any(str(h.get('message_id')) == str(yes['message_id']) for h in read):
                reasons.append('a yes is written on this step, but no reading of theirs message ties it to this step')
            else:
                msg, trouble = _his_message(life, yes['message_id'], yes.get('said_at'))
                ref = f"chat:{yes['message_id']}"
                if msg is not None:
                    words = ' '.join(str(yes.get('his_words') or '').lower().split())
                    if words and words[:120] not in ' '.join(str(msg['text']).lower().split()):
                        reasons.append('the message this yes points at does not say the words that were recorded')
                    else:
                        return done(True, 'owner_message', ref, 'their own message, read alone and tied to this exact step', 'their message was read again just now')
                elif trouble == 'missing':
                    reasons.append('the message this yes points at is not among their real messages')
                else:
                    return done(True, 'owner_message', ref, 'their own message, read alone and tied to this exact step when the owner answered',
                                'it is on record from when the owner answered; their messages could not be read again just now')
    # (b) a standing OK whose kind of item covers this one
    fam, entry = grant_for(life, item)
    if entry is not None:
        sourced = bool(entry.get('his_words')) and entry.get('message_id') is not None
        return done(True, 'standing_grant', f'grant:{fam}' + (f":chat:{entry['message_id']}" if sourced else ''),
                    'their standing OK for this kind of item' + ('' if sourced else ' (on file with no message of theirs recorded beside it)'),
                    'their words and their message are on the standing OK' if sourced else 'the standing OK is on file; no message of theirs is recorded with it')
    if item and (Path(life) / 'grants.json').exists():
        reasons.append('no standing OK covers this kind of item')
    # (c) the team rule
    if kind not in OWN_RULES:
        to, cc = (details.get('to') if row else act.get('to', details.get('to'))), (details.get('cc') if row else act.get('cc', details.get('cc')))
        if (kind in SENDS or to) and _team_only(life, to, cc):
            return done(True, 'team_rule', 'team:every-recipient', 'every recipient is on the team, so no yes is needed', 'each address was matched against the team list')
        if row is None and act.get('relayed') is not None:
            reasons.append(relay(life, act.get('relayed'))['why'])
    elif act.get('relayed') is not None or (details.get('to') or act.get('to')):
        reasons.append('the team rule does not cover spending, deletes, calendar changes or sign-ins')
    if out['relayed']:
        reasons.append('a sentence saying it was approved is only a claim, and is kept as unverified')
    if out['ignored']:
        reasons.append('an "approved" mark on the step itself counts for nothing')
    return done(False, why='No yes of theirs is on record for this step' + (': ' + '; '.join(reasons) if reasons else '') + '.')


# ---- writing down what was found --------------------------------------------------------------------------------------------

def _on_job(con, life, key: str, ref: dict, op_id: str, actor: str = 'chat') -> bool:
    """The resolved source onto the item's job (commitment), as bookkeeping: the version stays. Only when commitments
    are on and this item has an open one. Never raises."""
    try:
        import commit as CM
        if not CM.on(life) or not _has_table(con, 'commitments'):
            return False
        got = CM.get(con, key)
        if got is None or not got.get('open'):
            return False
        clean = CM._authority(ref)

        def change(obj, opp):
            obj['authority_ref'] = clean
        CM._book(con, got['commitment_id'], 'authority', op_id, actor, None, change, revision=got['revision'])
        return True
    except Exception:
        return False


def approved(con, rec_id) -> None:
    """Hook at the end of actions.move, when a step's record has just become "approved". With the switch: where that
    yes comes from is resolved and written on the approved step of the record and on the item's job. Without the
    switch: nothing. Never raises, never changes what the caller does next."""
    try:
        if not on_for(con):
            return
        life = A.home_of(con)
        rec = A.get(con, rec_id)
        if rec is None or rec['state'] != 'approved':
            return
        got = resolve(con, life, rec)
        history = rec['history']
        at = max(i for i, h in enumerate(history) if h.get('state') == 'approved')
        history[at]['authority_ref'] = got['authority_ref'] | {'ok': got['ok'], 'why': got['why'][:240], 'proof': got['proof'][:160]}
        con.execute("UPDATE actions SET history=? WHERE id=? AND state='approved' AND updated=?", (json.dumps(history, default=str), rec_id, rec['updated']))
        con.commit()
        if got['ok'] and got['kind'] != 'none':
            _on_job(con, life, rec['item'], got['authority_ref'], f"authority:{rec_id}:{at}", 'chat' if got['kind'] == 'owner_message' else 'tick')
    except Exception:
        pass


def legacy_yes(con, life, key: str, words: str) -> None:
    """Hook in lifecli's older `approve` road (no step record), after their yes was saved on the item. With the switch:
    the yes is checked against their real messages (a recent message of theirs that holds these words and reads as a plain
    yes, not already used for another item) and what was found is written on the item's saved yes and on its job.
    Without the switch: nothing. Never raises, and never undoes the yes the older road recorded."""
    try:
        if not on(life):
            return
        row = con.execute('SELECT payload FROM opps WHERE key=?', (key,)).fetchone()
        p = json.loads(tuple(row)[0]) if row else None
        if not isinstance(p, dict) or not isinstance(p.get('approved'), dict):
            return
        found = {'kind': 'none', 'ref': '', 'ok': False, 'why': 'their messages could not be read, so this yes rests on what chat reported'}
        store = chat_store(life)
        if store:
            try:
                import chats as C
                import hisreply as H
                now = time.time()
                told = float((p.get('told') or {}).get('at') or 0)
                msgs = [m for m in C.messages(max(now - RECENT, told - 1), path=store) if m['who'] == 'owner']
                hit = C.quoted_in(words, msgs) if len(' '.join(str(words or '').split())) >= 3 else None
                if hit is None:
                    found['why'] = 'no recent message of theirs holds these words, so this yes rests on what chat reported'
                elif H.read(hit['text'])['answer'] != 'yes' and not H.agrees(hit['text']):
                    found['why'] = 'their message does not read as a plain yes'
                elif con.execute("SELECT 1 FROM opps WHERE key!=? AND json_extract(payload,'$.approved.authority_ref.ref')=?", (key, f"chat:{hit['id']}")).fetchone():
                    found['why'] = 'that message of theirs already decided another item'
                else:
                    found = {'kind': 'owner_message', 'ref': f"chat:{hit['id']}", 'ok': True, 'why': 'a recent message of theirs holds these words and reads as a plain yes'}
            except Exception:
                pass
        p['approved']['authority_ref'] = found
        con.execute('UPDATE opps SET payload=? WHERE key=?', (json.dumps(p, default=str), key))
        con.commit()
        if found['ok']:
            _on_job(con, life, key, found, f"authority:{key}:{int(float(p['approved'].get('at') or 0))}")
    except Exception:
        pass

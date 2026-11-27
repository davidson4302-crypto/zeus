"""Profile-relative native request provenance, separate from memory/channel policy.

Remote scope follows an already accepted native event, live runtime identity,
actual author and current foreground lease. This never authenticates a sender,
admits a channel, labels a local operator as a human, or grants tool permission.
"""
from __future__ import annotations
from dataclasses import dataclass
from contextlib import closing
import hashlib,json,math,sqlite3,threading,time,weakref
from pathlib import Path

def surface(value):
    value=str(getattr(value,'value',value) or '').lower()
    return {'gui':'desktop','local':'cli'}.get(value,value)

def sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def parent_now():
    from agent.subagent_lifecycle import get_active_subagent_parent
    return get_active_subagent_parent()

def event_now():
    from gateway.delivery_attempts import current_source_event
    return current_source_event()

def native_text(value):
    """Only native current-user text parts; image URLs/data never become authority."""
    if isinstance(value,str):return value
    if not isinstance(value,list):raise ValueError('Unsupported native user content')
    for part in value:
        if not isinstance(part,dict) or part.get('type') not in {'text','input_text','image_url','input_image'}:
            raise ValueError('Unsupported native user content part')
        if any(k in part for k in ('role','source','display_kind','observed')):
            raise ValueError('Observed or injected content cannot provide work authority')
        if part['type'] in {'text','input_text'} and not isinstance(part.get('text'),str):
            raise ValueError('Native user text must be a string')
    try:
        from session_rules.turns import flatten_message_text   # current Hermes
    except ImportError:
        from agent.message_content import flatten_message_text  # older Hermes
    return flatten_message_text(value)

def lease(home,parent):
    native=getattr(parent,'_session_db',None)
    resolver=getattr(type(native),'_session_turn_lease_key_on_conn',None)
    holder=getattr(parent,'_active_session_turn_lease_holder',None)
    if not native or not holder or not callable(resolver) or Path(native.db_path).resolve()!=home/'state.db':return None
    try:
        with closing(sqlite3.connect((home/'state.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
            con.row_factory=sqlite3.Row;con.execute('BEGIN')
            key=resolver(native,con,parent.session_id)
            row=con.execute('SELECT holder,expires_at FROM session_turn_leases WHERE conversation_id=?',(key,)).fetchone()
            if not row or row['holder']!=holder or type(row['expires_at']) not in (int,float) or not math.isfinite(row['expires_at']) or row['expires_at']<=time.time():return None
            return key,holder
    except (OSError,sqlite3.Error,AttributeError,ValueError,TypeError):return None

def coordinates(event):
    from gateway.session_identity import identity_of
    source=getattr(event,'source',None);identity=identity_of(source) if source else None
    if identity is None:return None
    return (identity.runtime_profile,str(identity.runtime_home.resolve()),identity.transport_profile,
            str(identity.authorization_home.resolve()),id(identity.adapter()),surface(source.platform),
            source.chat_type,source.chat_id,source.user_id,getattr(source,'thread_id',None),
            getattr(source,'scope_id',None),getattr(event,'message_id',None),getattr(source,'is_bot',None),
            getattr(event,'internal',None),getattr(event,'_gateway_accepted',None))

def local_lineage(home,profile,sid,platform):
    allowed={'cli','oneshot','tui'} if platform in {'cli','tui'} else {'desktop'}
    seen=set()
    with closing(sqlite3.connect((home/'state.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
        for _ in range(64):
            if not sid or sid in seen:return False
            seen.add(sid)
            row=con.execute('SELECT source,profile_name,parent_session_id FROM sessions WHERE id=?',(sid,)).fetchone()
            if not row or row[0] not in allowed or row[1] not in (None,profile):return False
            if not row[2]:return True
            prior=con.execute('SELECT end_reason FROM sessions WHERE id=?',(row[2],)).fetchone()
            if not prior or prior[0]!='compression':return False
            sid=row[2]
    return False

@dataclass(frozen=True)
class Bound:
    parent:object
    event:object
    coordinates:tuple|None
    holder:str
    source:dict

class SourceAdapter:
    def __init__(self,ctx,home):
        self.ctx=ctx;self.home=Path(home).resolve();self.lock=threading.RLock();self.bound={}

    @property
    def private(self):return self.ctx.get_config('source_policy','native')=='zeus-private'

    def bind(self,**context):
        if self.private:return None
        parent=parent_now();sid=context.get('session_id');tid=context.get('turn_id')
        key=(sid,tid)
        with self.lock:self.bound.pop(key,None)
        if (not parent or not sid or not tid or parent.session_id!=sid or getattr(parent,'_current_turn_id',None)!=tid
            or getattr(parent,'_parent_session_id',None) or getattr(parent,'_turn_origin',None)
            or getattr(parent,'_persist_disabled',False) or getattr(parent,'_interrupt_requested',False)
            or context.get('parent_session_id') or context.get('turn_display_kind')):return None
        platform=surface(parent.platform)
        if platform!=surface(context.get('platform')) or platform=='cron':return None
        live=lease(self.home,parent)
        if not live:return None
        history=context.get('conversation_history') or [];latest=history[-1] if history else {}
        try:text=native_text(context.get('user_message'))
        except ValueError:return None
        if (not text.strip() or not isinstance(latest,dict) or latest.get('role')!='user'
            or latest.get('display_kind') or latest.get('observed') or latest.get('_compressed_summary') or latest.get('_shared_archive')):return None
        override=getattr(parent,'_persist_user_message_override',None)
        try:
            if (override is not None and native_text(override)!=text) or (override is None and native_text(latest.get('content'))!=text):return None
        except ValueError:return None
        metadata=latest.get('display_metadata') or {}
        if isinstance(metadata,str):
            try:metadata=json.loads(metadata)
            except ValueError:return None
        if not isinstance(metadata,dict) or metadata.get('source'):return None
        event=event_now();author=getattr(parent,'_turn_author',None)
        profile=self.ctx.profile_name
        source={'session_id':sid,'turn_id':tid,'profile_home':str(self.home),'profile':profile,
                'platform':platform,'text':text,'conversation_id':live[0]}
        if event is None:
            if platform not in {'cli','tui','desktop'} or author or getattr(parent,'_user_id',None):return None
            try:
                if not local_lineage(self.home,profile,sid,platform):return None
            except (OSError,sqlite3.Error,TypeError):return None
            if platform=='desktop':
                from .desktop_origin import desktop_human_submit
                if not desktop_human_submit(sid,tid,text,history):return None
                source.update(origin='desktop_submit',author_id=None,
                              audience={'kind':'private_owner','profile_home':str(self.home),'conversation_id':live[0]})
            else:
                source.update(origin='local_operator',author_id=None,audience={'kind':'local_operator','profile_home':str(self.home)})
            event_ref=None;coords=None
        else:
            from gateway.session_identity import identity_of
            remote=getattr(event,'source',None);identity=identity_of(remote) if remote else None
            if (getattr(event,'internal',None) is not False or getattr(event,'_gateway_accepted',None) is not True
                or not remote or getattr(remote,'is_bot',None) is not False or not isinstance(author,dict)
                or author.get('is_bot') is not False or not remote.user_id or author.get('id')!=remote.user_id
                or identity is None or identity.runtime_home.resolve()!=self.home or identity.runtime_profile!=profile
                or identity.adapter() is None or surface(remote.platform)!=platform or not remote.chat_id):return None
            claimed=context.get('turn_author')
            if isinstance(claimed,dict) and (claimed.get('id')!=author['id'] or claimed.get('is_bot') is not False):return None
            # Native canonical routing and accepted sender are authority. Keep the
            # receiving transport and authorization home; never infer default route.
            source.update(origin='direct',author_id=remote.user_id,chat_id=remote.chat_id,
                          message_id=getattr(event,'message_id',None),transport_profile=identity.transport_profile,
                          audience={'kind':'native_remote','profile_home':str(self.home),'conversation_id':live[0],
                                    'platform':platform,'chat_id':remote.chat_id,'chat_type':remote.chat_type,
                                    'author_id':remote.user_id if remote.chat_type=='dm' else None,
                                    'authorization_home':str(identity.authorization_home.resolve())})
            # Preserve already-enabled private Bot Chat continuity. The native
            # binding selects the shared conversation, while the existing owner
            # policy verifies this DM belongs to that owner. Group rooms never
            # receive this owner-wide audience scope.
            if remote.chat_type=='dm':
                from gateway.shared_conversation import binding_for_source,private_owner_source_allowed
                shared=binding_for_source(remote)
                if (shared and shared.get('profile_home')==str(self.home) and shared.get('profile')==profile
                    and private_owner_source_allowed(remote)):
                    source['audience']={'kind':'private_owner','profile_home':str(self.home),'conversation_id':live[0]}
            try:event_ref=weakref.ref(event)
            except TypeError:return None
            coords=coordinates(event)
        source['receipt']={'kind':source['origin'],'id':sha([str(self.home),sid,tid,text,source['audience']]),
                           'profile_home':str(self.home),'session_id':sid,'turn_id':tid,'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                           'human_author_claimed':event is not None or source['origin']=='desktop_submit'}
        with self.lock:
            self.bound[key]=Bound(weakref.ref(parent),event_ref,coords,live[1],source)
            if len(self.bound)>256:self.bound.pop(next(iter(self.bound)))
        return None

    def current(self):
        if self.private:
            from zeus_scope import current_source,allowed_private
            value=current_source(self.home)
            return value if value and allowed_private(self.home,session_id=value['session_id']) else None
        parent=parent_now()
        if not parent:return None
        if (getattr(parent,'_parent_session_id',None) or getattr(parent,'_turn_origin',None)
            or getattr(parent,'_persist_disabled',False) or getattr(parent,'_interrupt_requested',False)):return None
        key=(getattr(parent,'session_id',None),getattr(parent,'_current_turn_id',None))
        with self.lock:bound=self.bound.get(key)
        if not bound or bound.parent() is not parent or getattr(parent,'_interrupt_requested',False):return None
        live=lease(self.home,parent)
        if not live or live[1]!=bound.holder or live[0]!=bound.source['conversation_id']:return None
        event=event_now()
        if bound.event:
            if bound.event() is not event or coordinates(event)!=bound.coordinates:return None
            author=getattr(parent,'_turn_author',None)
            if not isinstance(author,dict) or author.get('id')!=bound.source['author_id'] or author.get('is_bot') is not False:return None
        elif event is not None or getattr(parent,'_turn_author',None) or getattr(parent,'_user_id',None):return None
        return dict(bound.source)

    def visible(self,record,source):
        if self.private or source.get('origin')=='local_operator':return True
        original=record.get('source') or {};a=original.get('audience') or {};b=source.get('audience') or {}
        if a.get('kind')=='private_owner' and b.get('kind')=='private_owner':
            return all(a.get(k)==b.get(k) for k in ['profile_home','conversation_id'])
        # Records from before portable audience binding need local reconciliation.
        if a.get('kind')!='native_remote' or b.get('kind')!='native_remote':return False
        return all(a.get(k)==b.get(k) for k in ['profile_home','conversation_id','platform','chat_id','chat_type','author_id','authorization_home'])

    def clear(self,session_id=None,**kwargs):
        with self.lock:
            for key in list(self.bound):
                if not session_id or key[0]==session_id:self.bound.pop(key,None)

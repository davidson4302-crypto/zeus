"""Narrow shared scope library. No credentials, personal facts or model-created authority."""
from pathlib import Path
import threading,weakref,hashlib,json
from . import trusted_chat, memory_gate
_lock=threading.RLock()
_sources={}

def bind(**context):
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home()).resolve()
    memory_gate.clear_native_turn(home,context)
    key=(str(home),context.get("session_id"))
    with _lock:
        _sources.pop(key,None)
        _local_sources.pop(key,None)
    receipt=trusted_chat.certify_native_turn(home,context)
    result=memory_gate.bind_native_turn(home,context,receipt)
    if not result.get("allowed"):
        _bind_local(home,context)
        return None
    grant=memory_gate._valid(home)
    if grant is None: return None
    source={"session_id":grant.session_id,"turn_id":grant.turn_id,
            "profile_home":str(home),"profile":home.name,"platform":grant.platform,
            "text":context.get("user_message",""),"author_id":grant.author_id,
            "origin":"direct","receipt":receipt}
    if grant.event is not None:
        event=grant.event()
        source.update(chat_id=event.source.chat_id,message_id=event.message_id,
                      transport_profile=home.name)
    with _lock: _sources[key]=source
    return None

def allowed_private(home,session_id=None):
    grant=memory_gate._valid(Path(home).resolve())
    return bool((grant and (session_id is None or grant.session_id==session_id)) or _local_valid(Path(home).resolve(),session_id))

def current_source(home):
    home=Path(home).resolve()
    grant=memory_gate._valid(home)
    if grant is None:
        row=_local_valid(home)
        return dict(row) if row else None
    with _lock: row=_sources.get((str(home),grant.session_id))
    return dict(row) if row and row.get("turn_id")==grant.turn_id else None

def clear(session_id=None,**kwargs):
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home()).resolve()
    memory_gate.clear_native_turn(home,{"session_id":session_id})
    if session_id:
        with _lock:
            _sources.pop((str(home),session_id),None)
            _local_sources.pop((str(home),session_id),None)

# Native CLI has no human-author stamp. Local private read/work scope does not
# invent one or grant personal-fact authority. Desktop additionally has the
# retained observer's exact durable-submit proof.
_local_sources={}
def _bind_local(home,context):
    from gateway.delivery_attempts import current_source_event
    parent=memory_gate._parent()
    if not memory_gate._owned(home,parent) or current_source_event() is not None:return
    sid,tid=context.get("session_id"),context.get("turn_id")
    platform=memory_gate._surface(getattr(parent,"platform",None))
    if (platform not in {"cli","tui","desktop"} or context.get("platform")!=platform
            or not sid or not tid or parent.session_id!=sid or parent._current_turn_id!=tid
            or context.get("turn_display_kind") or context.get("parent_session_id")
            or getattr(parent,"_turn_author",None) or getattr(parent,"_user_id",None)):
        return
    holder=getattr(parent,"_active_session_turn_lease_holder",None)
    if not holder or not memory_gate._live_turn_lease(home,parent,holder):return
    text=context.get("user_message")
    if not isinstance(text,str) or not text.strip():return
    try:
        with trusted_chat._db(home/"state.db") as db:
            _local_lineage(db,sid,home,platform)
        if platform=="desktop":
            from .desktop_origin import desktop_human_submit
            if not desktop_human_submit(sid,tid,text,context.get("conversation_history")):return
            origin="desktop_submit"
        else:
            history=context.get("conversation_history") or []
            latest=history[-1] if history else {}
            if (latest.get("role")!="user" or latest.get("display_kind")
                    or latest.get("observed") or latest.get("_compressed_summary")
                    or latest.get("content")!=text):return
            trusted_chat._metadata(latest.get("display_metadata"))
            origin="local_operator"
    except (OSError,ValueError,TypeError,trusted_chat.sqlite3.Error):return
    fingerprint=hashlib.sha256(text.encode()).hexdigest()
    receipt={"id":hashlib.sha256(json.dumps([str(home),sid,tid,fingerprint]).encode()).hexdigest(),
             "kind":origin,"profile_home":str(home),"session_id":sid,"turn_id":tid,
             "text_sha256":fingerprint,"human_author_claimed":platform=="desktop"}
    row={"session_id":sid,"turn_id":tid,"profile_home":str(home),"profile":home.name,
         "platform":platform,"text":text,"origin":origin,"author_id":None,"receipt":receipt}
    with _lock:
        _local_sources[(str(home),sid)]=(weakref.ref(parent),holder,row)
        while len(_local_sources)>128:_local_sources.pop(next(iter(_local_sources)))

def _local_valid(home,session_id=None):
    from gateway.delivery_attempts import current_source_event
    parent=memory_gate._parent()
    if not memory_gate._owned(home,parent) or current_source_event() is not None:return None
    if session_id is not None and parent.session_id!=session_id:return None
    with _lock: bound=_local_sources.get((str(home),parent.session_id))
    if bound is None:return None
    ref,holder,row=bound
    if (ref() is not parent or row['turn_id']!=getattr(parent,'_current_turn_id',None)
            or getattr(parent,'_active_session_turn_lease_holder',None)!=holder
            or getattr(parent,'_turn_author',None) or getattr(parent,'_user_id',None)
            or not memory_gate._live_turn_lease(home,parent,holder)):
        return None
    return row

def allows_provider_context(home,context):
    row=current_source(home)
    return bool(row and isinstance(context,dict) and context.get('hermesHome')==str(Path(home).resolve())
                and context.get('sessionId')==row['session_id'] and context.get('turnId')==row['turn_id']
                and memory_gate._surface(context.get('platform'))==row['platform']
                and context.get('agentContext')=='primary' and not context.get('parentSessionId')
                and context.get('authorIsBot') is False
                and context.get('userId')==row.get('author_id')
                and (not row.get('chat_id') or context.get('chatId')==row['chat_id']))

def admit_gateway(event=None,**kwargs):
    """Keep native profile isolation after the standard sender authentication.

    The standard sender allowlist decides who may talk to Zeus; this hook only requires an authenticated native
    source bound to this profile. Mail remains bot-authored and acquires no owner fact/work authority.
    """
    from hermes_constants import get_hermes_home
    from gateway.session_identity import identity_of
    home=Path(get_hermes_home()).resolve()
    if home.name not in trusted_chat.scoped_profiles():return None
    source=getattr(event,'source',None)
    identity=identity_of(source) if source is not None else None
    if (identity is None or identity.runtime_home.resolve()!=home
            or identity.runtime_profile!=home.name
            or identity.authorization_home.resolve()!=home
            or identity.transport_profile!=home.name
            or getattr(event,'internal',True) is not False):
        return {'action':'block','reason':'Zeus requires an authenticated native source bound to this profile.'}
    return None

def guard_private_tools(tool_name='',**kwargs):
    """Keep retained native history scoped to this profile and any configured retained predecessor."""
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home()).resolve()
    if tool_name=='session_search':
        args=kwargs.get('args') or {}
        requested=str(args.get('profile') or '').strip()
        session=str(args.get('session_id') or '')
        if not requested and '/' in session:
            requested=session.split('/',1)[0]
        if requested and requested not in {home.name,*trusted_chat.scoped_profiles()}:
            return {'action':'block','message':'Zeus history access is scoped to this profile and its retained predecessor.'}
    return None


def _local_lineage(db,session_id,home,platform):
    """Native one-shot CLI is local operator work, not a human-authorship stamp."""
    permitted={'cli','oneshot','tui'} if platform in {'cli','tui'} else {'desktop'}
    seen=set()
    for _ in range(64):
        if not session_id or session_id in seen:raise ValueError('Invalid local lineage')
        seen.add(session_id)
        row=db.execute('SELECT source,profile_name,parent_session_id FROM sessions WHERE id=?',(session_id,)).fetchone()
        if row is None or row['source'] not in permitted or row['profile_name'] not in (None,home.name):
            raise ValueError('Not a current native local operator session')
        parent=row['parent_session_id']
        if not parent:return
        previous=db.execute('SELECT end_reason FROM sessions WHERE id=?',(parent,)).fetchone()
        if previous is None or previous[0]!='compression':raise ValueError('Imported or delegated lineage')
        session_id=parent
    raise ValueError('Local lineage too deep')

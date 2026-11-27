"""Narrow shared scope library. No credentials, personal facts or model-created authority."""
from pathlib import Path
import threading,hashlib,json
from . import trusted_chat, memory_gate
_lock=threading.RLock()
_sources={}

def bind(**context):
    """pre_llm_call: bind this turn's private scope when Zeus's owner test says the owner sent it."""
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home()).resolve()
    memory_gate.clear_native_turn(home,context)
    key=(str(home),context.get("session_id"))
    with _lock:
        _sources.pop(key,None)
    result=memory_gate.bind_native_turn(home,context,None)
    if not result.get("allowed"):
        return None
    grant=memory_gate._valid(home)
    if grant is None: return None
    text=context.get("user_message","")
    local=grant.author_id is None
    receipt={"id":hashlib.sha256(json.dumps([str(home),grant.session_id,grant.turn_id,grant.message_id,
                                             hashlib.sha256(str(text).encode()).hexdigest()]).encode()).hexdigest(),
             "kind":"local_operator" if local else "direct","profile_home":str(home),
             "session_id":grant.session_id,"turn_id":grant.turn_id,
             "text_sha256":hashlib.sha256(str(text).encode()).hexdigest(),"human_author_claimed":not local}
    source={"session_id":grant.session_id,"turn_id":grant.turn_id,
            "profile_home":str(home),"profile":home.name,"platform":grant.platform,
            "text":text,"author_id":grant.author_id,
            "origin":"local_operator" if local else "direct","receipt":receipt}
    if not local:
        source.update(chat_id=grant.chat_id,message_id=grant.message_id,transport_profile=home.name)
    with _lock: _sources[key]=source
    return None

def allowed_private(home,session_id=None):
    grant=memory_gate._valid(Path(home).resolve())
    return bool(grant and (session_id is None or grant.session_id==session_id))

def current_source(home):
    home=Path(home).resolve()
    grant=memory_gate._valid(home)
    if grant is None:
        return None
    with _lock: row=_sources.get((str(home),grant.session_id))
    return dict(row) if row and row.get("turn_id")==grant.turn_id else None

def clear(session_id=None,**kwargs):
    from hermes_constants import get_hermes_home
    home=Path(get_hermes_home()).resolve()
    memory_gate.clear_native_turn(home,{"session_id":session_id})
    if session_id:
        with _lock:
            _sources.pop((str(home),session_id),None)

def allows_provider_context(home,context):
    row=current_source(home)
    if not (row and isinstance(context,dict)):
        return False
    zh=memory_gate.zeus_hermes()
    return bool(context.get('hermesHome')==str(Path(home).resolve())
                and context.get('sessionId')==row['session_id'] and context.get('turnId')==row['turn_id']
                and memory_gate._surface(context.get('platform'))==row['platform']
                and context.get('agentContext')=='primary' and not context.get('parentSessionId')
                and context.get('authorIsBot') is False
                and (row.get('author_id') is None or zh.normalize_id(context.get('userId'))==row.get('author_id'))
                and (not row.get('chat_id') or context.get('chatId')==row['chat_id']))

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

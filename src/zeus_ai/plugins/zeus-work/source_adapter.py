"""Profile-relative request provenance for saved work.

The current request comes only from the shared Zeus scope (aaa-zeus-scope), which binds a turn only when Zeus's
one owner test (engine/zeus_hermes.py) says the owner sent it. This never authenticates a sender, admits a
channel, labels a helper or a scheduled job as the owner, or grants tool permission. No current owner turn means
no source, and work tools then refuse.
"""
from __future__ import annotations
import hashlib,json,threading
from pathlib import Path

def surface(value):
    value=str(getattr(value,'value',value) or '').lower()
    return {'gui':'desktop','local':'cli'}.get(value,value)

def sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def native_text(value):
    """Only current-user text parts; image URLs/data never become authority."""
    if isinstance(value,str):return value
    if not isinstance(value,list):raise ValueError('Unsupported native user content')
    for part in value:
        if not isinstance(part,dict) or part.get('type') not in {'text','input_text','image_url','input_image'}:
            raise ValueError('Unsupported native user content part')
        if any(k in part for k in ('role','source','display_kind','observed')):
            raise ValueError('Observed or injected content cannot provide work authority')
        if part['type'] in {'text','input_text'} and not isinstance(part.get('text'),str):
            raise ValueError('Native user text must be a string')
    return ''.join(part['text'] for part in value if part['type'] in {'text','input_text'})

def is_owner(source):
    """Zeus's one owner test for a saved-work source: a local operator only when Zeus allows the CLI, else the
    owner's own direct chat on the owner platform. Anything unknown is not the owner."""
    try:
        import zeus_hermes
        if not isinstance(source,dict):return False
        if source.get('origin') in {'local_operator','desktop_submit'}:return zeus_hermes.allow_cli()
        if (source.get('audience') or {}).get('kind')!='private_owner' or source.get('origin')!='direct':return False
        return zeus_hermes.is_owner_source({'platform':source.get('platform'),'chat_type':'dm',
                                            'chat_id':source.get('chat_id'),'user_id':source.get('author_id')})
    except Exception:
        return False

class SourceAdapter:
    def __init__(self,ctx,home):
        self.ctx=ctx;self.home=Path(home).resolve();self.lock=threading.RLock()

    @property
    def private(self):
        """Always the shared owner scope: one owner per Zeus profile."""
        return True

    def bind(self,**context):
        """The shared scope binds the turn (aaa-zeus-scope's pre_llm_call); nothing to bind here."""
        return None

    def current(self):
        from zeus_scope import current_source,allowed_private
        value=current_source(self.home)
        if not value or not allowed_private(self.home,session_id=value['session_id']):return None
        value=dict(value)
        value.setdefault('conversation_id',value['session_id'])
        if value.get('origin')=='direct':
            value['audience']={'kind':'private_owner','profile_home':str(self.home)}
        else:
            value['audience']={'kind':'local_operator','profile_home':str(self.home)}
        return value

    def visible(self,record,source):
        """Saved work in this profile belongs to its one owner, so only an owner turn in this profile sees it.
        Anyone else (another number, a group, a helper, a garbled source) sees none of it and cannot stop it."""
        return bool(isinstance(source,dict) and source.get('profile_home')==str(self.home) and is_owner(source))

    def clear(self,session_id=None,**kwargs):
        return None

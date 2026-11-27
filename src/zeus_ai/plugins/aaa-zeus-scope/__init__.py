"""All Zeus profiles share this pinned code path; live grants remain home-keyed."""
import sys
from pathlib import Path
from . import zeus_scope as candidate_scope

def register(ctx):
    prior=sys.modules.get("zeus_scope")
    if prior is not None and Path(prior.__file__).resolve() != Path(candidate_scope.__file__).resolve():
        raise RuntimeError("Conflicting Zeus scope library path; use one pinned shared code release")
    scope=prior if prior is not None else candidate_scope
    sys.modules.setdefault("zeus_scope",scope)
    ctx.register_hook("pre_llm_call",scope.bind)
    ctx.register_hook("on_session_end",scope.clear)
    ctx.register_hook("pre_tool_call",scope.guard_private_tools)

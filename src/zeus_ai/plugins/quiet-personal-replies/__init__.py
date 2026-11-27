"""Reply style only. No credentials, facts, networking, capture, sends or native-note edits."""
import re
import sys
from pathlib import Path


def _agent_names():
    """Names a greeting may address the agent by: the configured agent name, plus the default."""
    names = {"zeus"}
    try:
        engine = Path(__file__).resolve().parents[2] / "engine"
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        import settings
        names.add(str(settings.AGENT_NAME or "").strip().lower())
    except Exception:
        pass
    return "|".join(sorted(re.escape(n) for n in names if n))
STYLE = (
    "Keep routine memory lookup, capture and internal review in the background. "
    "Give the useful answer in natural, everyday words. For a simple personal fact question, "
    "when authorized current records answer it, the lookup is complete: answer with the fact "
    "in one short sentence. Do not narrate stores, tools, record dates, rejected test values "
    "or unrelated people's records. Resolve irrelevant records silently. "
    "When the user explicitly asks about sources, conflicting records, memory or system behavior, "
    "explain what they asked in plain language. When access genuinely prevents an answer, "
    "state that limitation briefly and honestly. "
    "This is reply style only; existing roles, tools, permissions and approval rules still apply."
)



# A greeting is a turn of its own, not a request to resume the previous job.
_AVAILABILITY = re.compile(
    r"(?:hi|hey|hello|morning|good (?:morning|afternoon|evening)"
    r"|(?:are )?you (?:there|here|online)"
    r"|(?:are you able to|can you) (?:reply|respond)(?: here)?(?: now)?)"
    r"(?:[, ]+(?:" + _agent_names() + r"))?[?!.]*", re.I,
)
_ONE_SENTENCE = re.compile(
    r"^(?:please\s+)?(?:keep this to|(?:reply|answer) in)\s+"
    r"(?:one|1|a)\s+(?:short\s+)?sentence\s*:\s*", re.I,
)
AVAILABILITY_STYLE = (
    "Current human message is only a greeting or a check that this conversation can reply. "
    "Give a natural one-sentence acknowledgement now. This response itself establishes that "
    "you can reply. Do not run tools, delegate, send, patch, inspect, or finish prior tasks "
    "before answering this greeting. Prior tasks, internal notifications and compaction "
    "summaries stay background context for this greeting. Do not claim those tasks were "
    "completed, cancelled or changed. Keep them in their existing state. "
    "This is current-turn reply style only; preserve identity, roles, history, permissions "
    "and real safety controls."
)


def _is_availability_check(text):
    return len(text) <= 180 and bool(_AVAILABILITY.fullmatch(_ONE_SENTENCE.sub('', text.strip())))


def before_turn(user_message='', parent_session_id='', turn_author=None, turn_display_kind=None, **kwargs):
    text = str(user_message or '').strip()
    if parent_session_id or not text:
        return None
    zh = _zeus_hermes()
    context = {'turn_author': turn_author, 'turn_display_kind': turn_display_kind}
    if (zh.is_human_turn(zh.turn_author(context), zh.turn_display_kind(context), parent_session_id)
            and _is_availability_check(text)):
        return {'context': AVAILABILITY_STYLE}
    return {'context': STYLE}


def register(ctx):
    ctx.register_hook('pre_llm_call', before_turn)


def _zeus_hermes():
    """Zeus's shared Hermes bridge (zeus_ai/engine/zeus_hermes.py)."""
    import sys
    from pathlib import Path
    engine = str(Path(__file__).resolve().parents[2] / 'engine')
    if engine not in sys.path:
        sys.path.insert(0, engine)
    import zeus_hermes
    return zeus_hermes

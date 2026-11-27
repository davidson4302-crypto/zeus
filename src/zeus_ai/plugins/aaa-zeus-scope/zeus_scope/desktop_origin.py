"""Native Desktop source proof for the current turn: read-only, no personal data writes."""
import sqlite3,json
def desktop_human_submit(session_id, turn_id, user_message, conversation_history):
    """Read-only native origin adapter. A local desktop human has no author ID.

    Bot relay also persists submit rows, so both the scoped current author and
    the actual durable submit must match. Missing native metadata fails closed.
    This does not infer ownership from session titles or the session opener.
    """
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
        from hermes_constants import get_hermes_home
        from hermes_cli.profiles import get_active_profile_name
        from hermes_state import SessionDB
        from agent.message_content import flatten_message_text
        agent = get_active_subagent_parent()
        if (agent is None or getattr(agent, 'session_id', None) != session_id
                or getattr(agent, '_current_turn_id', None) != turn_id
                or getattr(agent, 'platform', None) != 'desktop'
                or getattr(agent, '_parent_session_id', None)
                or getattr(agent, '_user_id', None)
                or not hasattr(agent, '_turn_author') or agent._turn_author):
            return False
        current = next((m for m in reversed(conversation_history or []) if m.get('role') == 'user'), {})
        if (flatten_message_text(current.get('content')) != flatten_message_text(user_message)
                or current.get('display_kind') or not current.get('_submit_row_session_id')
                or not isinstance(current.get('_row_id'), int) or not current.get('message_uid')):
            return False
        profile = get_active_profile_name()
        owner_session = current['_submit_row_session_id']
        with sqlite3.connect((get_hermes_home() / 'state.db').as_uri()+'?mode=ro', uri=True) as source:
            source.row_factory = sqlite3.Row
            cursor, seen = session_id, set()
            while cursor and cursor not in seen:
                seen.add(cursor)
                session = source.execute('SELECT source,profile_name,parent_session_id FROM sessions WHERE id=?', (cursor,)).fetchone()
                if not session or session['source'] != 'desktop' or session['profile_name'] not in (None, profile):
                    return False
                if cursor == owner_session:
                    break
                cursor = session['parent_session_id']
            else:
                return False
            row = source.execute('''SELECT content,display_kind,display_metadata,message_uid FROM messages
                WHERE id=? AND session_id=? AND role='user' AND active=1''', (current['_row_id'], owner_session)).fetchone()
        if not row:
            return False
        stored = SessionDB._decode_content(row['content']) if isinstance(current.get('content'), list) else row['content']
        metadata = json.loads(row['display_metadata']) if row['display_metadata'] else {}
        return (not row['display_kind'] and row['message_uid'] == current['message_uid']
                and flatten_message_text(stored) == flatten_message_text(user_message)
                and isinstance(metadata, dict) and not set(metadata) - {'title_preview'}
                and not set(current.get('display_metadata') or {}) - {'title_preview'})
    except Exception:
        return False

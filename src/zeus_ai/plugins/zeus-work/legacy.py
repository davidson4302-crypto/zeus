"""Read-only exact current-turn ownership of work kept by a separate goals plugin.

Optional coexistence (plugin config `legacy_coexistence: true`): when another installed goals plugin already owns
this exact turn, zeus-work lets its file writes through instead of opening a second record. The other plugin's
database is named by `work.coexist_goals_db` (relative to the Hermes profile home); unset means no coexistence.
"""
import json,sqlite3,sys
from contextlib import closing
from pathlib import Path

_ENGINE=Path(__file__).resolve().parents[2]/'engine'
if str(_ENGINE) not in sys.path:sys.path.insert(0,str(_ENGINE))
import settings  # noqa: E402

def owns_turn(home,session_id,turn_id):
    if not session_id or not turn_id:return False
    relative=str(settings.get('work.coexist_goals_db','') or '')
    if not relative:return False
    path=Path(home).resolve()/relative
    if not path.exists():return False
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
        con.execute('PRAGMA query_only=ON');con.execute('BEGIN')
        source_ids=set()
        for ident,payload in con.execute('SELECT id,payload FROM loop_sources'):
            source=json.loads(payload)
            if source.get('session_id')==session_id and source.get('turn_id')==turn_id:source_ids.add(ident)
        if not source_ids:return False
        for payload, in con.execute("SELECT payload FROM loop_records WHERE kind='goal'"):
            goal=json.loads(payload)
            if goal.get('source_id') in source_ids or goal.get('correction_source') in source_ids:return True
    return False

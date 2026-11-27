"""chats: read-only view of what the owner and Zeus said to each other.

Zeus keeps one conversation across the owner's phone, Slack and the terminal in its own Hermes store. Without this
module, a date or a promise said in chat, or a correction ("Taylor isn't for me"), is only kept if the model happens
to call a save tool in that turn. This module is the one place that reads the store, so the quiet-moment pass, the
people pages and the nightly page all see the same messages.

It never writes to the store (opened read-only) and never reads tool output, background-job chatter, origin stamps,
bot-to-bot messages or outside email. Only two things come out:
  who='owner'  a real message that arrived from the owner (the gateway stamps those with a platform message id)
  who='zeus'   what Zeus answered in words

A message typed for the owner by a builder's terminal ("[<owner>, typing in the Codex terminal] ...") is marked
via='terminal' and LEFT OUT unless asked for: those are often test prompts, and a wrong learned fact is worse than
a missed one.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
import sqlite3
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402

STORE = os.environ.get('LIFE_CHAT_DB', str(settings.STATE_DB))
# Surfaces that are never the owner's chat: background jobs, sealed test sessions, one-shot asks, other programs, mail.
SKIP_SOURCES = ('tool', 'cron', 'oneshot', 'claude-code', 'codex', 'agentmail', 'a2a', 'webhook', 'api')
# How the gateway labels a relayed message from the owner: "[<owner first name>, ...]" (or "[Owner, ...]").
OWNER_LABEL = r'(?:%s|Owner|The owner|the owner)' % re.escape(settings.OWNER_FIRST)
TERMINAL = re.compile(r'^\[' + OWNER_LABEL + r', typing in the ([^\]]{1,60}?) terminal\]\s*')
BY_EMAIL = re.compile(r'^\[' + OWNER_LABEL + r', by email')
LABELLED = re.compile(r'^\[' + OWNER_LABEL + r',')
NOT_HIS = ('Gateway message origin', '[IMPORTANT:', 'Message from ', '[System', '[Cron', '<system')


# Rows that arrive as "a message from the owner" can carry text the owner never typed. A photo arrives with pages of
# Zeus's own operating rules after it, a Slack thread arrives with another bot's message inside, an emailed message has
# the relay's instructions in front, and a builder or a task board can type a long prompt in the owner's name. A model
# could quote any of that and a quote check would pass, because the words ARE in the row.
STAMPED = re.compile(r'^\(attachment\)|<[a-z][a-z_]{2,}>|Linked native session IDs'
                     r'|\[(?:Thread context|End of thread context|New message|Image attached at|screenshot|The user sent|Photon content)', re.I | re.M)
MACHINE = re.compile(r'^(FLEET-[A-Z-]+|' + OWNER_LABEL + r' assigns this |' + OWNER_LABEL + r' authorized |Health check from )', re.I)
MAX_TYPED = 1200     # the owner texts from a phone: anything longer was pasted or written by a program, so it is not read as theirs


def own_words(text: str):
    """-> (what the owner typed, how it came in) or ('', '') when that cannot be told for sure. Nothing in a
    delivery label may ever be quoted as theirs, and anything this cannot recognise counts as not theirs: a missed item is
    cheaper than a wrong one."""
    t = str(text or '').strip()
    via = 'direct'
    m = TERMINAL.match(t)
    if m:
        t, via = t[m.end():].strip(), 'terminal'
    elif BY_EMAIL.match(t):
        i = t.find('Subject:')
        if i < 0:
            return '', ''                     # the relay changed shape: do not guess where the owner's words start
        t, via = re.split(r'\n-- ?\n', t[i + len('Subject:'):])[0].strip(), 'email'
    elif LABELLED.match(t):
        t = re.sub(r'^\[[^\]]{0,300}\]\s*', '', t, count=1).strip()
    elif t.startswith('['):
        return '', ''
    if not t or STAMPED.search(t) or MACHINE.match(t) or len(t) > MAX_TYPED:
        return '', ''
    return t, via


def forgotten(profile=None):
    """What the owner asked Zeus to forget. The marker lives with Zeus's memory plugin
    (<profile>/unified-memory/forget.jsonl) and is read with that plugin's own matcher (runtime.Forgotten, loaded by
    path), so the chat and the engine cannot disagree about what is in scope. -> None when nothing was ever forgotten.
    Raises when a marker exists but cannot be read: every engine reader fails loudly rather than learn from it."""
    import importlib.util
    from pathlib import Path
    profile = Path(profile or Path(STORE).resolve().parent)
    if not (profile / 'unified-memory' / 'forget.jsonl').exists():
        return None
    path = (profile / 'plugins' / 'unified-memory' / 'runtime.py').resolve()
    key = 'life_forget_rt_%x_%d' % (abs(hash(str(path))), path.stat().st_mtime_ns)
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[key] = mod
    return sys.modules[key].Forgotten(profile)


def _owner_session(source, user_id) -> bool:
    """A user row counts as the owner's only by Zeus's one owner test (zeus_hermes.py): the owner platform from the
    owner's own sender id, or a local app session on this machine. Any other sender or platform is not the owner."""
    import zeus_hermes
    surface = zeus_hermes.surface(source)
    if surface in zeus_hermes.LOCAL_SURFACES or surface in ('desktop', 'gui'):
        return True
    return surface == zeus_hermes.owner_platform() and zeus_hermes.normalize_id(user_id) in zeus_hermes.owner_ids()


def _connect(path=None):
    con = sqlite3.connect('file:%s?mode=ro' % (path or STORE), uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def messages(since: float, until: float | None = None, path=None, include_terminal: bool = False, owner_terms=()) -> list:
    """The owner's real messages and Zeus's worded replies between two times, oldest first.
    Each: {'id', 'at', 'who', 'text', 'via', 'session'}. 'id' is the store's own row number (stable, citeable)."""
    marks = ','.join('?' * len(SKIP_SOURCES))
    sql = ('SELECT m.id, m.timestamp, m.role, m.content, m.platform_message_id, m.session_id, '
           's.source AS session_source, s.user_id AS session_user FROM messages m '
           'JOIN sessions s ON s.id = m.session_id '
           f'WHERE s.source NOT IN ({marks}) AND m.timestamp >= ? AND m.timestamp < ? '
           '{room}'
           "AND m.role IN ('user', 'assistant') AND m.content IS NOT NULL AND m.content != '' "
           'AND m._compressed_summary = 0 ORDER BY m.timestamp, m.id')
    out = []
    gone = forgotten(Path(path or STORE).resolve().parent)
    hidden = set()      # sessions whose last message from the owner was forgotten: Zeus's replies to it go too
    with closing(_connect(path)) as con:
        # A shared room has other speakers, so it is never read as the owner's. (Older stores have no chat_type column.)
        has_type = any(r[1] == 'chat_type' for r in con.execute('PRAGMA table_info(sessions)'))
        sql = sql.replace('{room}', "AND COALESCE(s.chat_type, '') != 'group' " if has_type else '')
        params = [*SKIP_SOURCES, float(since), float(until if until is not None else 4e12)]
        if owner_terms:
            # Internal words are literal alphanumeric search terms from a current source.
            words = [w for w in owner_terms if re.fullmatch(r"[a-z0-9'-]{3,}", w)][:40]
            if not words:
                return []
            predicates = ' OR '.join('instr(lower(m.content), ?) > 0' for _ in words)
            sql = sql.replace('ORDER BY', f"AND m.role='user' AND ({predicates}) ORDER BY")
            params.extend(words)
        for r in con.execute(sql, params):
            text = str(r['content']).strip()
            if not text:
                continue
            if r['role'] == 'user':
                if not r['platform_message_id'] or text.startswith(NOT_HIS):
                    continue
                if not _owner_session(r['session_source'], r['session_user']):
                    continue
                text, via = own_words(text)
                if not text or (via == 'terminal' and not include_terminal):
                    continue
                if gone and gone.hides(text, row_id=r['id'], session=r['session_id'], at=r['timestamp']):
                    hidden.add(r['session_id'])
                    continue
                hidden.discard(r['session_id'])
                out.append({'id': r['id'], 'at': r['timestamp'], 'who': 'owner', 'text': text, 'via': via, 'session': r['session_id']})
            else:
                if gone and (r['session_id'] in hidden or gone.hides(text, row_id=r['id'], session=r['session_id'], at=r['timestamp'])):
                    continue
                out.append({'id': r['id'], 'at': r['timestamp'], 'who': 'zeus', 'text': text, 'via': 'reply', 'session': r['session_id']})
    return out


def conversations(msgs: list, gap_minutes: float = 20) -> list:
    """Split into conversations wherever nobody spoke for gap_minutes. A conversation with no message from the owner is
    dropped (Zeus talking to tools or itself is not a conversation with the owner)."""
    groups, cur = [], []
    for m in msgs:
        if cur and m['at'] - cur[-1]['at'] > gap_minutes * 60:
            groups.append(cur)
            cur = []
        cur.append(m)
    if cur:
        groups.append(cur)
    return [g for g in groups if any(m['who'] == 'owner' for m in g)]


def gone_quiet(conv: list, now: float, quiet_minutes: float = 20) -> bool:
    """True once nobody has spoken for quiet_minutes: the moment to look back over it, never while the owner is mid-chat."""
    return bool(conv) and now - conv[-1]['at'] >= quiet_minutes * 60


def conv_id(conv: list) -> str:
    """Stable name for a conversation: its first and last message ids. A conversation that grows gets a new name,
    so a pass that already ran on the shorter one runs again only on what is new (callers keep the last id seen)."""
    return '%s-%s' % (conv[0]['id'], conv[-1]['id'])


def quoted_in(quote: str, conv: list, message_id=None) -> dict | None:
    """The message from the owner that really contains this quote (spaces and curly quotes forgiven), or None.
    Used so a learned fact can only be kept with the owner's own words and the message they came from."""
    def flat(s):
        s = str(s).replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
        return re.sub(r'\s+', ' ', s).strip().lower()
    q = flat(quote)
    if len(q) < 3:
        return None
    pool = [m for m in conv if m['who'] == 'owner']
    if message_id is not None:
        pool = [m for m in pool if str(m['id']) == str(message_id)] or pool
    for m in pool:
        if q in flat(m['text']):
            return m
    return None

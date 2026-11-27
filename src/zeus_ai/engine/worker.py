"""Worker: the real Zeus does the free work BEFORE the owner hears a word.

Every due item with free work gets one native Zeus job (a Hermes cron one-time job) that first checks the item is
still needed, then does the free steps, then prepares the step that needs the owner's yes all the way to the line. The
job delivers NOTHING. Its answer is a small JSON result ("packet") saved with the item. The composer (the real Zeus,
same model and voice as the owner's chat) later writes the one message the owner gets.

A one-time job does NOT catch up after sleep. Hermes drops a one-time job that is more than 120 seconds late (the Mac
asleep, a busy scheduler) without running it: it takes the job off its list and leaves a short note in the job's
output folder. Jobs are booked for now plus one minute, so a three-minute nap loses one. The harvest step looks for
that on every tick (_dropped) and books the job again under the usual rule: one retry, then the owner is told without
prep.

States: pending -> executing -> ready (packet saved, not told yet) | moot (nothing needed, never told)
        executing -> failed twice -> needs_attention + one repair alarm (_alarm)
        pending with no free work -> ready (packet built from the item itself)
Follow-ups a job asks for ("check the flight Saturday 10:50") are scheduled here, by code, as child items, so
"I'll check Saturday" is only ever said when something will really check.

Guards: mandate present and active; items whose key matches `work.skip_keys` (things handled elsewhere) are closed
untouched; a daily work budget and a few jobs at once (cost guards, not message caps: an item over budget is still
told, just without prep, and is counted).
Off: create <LIFE_HOME>/executor-off (items are then told without prep).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import fresh as FR
import settings

from ownertime import PACIFIC, clock as zone_clock  # the zone the owner is in right now (ownertime.py)
HERMES = settings.HERMES_BIN
PROFILE_HOME = settings.PROFILE_DIR
# The live life-engine folder, from the config file only (never from the LIFE_HOME environment variable), so a
# sandbox that sets LIFE_HOME is never taken for the live folder and never raises real alarms.
LIVE_HOME = Path(settings.get('paths.life_home', str(PROFILE_HOME / 'workspace' / 'life-engine')))
EXEC_DB = PROFILE_HOME / 'cron/executions.db'
OUT_DIR = PROFILE_HOME / 'cron/output'
ALARM_FILE = 'alarms.jsonl'            # every repair alarm is appended here, in the live life folder
ALARM_COMMAND = settings.get('alarms.command', '')   # optional program that also receives each alarm (JSON on stdin)
JOB_PREFIX = os.environ.get('LIFE_JOB_PREFIX', 'life-work')
MAX_WORK_PER_DAY = int(os.environ.get('LIFE_MAX_WORK_PER_DAY', '15'))
MAX_RUNNING = int(os.environ.get('LIFE_MAX_RUNNING', '3'))
PREP_LEAD = timedelta(minutes=40)      # start the work this long before the item is due, so the note carries results
NEVER_RAN_AFTER = 45 * 60              # a job that has no run this long after its start time is treated as failed
JOBS_FILE = PROFILE_HOME / 'cron/jobs.json'
ONESHOT_GRACE = 120                    # Hermes drops a one-time job more than this many seconds late (cron/jobs.py ONESHOT_GRACE_SECONDS)
DROPPED_BEFORE_FIRING = '# Cron job removed before firing'              # first line of the note Hermes leaves: it never ran
DROPPED_MID_RUN = '# Cron job removed without producing output'         # first line of its other note: claimed, never finished
SKIP_KEYS = tuple(str(k).lower() for k in (settings.get('work.skip_keys', []) or []) if k)   # item keys handled elsewhere: closed untouched
WORK_CLASSES = ('prepare_or_research', 'self_service_free')
STATUSES = ('done', 'needs_decision', 'moot', 'blocked', 'nothing_useful')
_WHO = '|'.join(['the owner', 'he', 'him', 'she', 'her'] + ([re.escape(settings.OWNER_FIRST)] if settings.OWNER_FIRST != 'the owner' else []))
NAG = re.compile(r"\b(" + _WHO + r")\b.{0,40}\b(approv\w*|repl\w+|answer\w*|respond\w*|decid\w*|chose|chosen|pick\w*|confirm\w*|sent|send)\b"
                 r"|\b(still|remains?)\s+(unsent|unanswered|undecided|unapproved|pending (?:his|her|their))\b"
                 r"|\b(repl\w+|answer\w*|respon\w+|approval|decision|word)\b.{0,25}\b(from|by)\s+(" + _WHO + r")\b", re.I)
FAILED = ('failed', 'error', 'timeout', 'interrupted', 'crashed', 'unknown', 'cancelled', 'missed', 'skipped')   # 'unknown' = its owner process died
MAX_JOB_SECONDS = 40 * 60              # a job still not finished this long after its start is treated as failed (a stalled model call)
CI_SWITCH = 'commit-integrity-on'      # integrity.py's switch file (commitment integrity). Without it nothing below calls into integrity.py, or even loads it
_STARTED = None                        # integrity: when this process's run() began (time.monotonic), for the real time of a claim
XF_SWITCH = 'cross-channel-fence-on'   # crossfence.py's switch file (needs commit-integrity-on). Without it nothing below calls into crossfence.py, or even loads it
_HEARD = {}                            # crossfence: what this run's prompts are built from (their lines on file, their newest message), carried by every claim; {} without the switch

PROMPT = """You are Zeus, working for the owner under their standing mandate (their own words, sent {mandate_at}):
"{mandate}"

Your own life pipes noticed something in the owner's {source}. Nobody has told them yet. Do what a great personal assistant
would do BEFORE speaking to them, so that what they hear is finished work, not an offer.

How this job works: your answer here is NOT sent to the owner, whatever any scheduler note says. This job delivers to
nobody. Your answer goes to your own pipes, and they alone write the owner one message later; they hold it for quiet hours
and never repeat themselves. Earlier background runs on this same item (you may find them in your session history,
their source is "cron") did not reach them either. So never hold back because of the hour or because you think they were
told, and never answer [SILENT]: always answer with the JSON object described below.

The item
Title: {title}
Why it matters: {why}
Evidence: {evidence}
Noticed: {noticed}. Now: {now}.
{extra}
What your pipes proposed as free work (a starting point, use your judgment):
{free_steps}
Steps that need their yes (prepare them fully, do NOT do them):
{gated_steps}
{prefs}
Work in this order:
1. Is it still needed? Check the source itself first. Has the owner already answered or handled it (their own sent mail
   in the same thread, a later email, the calendar event's own details)? Then check what they already told YOU:
   search your own recent conversation with them for this topic (their own messages to you; your background jobs,
   source "cron", are not them). If they already answered the question you would
   ask, use their answer and do not ask again. If they said they have it or to leave it, report status "moot". Is it an
   old repeating reminder with nothing behind it? If nothing is needed any more, stop and report status "moot".
2. Do every free step yourself now with your own tools: look things up, read their email and calendar (read-only),
   find addresses, times, prices, options and confirmation details, work out leave times, compare. Use what you
   already know about their people and places; check your memory and their contacts before treating anyone as a
   stranger. Never ask them for something you can find yourself.
3. Prepare the step that needs their yes all the way to the line: write the exact draft, name the one option you
   would choose and why (one pick, not a shortlist, unless the choice is truly their taste). They should be able to
   answer in one word.
4. If the thing changes on its own over the coming days (a flight, a delivery, a deadline, a reply you are waiting
   for from someone else), say what should be checked later and when in "followups". You do not schedule anything
   yourself; your pipes schedule those checks. Never add a follow-up just to see whether the owner has answered you:
   they are asked once.

Hard limits: never spend money, never message anyone except through this answer, never change or add calendar
events, never delete anything, never sign in anywhere, never text the owner yourself, never create or edit a scheduled
job, and never save or change your own memory, notes or skills from this job (durable facts go in "learned" below;
your pipes keep them). If a limit or a missing access stops you, finish everything else and put exactly what is missing in "limits" (never in the headline or details).
Where you may look: the item's own source, their email and calendars, their contacts, reminders and notes, your own
memory, and the public web. Do not read their private text conversations with other people (iMessage, WhatsApp,
direct messages) unless the item itself came from one. Do not open apps or windows on their Mac.
Honesty: do not invent facts. If you could not confirm something, leave it out, or say it is not known yet in a few
words about the thing itself ("no gate posted yet"), never about your own access. The headline and details are about
the thing only: what happened and what is true. Keep the facts the item already gives (place, time, amount, name).

Answer with ONE JSON object and nothing else:
{{
 "status": "done" | "needs_decision" | "moot" | "blocked" | "nothing_useful",
 "headline": "one plain sentence the owner would want to hear: the result, not the process",
 "details": "up to three short sentences with the facts that matter (times with weekday and date, places, prices, names). Empty if none.",
 "decision": null or {{"ask": "the one decision, phrased as a ready action", "ready": "exactly what is prepared: the full draft text, or the pick with its price or time", "default": "what you would do and why, one sentence", "by": "zeus" if you can carry it out yourself once the owner says yes, or "owner" if only the owner can do it (it needs their sign-in or their own hands); for "owner" put the one step they take in "ready"}},
 "followups": [{{"when": "ISO time with offset", "check": "what to check then", "tell_if": "when it is worth telling them"}}],
 "learned": ["durable facts you confirmed that are worth remembering, e.g. which office, which bank"],
 "moot_reason": "only for moot or nothing_useful: why nothing is needed",
 "limits": "what you could not open, read or check, if anything. This is for your pipes; the owner never sees it.",
 "sources": ["where each key fact came from, a few words each"]
}}
status guide: done = everything useful is finished and no decision is needed. needs_decision = the prep is finished
and one real decision is theirs (money, a message to someone outside their team, a calendar change, a delete, or a fact
only they know). A plan or a suggestion that leaves you nothing to send, buy, book, change or cancel on their yes is NOT a decision:
report it as done and put the suggestion in the headline. The same goes when your own pick is NOT to do it (send
nothing, leave it alone): there is no decision for them, so report done, or nothing_useful if they would not miss
hearing it, and say in the headline what you found and why you are leaving it. Never ask them to approve doing
nothing. And if someone else is already handling it (a teammate asked first, the other side is on it), that is
nothing_useful unless they would be worse off not knowing. blocked = something only they can supply stops the useful
part (say exactly what, once).
nothing_useful = you looked and there is nothing worth their attention. moot = it no longer needs anything."""

FOLLOWUP_EXTRA = """This is a follow-up check you asked your pipes to schedule on {asked}: "{check}"
Tell them only if: {tell_if}. If that is not the case, report status "nothing_useful" and they hear nothing.
Earlier result on this item: {earlier}
"""


def _ci(life=None, con=None):
    """integrity.py (commitment integrity), only when its switch file is in the life folder -> the module, else None.
    Every hook in this file asks here first, so without the switch the worker is what it was and integrity.py is not
    loaded. With the switch, a core that cannot be loaded is an error (it is not passed off as "switch off")."""
    try:
        if life is None:
            life = __import__('commit').home_of(con)
        if not (Path(life) / CI_SWITCH).exists():
            return None
    except Exception:
        return None
    import integrity
    return integrity


def _xf(life):
    """crossfence.py (what they said after a job was booked reaches that job), only when its switch file and integrity's
    are both in the life folder -> the module, else None. Without them the worker is what it was."""
    try:
        if not ((Path(life) / XF_SWITCH).exists() and (Path(life) / CI_SWITCH).exists()):
            return None
    except Exception:
        return None
    import crossfence
    return crossfence


def _claim_time(now_dt: datetime) -> datetime:
    """integrity: the moment a claim is really made, on now_dt's clock. now_dt is when the tick began; noticing can take
    minutes before the worker books anything, and the wait for a booking to land is counted from the claim. With a
    clock that is not the real one (a test's fixed time), only the time this run itself has taken is added."""
    lag = time.time() - now_dt.timestamp()
    if not 0 <= lag <= 1800:
        lag = time.monotonic() - _STARTED if _STARTED is not None else 0
    return now_dt + timedelta(seconds=max(0.0, lag))


def has_free_work(opp: dict) -> bool:
    """Worth a background job? Yes if it has free steps, or any step that needs their yes: preparing that step to the
    line (finding the options, writing the draft) is itself free work. Only a pure heads-up skips the job.
    (A birthday item with only buy/send steps still gets its options researched first.)"""
    steps = (opp.get('decision') or {}).get('steps') or []
    return any((s.get('autonomy') == 'auto' and s.get('action_class') in WORK_CLASSES) or s.get('autonomy') in ('offer', 'ask')
               or s.get('granted') for s in steps)


def standing_ok(opp: dict) -> list:
    """The steps of this item that run only because they gave a standing OK for this kind of thing (life grant)."""
    return [s['do'] for s in ((opp.get('decision') or {}).get('steps') or []) if s.get('granted') and s.get('do')]


def job_name(opp: dict, retry: bool = False) -> str:
    """The job's name tells the tool guard (life-work-guard) what the job may do: a plain prep job can only read and
    draft; a job for an item they gave a standing OK for may also carry out that step."""
    kind = JOB_PREFIX + ('-granted' if standing_ok(opp) else '')
    return f"{kind}{' retry' if retry else ''}: {str(opp.get('title'))[:40 if retry else 48]}"


def _runs(job_id):
    try:
        with closing(sqlite3.connect(EXEC_DB.as_uri() + '?mode=ro', uri=True, timeout=5)) as c:
            return c.execute('SELECT status,delivery_outcome,error,finished_at FROM executions WHERE job_id=? '
                             'UNION ALL SELECT status,delivery_outcome,error,finished_at FROM execution_archive WHERE job_id=?',
                             (job_id, job_id)).fetchall()
    except sqlite3.Error:
        return None


def _dropped(job_id):
    """Did the scheduler drop this one-time job? Asked only for a job with no run on record, past the 120 seconds.
    -> 'never ran'  Hermes's own note in the job's output folder says it was removed before firing;
       'cut short'  its note says the run was claimed and never finished;
       'gone'       no note, and the job is no longer on the scheduler's list (a finished one-time job stays listed);
       None         still listed, or the list could not be read. Not knowing is never "dropped"."""
    try:
        files = sorted((OUT_DIR / job_id).glob('*.md'), key=lambda p: p.stat().st_mtime)
        head = files[-1].read_text()[:120] if files else ''
    except OSError:
        head = ''
    if head.startswith(DROPPED_BEFORE_FIRING):
        return 'never ran'
    if head.startswith(DROPPED_MID_RUN):
        return 'cut short'
    try:
        data = json.loads(JOBS_FILE.read_text())
    except (OSError, ValueError):
        return None
    jobs = data.get('jobs', data) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return None
    return None if any(isinstance(j, dict) and str(j.get('id')) == str(job_id) for j in jobs) else 'gone'


def _response(job_id):
    """The job's final answer from its saved output file (the text after '## Response'), or None."""
    try:
        files = sorted((OUT_DIR / job_id).glob('*.md'), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    if not files:
        return None
    try:
        text = files[-1].read_text()
    except OSError:
        return None
    head, sep, tail = text.rpartition('\n## Response')
    return tail.strip() if sep else None


def parse_packet(text: str) -> dict:
    """The job's JSON result, checked. A reply that is not valid JSON comes back marked "unparsed": the caller treats
    that as a failed run (one retry, then the item is told without prep), so nothing is ever dropped on a non-answer."""
    raw = str(text or '').strip()
    obj = None
    start, end = raw.find('{'), raw.rfind('}')
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
        except ValueError:
            obj = None
    if not isinstance(obj, dict):
        return {'status': 'done', 'headline': '', 'details': raw[:900], 'decision': None, 'followups': [], 'learned': [],
                'unparsed': True}
    status = obj.get('status') if obj.get('status') in STATUSES else 'done'
    dec = obj.get('decision')
    if not (isinstance(dec, dict) and str(dec.get('ask') or '').strip()):
        dec = None
    else:
        by = 'owner' if str(dec.get('by') or '').strip().lower() == 'owner' else 'zeus'
        dec = {k: str(dec.get(k) or '').strip()[:2400] for k in ('ask', 'ready', 'default')}
        dec['by'] = by
    if status == 'needs_decision' and dec is None:
        status = 'done'
    # A follow-up watches the world (a flight, a delivery, someone else's reply). "Did the owner answer me yet?" is a nag,
    # not a follow-up: the owner is asked once.
    def listed(value, cap, size):
        return [str(x)[:size] for x in value if x][:cap] if isinstance(value, list) else []

    raw_fus = obj.get('followups') if isinstance(obj.get('followups'), list) else []
    fus = [f for f in raw_fus if isinstance(f, dict) and f.get('when') and f.get('check')
           and not NAG.search(str(f.get('check')))][:4]   # add_followups schedules at most two usable ones
    return {'status': status, 'headline': str(obj.get('headline') or '').strip()[:400],
            'details': str(obj.get('details') or '').strip()[:900], 'decision': dec, 'followups': fus,
            'learned': listed(obj.get('learned'), 6, 300),
            'moot_reason': str(obj.get('moot_reason') or '').strip()[:400],
            'limits': str(obj.get('limits') or '').strip()[:300],
            'sources': listed(obj.get('sources'), 8, 160)}


def synth_packet(opp: dict, why: str) -> dict:
    """For an item with no free work (or when work could not run): the packet is the item itself. Honest: it says
    prep was not done when that is the case."""
    steps = (opp.get('decision') or {}).get('steps') or []
    gated = [s['do'] for s in steps if s.get('autonomy') in ('offer', 'ask') and s.get('do')]
    fus = []
    for s in steps:
        if s.get('action_class') == 'remind_owner' and s.get('when') and s.get('do'):
            fus.append({'when': s['when'], 'check': s['do'], 'tell_if': 'always: this is the reminder they should get', 'reminder': True})
    dec = {'ask': gated[0], 'ready': '', 'default': ''} if gated else None
    fu = opp.get('followup') or {}
    if fu.get('reminder'):   # a reminder they are owed: the reminder text itself is the news
        return {'status': 'done', 'headline': 'Reminder: ' + str(fu.get('check') or '')[:380], 'details': str(opp.get('why') or '')[:300],
                'decision': None, 'followups': [], 'learned': []}
    return {'status': 'needs_decision' if dec else 'done', 'headline': str(opp.get('why') or opp.get('title') or '')[:400],
            'details': '', 'decision': dec, 'followups': fus[:3], 'learned': [], 'prep': why}


def build_prompt(opp: dict, mandate: dict, now_dt: datetime, prefs_text: str = '') -> str:
    steps = (opp.get('decision') or {}).get('steps') or []
    free = [s['do'] for s in steps if s.get('autonomy') == 'auto' and s.get('action_class') != 'notify_owner_now' and s.get('do')
            and not s.get('granted')]
    gated = [s['do'] for s in steps if s.get('autonomy') in ('offer', 'ask') and s.get('do')]
    extra = ''
    ok = standing_ok(opp)
    if ok:
        extra += ('Standing OK: the owner told you to handle this kind of thing every time without asking. So this time you DO carry out '
                  'the step below for real, once, exactly as written, after your own check that it is still needed and still the same '
                  'kind of thing they approved. If anything about it is different (a new amount, a new person, a new place), do not do '
                  'it: prepare it and report it as a decision for them instead. Everything else stays read-and-draft only.\n'
                  + '\n'.join(f'- {x}' for x in ok) + '\nIn your result say plainly what you did and where it can be checked.\n')
        by = (opp.get('decision') or {}).get('granted_by') or {}
        if by.get('owners_words'):
            extra += 'What they approved then: "%s". Their words: "%s"\n' % (str(by.get('approved_example') or '')[:120], str(by['owners_words'])[:200])
    fu = opp.get('followup')
    if fu:
        earlier = opp.get('parent_packet') or {}
        extra = FOLLOWUP_EXTRA.format(asked=str(opp.get('asked_at') or '')[:16], check=fu.get('check', ''),
                                      tell_if=fu.get('tell_if') or 'something changed that affects what they should do',
                                      earlier=(str(earlier.get('headline') or '') + ' ' + str(earlier.get('details') or '')).strip() or '(none)')
    if opp.get('update_of'):
        e = opp.get('earlier') or {}
        extra += ("This is news about something you already handled (it ended: %s). Earlier they were told: %s\n"
                  "Work out what actually changed. Tell them only the change, and only if it matters to them; otherwise report status \"nothing_useful\".\n"
                  % (e.get('how_it_ended') or 'closed', e.get('what_he_was_told') or e.get('why_closed') or '(nothing)'))
    extra += FR.prompt_extra(opp, now_dt)   # fresh.py: empty unless this item was sent back for fresh work (its times worded for now)
    extra += __import__('commit').prompt_line(opp)   # commit.py (switch commit-on): one plain sentence when the job was corrected or something is not confirmed; otherwise ''
    if _HEARD:
        extra += __import__('crossfence').prompt_line(opp)   # crossfence.py (switch cross-channel-fence-on): their own words about this item that no rule or note carries yet; never reached without the switch
    extra += __import__('recover').prompt_line(opp, now_dt)   # recover.py / capability.py (switches recover-on, capability-on): what an earlier run left, and what this job needs that is not available; '' with neither switch
    if (opp.get('exec') or {}).get('no_result'):
        extra += ('Your previous run on this item did not answer with the JSON object (it said: "%s"). Your answer is read by '
                  'your pipes, not sent to the owner. Answer with the JSON object this time, whatever you conclude.\n'
                  % str(opp['exec']['no_result'])[:120])
    noticed = datetime.fromtimestamp(float(opp.get('_first_seen') or time.time()), PACIFIC).strftime('%a %b %d %I:%M%p')
    return PROMPT.format(
        mandate_at=mandate.get('sent_at', ''), mandate=mandate.get('text', ''), source=opp.get('_source') or 'email or calendar',
        title=opp.get('title', ''), why=opp.get('why', ''), evidence=opp.get('evidence', ''), noticed=noticed,
        now=zone_clock(now_dt, '%a %b %d %Y %I:%M%p Pacific'), extra=extra,
        free_steps='\n'.join(f'- {x}' for x in free) or f"- {opp.get('proposed_action') or 'Work out what would help and do it.'}",
        gated_steps='\n'.join(f'- {x}' for x in gated) or '- (none)',
        prefs=('What they have told you before (respect it):\n' + prefs_text + '\n') if prefs_text else '') + __import__('writingstyle').for_job(None, opp) + _file_line()   # filedeliver: '' without the switch file file-delivery-on


def _file_line() -> str:   # filedeliver.py (switch file-delivery-on): the one sentence that tells a job how to hand a file back. '' without the switch, and on any trouble
    try:
        if (Path(os.environ.get('LIFE_HOME') or LIVE_HOME) / 'file-delivery-on').exists():
            return __import__('filedeliver').job_line()
    except Exception:
        pass
    return ''


def create_job(prompt: str, run_at: str, name: str) -> str:
    """One native one-shot job that delivers nothing ('local'): its answer is saved, the engine reads it."""
    p = subprocess.run([HERMES, '-p', settings.PROFILE, 'cron', 'create', run_at, prompt, '--name', name,
                        '--deliver', 'local', '--repeat', '1'], capture_output=True, text=True, timeout=60)
    out = p.stdout + p.stderr
    m = re.search(r'Created job:\s*([0-9a-f]{8,})', out)
    if not m:
        raise RuntimeError('job create failed: ' + out[-200:])
    return m.group(1)


def whole(text, limit: int = 140) -> str:
    """One line that is never cut inside a word: shortened at a word boundary, with a mark that says so."""
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rsplit(' ', 1)[0].rstrip(' ,;:(') + '…'


def broke_title(opp: dict, broke: list) -> str:
    """What the owner reads if an alarm about a job that broke a limit reaches them. No tool names in it."""
    private_only = bool(broke) and all(str(b).startswith('private read') for b in broke)
    what = 'read private text threads' if private_only else 'may have sent or changed something'
    return f"Zeus background job {what} while working on: {opp.get('title')}"


def _alarm(life_home: Path, key: str, title: str, kind: str = 'life_exec_failed', detail=None):
    """One repair alarm. It is appended to <live life folder>/alarms.jsonl (the health note and `zeus doctor` read it)
    and, when `alarms.command` is configured, handed to that program as one JSON object on stdin. If the alarm reaches
    the owner, they read `title` word for word, so it is plain words and whole. Raw detail (tool names, states, errors)
    goes in `detail`. -> True when it was recorded (and, with a command, the command took it), False when it could not
    be recorded or the command said no (the health note's once-a-day alarms try again on the next tick), None in a
    sandbox."""
    if Path(life_home).resolve() != LIVE_HOME.resolve():
        return None  # a sandbox never raises real repair alarms
    lines = [whole(f'Zeus life engine detail: {line}', 300) for line in ([detail] if isinstance(detail, str) else list(detail or []))[:6]]
    record = {'at': time.time(), 'kind': kind, 'where': f'life:{key}', 'title': whole(title), 'log': lines}
    try:
        with open(Path(life_home) / ALARM_FILE, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        return False
    if not ALARM_COMMAND:
        return True
    try:
        cmd = ALARM_COMMAND if isinstance(ALARM_COMMAND, list) else [str(ALARM_COMMAND)]
        done = subprocess.run(cmd, input=json.dumps(record, ensure_ascii=False), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(done, 'returncode', 0) == 0


def _job_audit(job_id: str):
    """Tripwire: what did this finished job actually do with its tools? -> the sends, changes and private reads it
    made (empty = clean), or None when the audit itself could not run: the job is then unchecked, and that is said,
    never passed off as clean. The job's instructions forbid all of these; this is how a slip gets noticed."""
    for attempt in (1, 2):
        try:
            import jobaudit
            r = jobaudit.audit(job_id)
            break
        except Exception:
            if attempt == 2:
                return None
            time.sleep(2)
    return [str(x)[:200] for x in r.get('writes', [])][:5] + ['private read: ' + str(x)[:180] for x in r.get('private', [])][:5]


def _iso(value, now_dt):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    t = (t if t.tzinfo else t.replace(tzinfo=PACIFIC)).astimezone(PACIFIC)
    return t if t > now_dt - timedelta(minutes=5) else None


def add_followups(con, parent_key: str, opp: dict, packet: dict, now_dt: datetime) -> list:
    """Schedule the later checks a job asked for, as child items. Returns the ones that really are scheduled: only
    those may be promised to the owner (a follow-up with an unreadable or past time is dropped, not promised)."""
    scheduled = []
    if not __import__('commit').result_current(con, parent_key, opp):   # commit.py (switch commit-on): later checks of a result for an older version of the job are not scheduled
        return scheduled
    ci = _ci(con=con)   # integrity.py, only with its switch (commit-integrity-on)
    if ci and not ci.still_worked(con, parent_key):   # integrity: no later check for an item they dropped or closed while its result was read
        return scheduled
    covered = __import__('watches').covers(con, parent_key, opp)   # a running monitor watches this thing; it tells them the moments, so no "check again at T" item is booked and no check time is promised. A reminder they are owed is still booked
    for i, fu in enumerate(packet.get('followups') or [], 1):
        if len(scheduled) >= 2:
            break
        if covered and not fu.get('reminder') and __import__('watches').covers(con, parent_key, opp, about=str(fu.get('check') or '')):
            continue      # only a check on the watched thing itself; a check about something else on the same item is still booked
        when = _iso(fu.get('when'), now_dt)
        if when is None:
            continue
        key = f"{parent_key}#fu{i}:{when.strftime('%m%d%H%M')}"
        fu = {**fu, 'when': when.isoformat()}
        if con.execute('SELECT 1 FROM opps WHERE key=?', (key,)).fetchone():
            scheduled.append(fu)
            continue
        reminder = bool(fu.get('reminder'))
        step = {'do': fu['check'], 'action_class': 'notify_owner_now' if reminder else 'prepare_or_research', 'autonomy': 'auto'}
        child = {'title': ('Reminder: ' if reminder else 'Follow-up: ') + str(fu['check'])[:120], 'why': str(opp.get('title') or ''),
                 'evidence': '', 'urgency': 'today', 'dedupe_key': key, 'parent': parent_key, 'followup': fu,
                 'asked_at': now_dt.isoformat(), 'parent_packet': {'headline': packet.get('headline'), 'details': packet.get('details')},
                 '_source': opp.get('_source'), 'owner_involved': 'yes', 'confidence': 1.0, 'steps': [step],
                 'decision': {'autonomy': 'auto', 'surface_at': when.isoformat(), 'channel': 'text', 'steps': [step], 'granted': False,
                              'reason': 'follow-up scheduled by code'}}
        __import__('commit').child_of(con, parent_key, opp, child)   # commit.py (switch commit-on): the check carries its parent's commitment id and version; nothing without the switch
        con.execute('INSERT INTO opps VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (key, 'pending', 'auto', when.isoformat(), 'today', step['action_class'], json.dumps(child, default=str),
                     '[]', time.time(), time.time()))   # no mail source of its own: the "they already answered" gate must not apply to a follow-up
        scheduled.append(fu)
    return scheduled


def _save(con, key, state, opp):
    guard, more = __import__('commit').save_guard(con, key, opp)   # commit.py (switch commit-on): '' and () without the switch, so the statement is the one it always was
    ci = _ci(con=con)   # integrity.py, only with its switch (commit-integrity-on)
    still = ci.save_guard(con) if ci else ''   # integrity: only an item that is still being worked is written; '' without the switch, so the statement is the one it always was
    cur = con.execute('UPDATE opps SET state=?, payload=?, updated=? WHERE key=?' + guard + still, (state, json.dumps(opp, default=str), time.time(), key) + more)
    if (guard or still) and cur.rowcount == 0:   # the job was corrected since this work began (or, with integrity on, the item was dropped or closed meanwhile): nothing was written over
        raise __import__('commit').Stale(f'{key}: this work was for an older version of the job' + (', or the item was closed while it was worked' if still else ''))


def _ready(con, key, opp, packet, now_dt, res):
    """A finished packet: moot ones close quietly, the rest wait for the composer."""
    ci = _ci(con=con)   # integrity.py, only with its switch (commit-integrity-on)
    if ci and ci.stale_line(con, key, opp, packet, now_dt, res):   # integrity: a result that states a value they corrected on a job this item is only linked to gets one more run, then that line is held; False for every other result
        return
    try:   # draftvoice.py (switch draft-voice-on): a draft in their name is checked before it is frozen; without the switch draftvoice.py is not even loaded
        __import__('draftvoice').on_packet(con, key, opp, packet)   # never raises (the draft-voice-on switch is folded in)
    except Exception:   # a fault here leaves the item on the old path
        pass
    __import__('onesend').on_ready(con, key, opp, packet)   # one-send-on: the step that needs their yes is frozen as an action record
    opp['packet'] = packet
    packet = opp['packet'] = FR.packet_ready(con, key, opp, packet, now_dt)   # fresh.py: the version it was built from; a redone ask is settled
    if __import__('glue').watch_since_work(con, key, opp, packet, now_dt, res):   # glue.py (watches-on + fresh-on): the watched page changed while this was worked
        return
    if packet['status'] in ('moot', 'nothing_useful'):
        ci = _ci(con=con)   # integrity.py, only with its switch (commit-integrity-on)
        if ci and ci.unreceipted(con, key, opp, packet, now_dt, res):   # integrity: the job saying "nothing needed" is not a receipt; the item stays open, quiet, with its deadline and later checks
            return
        opp['closed'] = {'state': 'moot', 'at': time.time(), 'by': 'worker', 'why': packet.get('moot_reason') or packet.get('headline')}
        if 'receipt' in opp:   # integrity.py: what stood behind the close (the run record and clean read-back, or the item's own time); never there without the switch
            opp['closed']['receipt'] = opp.pop('receipt')
        _save(con, key, 'moot', opp)
        res['moot'] += 1
        return
    packet['followups'] = add_followups(con, key, opp, packet, now_dt)   # only checks that are really scheduled stay in the result
    res['followups'] += len(packet['followups'])
    opp['ready_at'] = time.time()
    _save(con, key, 'ready', opp)
    res['ready'] += 1


def _close(con, key, opp, state, why, by='worker'):
    opp['closed'] = {'state': state, 'at': time.time(), 'by': by, 'why': why}
    _save(con, key, state, opp)


def _own_pipes(broke: list) -> bool:
    """Did the job run one of Zeus's own list or switch commands (zeus-life stop, mute, drop, done, add, approve,
    grant ...)? No standing OK covers those: they approved a step out in the world, never a change to Zeus's own list."""
    try:
        import jobaudit
        prefix = jobaudit.OWN_PIPES
    except Exception:
        prefix = 'own pipes command: '
    return any(str(b).startswith(prefix) for b in broke)


def _harvest_one(con, r, now_dt, life_home, mandate, can_work, prefs_text, create, runs, response, res, audit=None, dropped=None):
    opp = json.loads(r['payload'])
    ex = opp.get('exec') or {}
    rows = runs(ex.get('job_id')) if ex.get('job_id') else None
    status = rows[-1][0] if rows else None
    err = rows[-1][2] if rows else None
    if 'run_at_ts' not in ex:
        # A job record in the older format (no run_at_ts) delivered its own result when it finished. Never tell it a second time.
        if status == 'completed':
            ex.update(done_at=time.time(), delivery='by the job itself')
            opp['exec'] = ex
            _save(con, r['key'], 'done', opp)
        elif status in FAILED:
            _close(con, r['key'], opp, 'needs_attention', f'older-format job ended {status}')
        return
    text = response(ex['job_id']) if status == 'completed' else None
    packet = parse_packet(text) if text else None
    packet = __import__('onesend').with_send(con, packet, text)   # one-send-on: keep the whole reply the job wrote out
    if packet is not None and packet.get('unparsed'):
        # "[SILENT]", an apology, a half answer: not a result. A job can answer [SILENT] because it wrongly believes its
        # answer would text the owner at night; treating that as a result would drop a real open question.
        ex['no_result'] = str(text).strip()[:160]
        packet = None
    packet = __import__('recover').blocked_first(life_home, r['key'], opp, ex, packet, can_work)   # recover.py (switch recover-on): "blocked" with nowhere searched is not yet an answer; the same packet without the switch
    age = now_dt.timestamp() - float(ex.get('run_at_ts') or now_dt.timestamp())
    ci, was_running = _ci(life_home), res['running']   # integrity.py, only with its switch (commit-integrity-on). A hold below is not a running job: it does not count toward MAX_RUNNING
    if ci and not standing_ok(opp) and ci.refused_run(con, life_home, r['key'], opp, ex, bool(rows) or age > MAX_JOB_SECONDS, now_dt, res):
        return                                            # integrity: a run the job gate refused did no work: not a result, not a failure; the item is started again. Never for a standing-OK step: that one is settled by the normal harvest and its read-back, never by a second start
    xf = _xf(life_home) if ci else None   # crossfence.py, only with its switch (cross-channel-fence-on)
    if xf and xf.result_held(con, life_home, r['key'], opp, ex, bool(rows), bool(standing_ok(opp)), now_dt, res):
        res['held'] = res.get('held', 0) + 1
        return                                            # crossfence: they said something since this job was booked that bears on it: its result is not used, the item stays open and is started again with their words (below, in this same run)
    if status == 'completed' and packet is not None:
        if ci and ci.result_held(con, life_home, r['key'], ex, res):   # integrity: their stop, or the item closed or dropped while the result was read: the result is not used
            res['running'], res['held'] = was_running, res.get('held', 0) + 1
            return
        if __import__('commit').stale_result(con, life_home, r['key'], ex, packet, now_dt, res):   # commit.py (switch commit-on): a result for an older version of the job closes and rewrites nothing; the item goes back for its current version
            return
        ex.pop('no_result', None)
        ex.update(done_at=time.time())
        try:
            broke = audit(ex['job_id']) if audit else []
        except Exception:
            broke = None
        if broke is None:                                 # the audit itself could not run: the job is unchecked, and that is said
            ex['audit_skipped'] = True
            res['audit_skipped'] = res.get('audit_skipped', 0) + 1
            _alarm(life_home, r['key'], f"Zeus could not check what a background job did while working on: {opp.get('title')}", kind='life_audit_failed')
        elif broke and standing_ok(opp) and not any(str(b).startswith('private read') for b in broke) and not _own_pipes(broke):
            ex['did_under_standing_ok'] = broke           # they gave a standing OK for this kind of item: recorded, not an alarm
        elif broke:                                       # the job sent or changed something, or read their private threads
            ex['broke_a_limit'] = broke
            res['limit_breaks'] = res.get('limit_breaks', 0) + 1
            _alarm(life_home, r['key'], broke_title(opp, broke), kind='life_job_broke_limit', detail=broke)
        if ci:
            ci.granted_ran(con, life_home, r['key'], opp, ex, packet, now_dt)   # integrity: a standing-OK job's step ran, or may have: written on the item, so no second standing-OK job is booked for it (below) and the job gate refuses one
        opp['exec'] = ex
        _ready(con, r['key'], opp, packet, now_dt, res)
        __import__('watches').from_job(con, life_home, r['key'], text, broke, now_dt, res)    # a watch the job asked for (switch file watches-on): checked and booked by code
        if (Path(life_home) / 'file-delivery-on').exists():   # filedeliver.py: a file the job handed back is checked by code and recorded as prepared. Never sends. Own guard
            __import__('filedeliver').harvest(con, life_home, r['key'], text, broke, now_dt, res)
        return
    # A late one-time job is dropped by the scheduler, not run late. With no run on record (the run records were
    # readable and empty: rows == []) and the 120 seconds gone, ask whether it was dropped instead of waiting 45
    # minutes. A job that carries out a step for real (standing OK) is hurried into a second run only when the
    # scheduler itself says the first never started.
    gone = dropped(ex['job_id']) if dropped and rows is not None and not rows and age > ONESHOT_GRACE else None
    if gone in ('cut short', 'gone') and standing_ok(opp):
        gone = None
    if gone:
        ex['dropped'] = f'the scheduler dropped the job ({gone}); it was more than {ONESHOT_GRACE} seconds late'
    never_ran = (not rows and age > NEVER_RAN_AFTER) or bool(gone)
    stalled = status != 'completed' and age > MAX_JOB_SECONDS
    if status in FAILED or never_ran or stalled or (status == 'completed' and packet is None):
        if __import__('commit').stale_result(con, life_home, r['key'], ex, None, now_dt, res, why=str(err or ex.get('no_result') or status or ex.get('dropped') or 'never ran')):
            return                                        # commit.py (switch commit-on): the failed run was for an older version; the next run is for the current one and counts as the retry
        if ci and int(ex.get('tries', 1)) < 2 and not ci.may_rebook(con, life_home, r['key'], opp, ex, rows, gone, audit, res):   # integrity: no second run on what could not be read (run records, the scheduler's list, what a cut-short run did)
            res['running'], res['held'] = was_running, res.get('held', 0) + 1
            return
        can_work = __import__('recover').job_failed(con, life_home, r['key'], opp, ex, can_work, now_dt, res, status=status, rows=rows, gone=gone, stalled=stalled, err=err,
                                                    audit=audit, response=response)   # recover.py (switch recover-on): the failed run goes into the item's one attempt history and the policy is asked; it can only take the recovery run away, never add one. The same value without the switch
        tries = int(ex.get('tries', 1))
        if tries < 2 and can_work and __import__('onesend').may_retry(con, r['key'], opp, ex, audit):   # one-send-on: a standing-OK step is read back first
            if xf:
                xf.before_retry(con, life_home, r['key'], opp, now_dt, res)   # crossfence: their words bear on this item: the second run is a plain prep job, never a second standing-OK job
            claimed = None
            try:
                run_at = now_dt + timedelta(minutes=1)
                prompt, last = build_prompt(opp, mandate, now_dt, prefs_text), str(err or ex.get('no_result') or status or ex.get('dropped') or 'never ran')[:200]
                if ci:   # integrity: the second attempt is saved and committed BEFORE the scheduler is asked, and the job's name carries its mark
                    claimed = ci.claim(con, life_home, r['key'], opp, run_at, tries + 1, _claim_time(now_dt), base=ex, last_error=last, prev_job=ex.get('job_id'), **_HEARD)   # crossfence: {} without its switch
                    ex = dict(claimed)
                job = create(prompt, run_at.isoformat(), job_name(opp, retry=True) + (ci.tag(claimed) if claimed else ''))
                ex.update(job_id=job, tries=tries + 1, last_error=last, created_at=time.time(), run_at_ts=run_at.timestamp())
                if claimed:
                    ex = ci.booked(con, r['key'], claimed, ex, now_dt)
                ex = __import__('commit').exec_for(con, life_home, r['key'], opp, ex, now_dt)   # commit.py (switch commit-on): the retry job is listed on the commitment; the same dict without the switch
                opp['exec'] = ex
                _save(con, r['key'], 'executing', opp)
                res['running'] += 1
                return
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                if claimed:                               # integrity: the booking gave no answer. The claim stands; the next run reads the scheduler before anything else is done (not "failed twice", and not counted as a running job)
                    res['held'] = res.get('held', 0) + 1
                    return
        _alarm(life_home, r['key'], f"Zeus background work failed twice on: {opp.get('title')}",
               detail=f"last result: {str(err or ex.get('no_result') or status or ex.get('dropped') or 'never ran')[:200]}")
        # The owner still hears about the item, honestly, without the prep.
        _ready(con, r['key'], opp, synth_packet(opp, 'the background work failed twice'), now_dt, res)
        __import__('recover').gave_up(con, life_home, r['key'], 'job_interrupted', now_dt, res)   # recover.py (switch recover-on): the facts for the writer, and the two-hour rule; nothing without the switch
        res['failed'] += 1
        return
    res['running'] += 1


def run(con, now_dt: datetime, life_home: Path, *, prefs_text: str = '', create=None, runs=None, response=None, audit=None,
        dropped=None) -> dict:
    if audit is None:                                     # the real audit only with the real job records; a test passes its own
        audit = _job_audit if runs is None else (lambda _job_id: [])
    if dropped is None:                                   # the same for "did the scheduler drop it?"
        dropped = _dropped if runs is None else (lambda _job_id: None)
    create, runs, response = create or create_job, runs or _runs, response or _response
    create = __import__('watches').wrap_create(create, life_home)    # switch files watches-on + watches-in-job-prompt; otherwise the same function comes back
    create = __import__('onesend').wrap_create(create, life_home)   # switch file one-send-on; without it this is the same function
    res = {'started': 0, 'ready': 0, 'moot': 0, 'failed': 0, 'skipped': 0, 'followups': 0, 'over_budget': 0, 'running': 0}
    life_home = Path(life_home)
    global _STARTED
    _STARTED = time.monotonic()
    __import__('commit').fence_home(life_home)   # commit.py: the folder build_prompt's one added sentence reads its switch (commit-on) from
    off = (life_home / 'executor-off').exists()
    try:
        mandate = json.loads((life_home / 'mandate.json').read_text())
    except (OSError, ValueError):
        mandate = {}
    can_work = bool(mandate.get('active')) and not off
    if not can_work:
        res['no_work'] = 'executor-off' if off else 'no active mandate'
    ci = _ci(life_home)   # integrity.py, only with its switch (commit-integrity-on); None without it, and then nothing below calls into it
    global _HEARD
    xf = _xf(life_home) if ci else None   # crossfence.py, only with its switch (cross-channel-fence-on) on top of integrity's
    got = xf.heard(life_home) if xf else None   # taken here, right where the caller read the prompt's own text (prefs_text)
    _HEARD = {'heard': got} if got else {}
    if xf:
        xf.health(life_home, res)   # crossfence: their chat store or their preferences file cannot be read: said in this run's report (fence_unreadable) and ONE repair ticket; the gate holds meanwhile
    held = False
    if ci:                                                # integrity: a mandate file that cannot be read holds new work for a short while, not for good (their own executor-off is not a hold)
        try:
            held = not off and ci.mandate_unreadable(life_home)   # the core keeps the wait itself (integrity.MANDATE_WAIT): after it this is False again, the old rule applies (told without prep) and ONE ticket is raised
            if held:
                res['no_work'] = 'the mandate could not be read; new background work waits'
        except Exception as e:                            # trouble with the mark itself: the old rule (told without prep), never a silent wait
            res.setdefault('errors', []).append(f'mandate wait: {type(e).__name__}: {str(e)[:80]}')
    try:
        import prefs as F
        muted = F.muted_families(life_home)
    except Exception:
        muted, F = set(), None

    def is_muted(key):
        return bool(F) and bool(muted) and F.is_muted(life_home, key)

    # 1) harvest running jobs. One bad item never blocks the others.
    waiting = ci.reconcile_claims(con, life_home, _claim_time(now_dt), res) if ci else ()   # integrity: an attempt that was claimed and has no job on record is settled by READING the scheduler, before anything is harvested or booked
    for r in con.execute("SELECT key,payload FROM opps WHERE state='executing'").fetchall():
        if r['key'] in waiting:                           # integrity: its booking is not known yet: neither harvested nor booked for on this run (a hold, not a running job)
            res['held'] = res.get('held', 0) + 1
            continue
        try:
            _harvest_one(con, r, now_dt, life_home, mandate, can_work, prefs_text, create, runs, response, res, audit, dropped)
        except Exception as e:
            res.setdefault('errors', []).append(f"harvest {r['key'][:40]}: {type(e).__name__}: {str(e)[:80]}")
            try:
                opp = json.loads(r['payload'])
                _ready(con, r['key'], opp, synth_packet(opp, 'the result of the background work could not be read'), now_dt, res)
                res['failed'] += 1
            except Exception:
                pass
    con.commit()

    # 1b) things handled elsewhere (work.skip_keys) are closed at once, however far off they are, so they are neither
    #     worked twice nor shown to the chat agent as its own.
    for r in con.execute("SELECT key,payload FROM opps WHERE state='pending'").fetchall():
        if any(k in r['key'].lower() for k in SKIP_KEYS):
            try:
                _close(con, r['key'], json.loads(r['payload']), 'moot', 'this is handled elsewhere (work.skip_keys); Zeus stays out of it')
                res['skipped'] += 1
            except Exception as e:
                res.setdefault('errors', []).append(f"skip {r['key'][:40]}: {type(e).__name__}: {str(e)[:80]}")
    con.commit()

    # 2) start work for items that are coming due; items with no free work become ready as they are
    day0 = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    started_today = con.execute("SELECT COUNT(*) FROM opps WHERE json_extract(payload,'$.exec.created_at')>=?", (day0,)).fetchone()[0]
    horizon = (now_dt + PREP_LEAD).isoformat()
    now_iso = now_dt.isoformat()
    due = con.execute("SELECT key,payload,surface_at,urgency,first_seen,sources FROM opps WHERE state='pending' AND autonomy IN ('auto','offer','ask') "
                      "AND surface_at<=? ORDER BY urgency='now' DESC, surface_at", (horizon,)).fetchall()
    for r in due:
        try:
            opp = json.loads(r['payload'])
            opp['_first_seen'] = r['first_seen']
            due_now = r['surface_at'] <= now_iso
            if any(k in r['key'].lower() for k in SKIP_KEYS):
                res['skipped'] += 1   # handled elsewhere: closed at once, so it is neither worked twice nor shown as Zeus's
                _close(con, r['key'], opp, 'moot', 'this is handled elsewhere (work.skip_keys); Zeus stays out of it')
                continue
            if is_muted(r['key']):
                _close(con, r['key'], opp, 'declined', 'muted kind: they said to stop raising this')
                continue
            if opp.get('followup') and not due_now:
                continue  # a follow-up check runs at its time, not before
            if opp.get('followup') and not (opp.get('followup') or {}).get('reminder') and __import__('watches').covers(con, r['key'], opp, about=str((opp.get('followup') or {}).get('check') or '')):
                _close(con, r['key'], opp, 'moot', 'a monitor now watches this and tells them the moments; the check was booked before it started')
                continue
            if not has_free_work(opp):
                if due_now:
                    _ready(con, r['key'], opp, synth_packet(opp, 'no free work was needed'), now_dt, res)
                continue
            if not can_work:
                if held:                                  # integrity: the mandate could not be read. The work waits (for MANDATE_WAIT at most); nothing is told without its prep on the strength of a file that could not be read
                    continue
                if due_now:
                    _ready(con, r['key'], opp, synth_packet(opp, 'background work is switched off'), now_dt, res)
                continue
            if opp.get('suspicious_mail'):
                # The email has text written to steer an assistant (screen.py). They are still told about the item, but no
                # tool-holding job is started from it.
                res['screened'] = res.get('screened', 0) + 1
                if due_now:
                    _ready(con, r['key'], opp, synth_packet(opp, 'no background work: the email has text aimed at an assistant'), now_dt, res)
                continue
            if __import__('recover').after_hold(con, life_home, r['key'], opp, now_dt, res, __import__('signedin').hold(con, life_home, r, opp, now_dt, res)):    # switch file signin-check-on: the site's sign-in has lapsed, so the errand waits. recover.py (switch recover-on) writes the hold, the give-up or the pick-up into the item's history and hands back signedin's own answer; without its switch it only hands that answer back
                continue
            if started_today >= MAX_WORK_PER_DAY:
                res['over_budget'] += 1
                tell_now = r['urgency'] == 'now' or opp.get('action_class') == 'notify_owner_now'   # news they are waiting on is told without the prep, as this file always said; only prep work waits for the budget
                if due_now and (tell_now or not __import__('recover').stay_pending(con, life_home, r['key'], opp, now_dt, res)):   # recover.py (switch recover-on): it waits for the budget to reset or its deadline to near; False without the switch
                    pk = synth_packet(opp, 'over the daily work budget')
                    if tell_now and pk.get('prep'):
                        pk.update(decision=None, status='done')   # nothing was prepared, so they get the fact and are asked nothing
                    _ready(con, r['key'], opp, pk, now_dt, res)
                continue
            if res['running'] >= MAX_RUNNING:
                continue  # next tick
            if xf and not xf.before_start(con, life_home, r['key'], opp, now_dt, res):   # crossfence: their words bear on an item with a step under a standing OK: the standing OK comes off this item first, so the job booked below is a plain prep job; False = their messages could not be read, so nothing that acts for real is booked on this run
                continue
            if ci and not ci.may_start(con, life_home, r['key'], opp, res):   # integrity: their stop, the item's state, its current version and the permission for this run are read again; a check that fails or cannot be made holds this item only
                continue
            if ci and standing_ok(opp) and ci.acted_before(opp):
                # integrity: a standing-OK job already ran this step for this item (or may have). Whatever sent the item back (a rework
                # after they said something, a changed source, a restart), the step is never started a second time: they are told plainly, without it being done again
                _ready(con, r['key'], opp, synth_packet(opp, 'not started again'), now_dt, res)
                continue
            if not __import__('onesend').may_start(con, r['key'], opp, now_dt, res):   # one-send-on: a standing-OK step is marked before its job is booked
                continue
            if ci and standing_ok(opp) and isinstance(opp.get('dispatch_last'), dict):
                # integrity: an earlier attempt at this standing-OK step was claimed and its job was never found (or its run was refused). Whether it did
                # anything is not known for sure, so the step is never started a second time: they are told plainly, without the step being done
                _ready(con, r['key'], opp, synth_packet(opp, 'not started again'), now_dt, res)
                continue
            run_at = now_dt + timedelta(minutes=1)
            claimed = None
            try:
                prompt = build_prompt(opp, mandate, now_dt, prefs_text)
                if ci:   # integrity: the attempt is saved and committed BEFORE the scheduler is asked (raises commit.Stale, writing nothing, when the job was corrected since this run read it). Its time is the real moment of the claim
                    claimed = ci.claim(con, life_home, r['key'], opp, run_at, 1, _claim_time(now_dt), **_HEARD)   # crossfence: {} without its switch
                job = create(prompt, run_at.isoformat(), job_name(opp) + (ci.tag(claimed) if claimed else ''))   # integrity: the job's NAME carries the attempt's mark (' [a_<12 hex>]'); nothing without the switch
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                res.setdefault('errors', []).append(str(e)[:120])
                continue                                  # integrity: with a claim saved it stands, and the next run reads the scheduler before it books anything for this item
            ex = {'job_id': job, 'created_at': time.time(), 'run_at_ts': run_at.timestamp(), 'tries': 1}
            if claimed:
                ex = ci.booked(con, r['key'], claimed, dict(ex, tries=claimed.get('tries', 1)), now_dt)   # integrity: the attempt names its job (a run after a stale result still counts as the one retry)
            opp['exec'] = __import__('commit').exec_for(con, life_home, r['key'], opp, ex,
                                                        now_dt)   # commit.py (switch commit-on): the job carries the commitment's id and version; the same dict without the switch
            FR.work_began(life_home, opp, r['sources'], now_dt)   # fresh.py: what the source looks like as this work begins
            _save(con, r['key'], 'executing', opp)
            con.commit()
            started_today += 1
            res['started'] += 1
            res['running'] += 1
        except Exception as e:
            res.setdefault('errors', []).append(f"start {r['key'][:40]}: {type(e).__name__}: {str(e)[:80]}")
    con.commit()
    return res

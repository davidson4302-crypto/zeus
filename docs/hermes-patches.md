# Hermes patches

Zeus runs inside [Hermes Agent](https://github.com/NousResearch/hermes-agent). A few of Zeus's safety
features need small changes inside Hermes itself. Until those changes are part of Hermes, Zeus ships
them as patch files and applies them to your Hermes install for you.

- Patch files: `src/zeus_ai/hermes_patches/*.patch` (plain `git diff` files)
- Order, purpose and which ones are required: `src/zeus_ai/hermes_patches/series.json`
- The tool that applies them: `src/zeus_ai/hermes_patch.py`

All patches were made against Hermes commit `7793ed0da9`. Each patch changes its own lines, so you
can apply any subset in the listed order.

## Using the patch tool

```bash
python -m zeus_ai.hermes_patch status ~/.hermes/hermes-agent   # what is applied
python -m zeus_ai.hermes_patch check  ~/.hermes/hermes-agent   # would apply work? changes nothing
python -m zeus_ai.hermes_patch apply  ~/.hermes/hermes-agent   # apply what is missing
python -m zeus_ai.hermes_patch revert ~/.hermes/hermes-agent   # undo, newest first
```

Add `--required-only` to apply only the two safety patches, or `--only 01-quiet-hours-deliver-gate`
to pick patches by name. Add `--json` for the full result.

How it stays safe:

- **Nothing changes unless everything fits.** Before touching a file, every selected patch is checked.
  If one of them does not fit, nothing is changed and the result names the patch and the file.
- **Running it twice is harmless.** A patch that is already applied is detected and skipped.
- **Your own edits are never merged over.** If a patch does not fit and the files it touches have
  uncommitted changes, it stops and lists those files.
- **A different Hermes version is handled carefully.** On a git checkout at another commit, patches
  that still fit apply normally. The others are tried with a 3-way merge (git combines the patch with
  your version using the original as a reference), but only if that merge would succeed with no
  conflicts. A folder that is not a git checkout gets exact matches only.
- **A failed run undoes itself.** If a patch fails part-way through `apply`, the patches already
  applied in that run are reverted.

Hermes updates can drop these changes. Run `status` after every Hermes update.

---

## What each patch does

### 01 Quiet-hours delivery gate (required, safety)

**What it does.** Adds an optional "delivery gate" for scheduled job results and for messages the
agent sends with `send_message`. When a profile turns it on (`cron.deliver_gate: true`), Hermes asks
the profile's plugins, through a new `pre_deliver` hook, whether this message may go now. A plugin
answers "send now" or "hold until time T". A held message is parked whole (text, attachments and
destination) in the existing delivery queue database, and sent exactly once when its time comes.

**Why Zeus needs it.** Zeus promises quiet hours: nothing reaches your phone at night unless it is
urgent. Without this patch, a scheduled job or an agent-sent message has no way to wait. Zeus's
`life-work-guard` plugin answers the hook with the end of your quiet hours.

**Safety details.** The gate is off unless the profile's config turns it on, so other profiles behave
exactly as before. If the gate is on but no plugin answers (or the plugin crashes), the message is
held and asked about again five minutes later. A late message can be recovered; a sent one cannot.
Held messages go through the existing queue, so the queue's "at most once" rule and cancellations
still apply.

**Draft upstream pull request**

> **Add an opt-in `pre_deliver` hook and `cron.deliver_gate` to hold deliveries**
>
> Some deployments need to delay outbound delivery (quiet hours, business hours, rate windows)
> without dropping or duplicating it. This adds:
>
> - a `pre_deliver` plugin hook, called with `job`, `content`, `for_failure` and `surface`
>   (`"cron"` or `"send"`). A plugin returns `{"deliver_after": <epoch seconds>}` to hold, or
>   `{"open": True}` to send now. The latest hold wins.
> - `cron.deliver_gate` (default off). When it is off, nothing changes and the hook is never called.
> - a `delivery_holds` table in the existing delivery queue database. A held delivery is stored
>   before anything is claimed or sent, and re-enters the normal queue through `enqueue` when due,
>   so idempotency, tombstones and cancellation all keep working.
> - `send_message` uses the same gate, and returns `held: true` with the hold id instead of sending.
>
> Fail-closed: with the gate on and no usable answer, the delivery is held and re-asked after
> `HOLD_RECHECK_SECONDS`. Tests: `tests/cron/test_deliver_gate.py`.

### 02 Helper-result delivery identity (required, bug fix)

**What it does.** When a background helper (a delegated task or a watched process) finishes, Hermes
sends its result into the chat. On chat lines with durable delivery, each send needs a unique
identity so it is never sent twice. Helper results had no message id, so every one of them in a chat
got the *same* identity. The first was delivered; every later one was refused as a duplicate.
The patch gives each helper result its own stable identity.

**Why Zeus needs it.** Zeus hands long jobs to background helpers so the chat stays free. Without
this fix, only the first helper result ever reaches you.

**Draft upstream pull request**

> **Give synthetic completion events their own delivery obligation id**
>
> On strict (durable) transports the obligation id ignores the text, so an event with no message id
> and no ledger id hashes to `sha(session||)`: one id shared by every background completion in that
> chat. The first owns the journal row and later ones fail with "payload changed".
>
> The completion notifier now stamps each synthetic event with its producer's stable id
> (`process:<id>`), and `send_final_ledgered` falls back to that, or to a fresh per-event id, when the
> strict id would otherwise be empty. Normal replies to inbound messages keep their existing id, so
> existing ledger rows stay valid. Tests: `tests/gateway/test_completion_producer_ref.py`,
> `tests/gateway/test_synthetic_final_identity.py`.

### 03 Ghost-turn watchdog fix (optional, bug fix)

**What it does.** The gateway watchdog cleans up "ghost" turns: work that looks stuck. It treated a
turn whose session row was not yet written as "closed", so a brand-new turn could be flagged at age
zero. The patch counts a turn as closed only when its row exists and says it ended. Truly stuck turns
are still caught by the idle and wall-clock rules.

**Draft upstream pull request**

> **Watchdog: a missing session row is "not written yet", not "closed"**
>
> `_ghost_reason` used the routing store's `_is_session_ended_in_db`, which treats a missing row as
> ended. That is right for routing, but a just-admitted turn has its session id in the routing index
> before `run_agent` inserts the row, so the watchdog flagged live turns as `session_closed` at age 0.
> `_session_row_ended` now reads the owning profile's database and requires an existing row with an
> `end_reason`. Stuck slots with no row still fall through to the idle and wall-clock checks.
> Tests: `tests/gateway/test_watchdog_ghost_turns.py`.

### 04 Busy note per profile (optional)

**What it does.** When you text an agent that is still working, Hermes can send a short "still on
your last message" note. That was one switch for the whole gateway (an environment variable). The
patch makes it a normal per-profile display setting, `display.busy_ack_enabled`, which can also be
set per platform. When unset, the old environment variable still decides.

**Why Zeus needs it.** Zeus folds your follow-up into the work quietly instead of sending a busy note.

**Draft upstream pull request**

> **Make busy acknowledgements a per-profile display setting**
>
> `HERMES_GATEWAY_BUSY_ACK_ENABLED` is process-wide, so on a multi-profile gateway one profile cannot
> opt out. This adds `display.busy_ack_enabled` (and its per-platform form), resolved from the admitted
> event's own profile, with the environment variable as the fallback. If the profile cannot be
> resolved, the note is suppressed rather than borrowing another profile's setting. Queued input and
> the final reply are unchanged. Tests: `tests/gateway/test_busy_ack_profile_settings.py`.

### 05 Quiet acknowledgements (optional)

**What it does.** When you text just "ok", "thanks" or a thumbs-up, there is nothing to answer. If the
agent stayed quiet, Hermes used to send a warning that the model returned no reply. The patch marks a
bare acknowledgement as "no reply expected", so silence is fine there. It also rewords that warning,
for the cases where it still appears, to plain language: "Sorry, my reply got lost there. Send that
again and I'll answer."

**Draft upstream pull request**

> **Allow silence on a bare acknowledgement; plain-language unexpected-silence notice**
>
> A message that is only a short closing acknowledgement ("got it", "ok thanks", "👍") now sets
> `reply_expected = False` before the turn runs (not for internal events or messages with media), so
> a silence marker on it is not reported as an error. The `unexpected_silence` notice is reworded for
> end users. Tests: `tests/gateway/test_ack_silence.py`, updated `test_active_turn_recovery.py`.

### 06 Reasoning effort per profile and job type (optional)

**What it does.** Lets a profile choose how hard the model thinks for different kinds of work:

- `cron.reasoning_effort`: a lower (cheaper) effort for scheduled jobs that use the cron model, while
  chat keeps its own effort. A fallback to a different model keeps that model's effort.
- A background review can use a separate effort without disturbing the main chat's cached prompt
  (`inherit_parent_reasoning: false`).
- The Grok 4.7 effort opt-in (`agent.grok47_reasoning_effort`) is read from the current profile, so
  scheduled and delegated work on that profile honor it too.

**Draft upstream pull request**

> **Per-profile reasoning effort for cron, background review and delegated xAI work**
>
> - `cron.reasoning_effort` applies to jobs on `cron.model` only; an explicit job effort still wins,
>   and an invalid value logs a warning and falls back to the agent config.
> - `_resolve_review_runtime` honors `inherit_parent_reasoning: false` for a same-model review by
>   taking the routed (cold) branch, leaving the parent's warm prompt untouched.
> - The xAI Responses transport reads `agent.grok47_reasoning_effort` from the active profile when the
>   private resolver marker is absent (task-specific parsers), so the opt-in no longer depends on
>   which code path built the request.
> Tests: `test_cron_default_reasoning.py`, `test_background_review_separate_effort.py`,
> `test_grok_effort_profile_scope.py`.

### 07 Work model tiers (optional, not used by the default Zeus config)

**What it does.** An opt-in setting, `agent.work_model_tiers`, with up to three routes: `lookup`,
`workflow` and `recovery`, each a model, provider and effort you configure. Hermes reads what a turn,
scheduled job or delegated task asks for and picks a route: a quick lookup, a multi-step workflow, or
debugging and recovery. After real tool calls complete, a turn can move up a tier (never down, never
to another provider). Your words pick a category only, never a model, account or permission. An
explicit model choice (session, channel, command line) always wins.

**Draft upstream pull request**

> **Opt-in work model tiers for chat, cron, one-shot and delegated tasks**
>
> Adds `agent/work_model_tiers.py` and wires it into the gateway turn runner, CLI chat and single-query
> modes, `oneshot`, cron agent setup and `delegate_task`. With no `agent.work_model_tiers` configured,
> every path is unchanged. Each tier resolves through the normal runtime resolver and its own
> fallback chain. Promotion after tools reads only completed tool receipts in the current turn, and
> never retries, rewrites or invokes anything. Explicit session, channel, CLI and job overrides are
> honored. Tests: `tests/agent/test_work_model_tiers.py`.

### 08 Photon iMessage: tapbacks, link bubbles, status stickers, stream refresh (optional)

**What it does.** Several improvements to the Photon (iMessage) platform, each off unless the
profile's `platforms.photon.extra` turns it on:

- **Tapbacks that mean something** (`reactions: meaning`). 👀 while the agent works, ❤️ for a thanks,
  👍 for a plain "ok", nothing on a hello. At the end: ✅ only when a to-do was really closed this turn,
  ⏰ only when a reminder was set *and read back*, both proven from saved records, never from the
  model's words. A reply that only says "Got it" is sent as the 👍 tapback instead of a text. While a
  background helper works on your ask, the 👀 stays until its result arrives. Optional:
  `tapback_names` (names you might add, "thanks Sam") and `tapback_life_cli` (default `zeus-life`).
- **Link bubbles** (`link_bubbles: true`). A link at the end of a line is sent as its own message so
  iMessage shows a preview card. No words are dropped or reordered.
- **Status stickers** (optional). Small stickers such as "Need your OK", "Pick one", "Reminder set" or
  "Price dropped" on your message, placed only from saved records and never during quiet hours.
  They stay off unless a settings file at `~/.config/photon-status-stickers/<project>-<port>.json`
  turns them on. A missing or broken sticker bundle never stops the iMessage connection.
- **Quiet a recipient's automatic tapbacks** on a shared line (`automatic_reactions_ignored_users`).
- **Stream refresh.** A long-quiet inbound stream is refreshed in place instead of restarting the
  whole connection, and outbound sends stay available. The quiet check moves from 10 minutes to
  6 hours. Probe ids use Spectrum's `spc-msg-` id format.

**Draft upstream pull request**

> **Photon: meaningful tapbacks, link bubbles, optional status stickers, in-place stream refresh**
>
> - `reactions: meaning` adds a third reaction mode beside off and on. Opening tapbacks come from the
>   user's words; final tapbacks (✅, ⏰) need durable evidence read from state.db and the profile's
>   life-engine ledger, read-only. An acknowledgement-only final reply becomes a 👍 tapback and the
>   delivery ledger row is marked delivered.
> - `link_bubbles: true` sends line-ending links as separate messages; each bubble has its own
>   delivery component id so a redelivery skips bubbles already accepted. Never applied to a
>   verified handoff send.
> - Status stickers live in `sidecar/status-stickers/`, load lazily, and are off without an explicit
>   per-project settings file.
> - `automatic_reactions_ignored_users` quiets automatic tapbacks for listed senders; deliberate
>   reactions are unaffected.
> - The zombie-stream watchdog refreshes the provider subscription (`refreshable-stream.mjs`) instead
>   of exiting for a restart, since a successful probe cannot tell a half-open stream from a quiet one.
> Tests: `test_meaning_tapbacks.py`, `test_link_bubbles.py`, `test_helper_eyes.py`, updated
> `test_reactions.py` and `test_zombie_stream_watchdog.py`, `sidecar/refreshable-stream.test.mjs`.

---

## Not included

- Changes to the Slack platform (a shared-room feature) are not part of Zeus and are not shipped.

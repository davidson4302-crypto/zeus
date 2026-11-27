# Hermes patches

Zeus runs inside [Hermes Agent](https://github.com/NousResearch/hermes-agent). A few of Zeus's safety
features need small changes inside Hermes itself. Until those changes are part of Hermes, Zeus ships
them as patch files and applies them to your Hermes install for you.

- Patch files: `src/zeus_ai/hermes_patches/*.patch` (plain `git diff` files)
- Order, purpose and which ones are required: `src/zeus_ai/hermes_patches/series.json`
- The tool that applies them: `src/zeus_ai/hermes_patch.py`

**Hermes version.** The patches are made against Hermes **v0.23.4** (commit `8fc0374447`), the
release the Hermes installer's default `stable` channel installs. To get exactly that version:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --commit 8fc0374447313e276f6b9a2764bfc750190c966a
```

Each patch changes its own files, so you can apply any subset in the listed order.

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
It also adds `cancel_execution`, which stops a queued or held delivery before it goes out.

**Why Zeus needs it.** Zeus promises quiet hours: nothing reaches your phone at night unless it is
urgent. Zeus's `life-work-guard` plugin answers the hook with the end of your quiet hours, and
Zeus's work tracker cancels a held reply it no longer needs.

**Safety details.** The gate is off unless the profile's config turns it on, so other profiles behave
exactly as before. If the gate is on but no plugin answers (or the plugin crashes), the message is
held and asked about again five minutes later. A late message can be recovered; a sent one cannot.
Held messages go through the existing queue, so its "at most once" rule and tombstones still apply.

**Draft upstream pull request**

> **Add an opt-in `pre_deliver` hook and `cron.deliver_gate` to hold deliveries**
>
> Some deployments need to delay outbound delivery (quiet hours, business hours, rate windows)
> without dropping or duplicating it. This adds:
>
> - a `pre_deliver` plugin hook, called with `job`, `content`, `for_failure` and `surface`
>   (`"cron"` or `"send"`). A plugin returns `{"deliver_after": <epoch seconds>}` to hold, or
>   `{"open": True}` to send now. The latest hold wins. Listed in the hook table in the plugin guide.
> - `cron.deliver_gate` (default off). When it is off, nothing changes and the hook is never called.
> - a `delivery_holds` table in the existing delivery queue database. A held delivery is stored
>   before anything is claimed or sent, and re-enters the normal queue through `enqueue` when due,
>   so idempotency and tombstones keep working.
> - `send_message` (`gateway/outbound/send.py`) uses the same gate and returns `held: true` with the
>   hold id instead of sending.
> - `cancel_execution(execution_id, reason)`: suppresses a pending delivery, tombstones one not yet
>   queued and drops its hold; a claimed send keeps its real outcome.
>
> Fail-closed: with the gate on and no usable answer, the delivery is held and re-asked after
> `HOLD_RECHECK_SECONDS`. Tests: `tests/cron/test_deliver_gate.py`.

### 02 Quiet acknowledgements (optional)

**What it does.** Two per-profile display settings, both off by default:

- `display.quiet_acknowledgements: true`. When you text just "ok", "thanks" or a thumbs-up there is
  nothing to answer, so the agent may stay quiet without Hermes warning that no reply came back.
- `display.unexpected_silence_notice: "<your wording>"`. Replaces the wording of that warning, for
  the cases where it still appears (for example "Sorry, my reply got lost there. Send that again.").

**Draft upstream pull request**

> **Opt-in silence on a bare acknowledgement; configurable unexpected-silence notice**
>
> With `display.quiet_acknowledgements: true`, a message that is only a short closing acknowledgement
> ("got it", "ok thanks", "👍") is marked `reply_expected = False` before the turn runs (not for
> internal events or messages with media), so a silence marker on it is not reported. With
> `display.unexpected_silence_notice` set, that string replaces the localized notice. Both default
> off; locale catalogs are unchanged. Tests: `tests/gateway/test_ack_silence.py`.

### 03 Reasoning effort per job type (optional)

**What it does.** Lets a profile choose how hard the model thinks for different kinds of work:

- `cron.reasoning_effort`: a lower (cheaper) effort for scheduled jobs that use the cron model, while
  chat keeps its own effort. A fallback onto a different model keeps that model's effort.
- A background review can use a separate effort without disturbing the main chat's cached prompt
  (`inherit_parent_reasoning: false`).

**Draft upstream pull request**

> **cron.reasoning_effort and a separate background-review effort**
>
> - `cron.reasoning_effort` applies to jobs on `cron.model` only; an explicit job effort still wins,
>   and an invalid value logs a warning and falls back to the agent config.
> - `_resolve_review_runtime` honors `inherit_parent_reasoning: false` for a same-model review by
>   taking the routed (cold) branch, leaving the parent's warm prompt untouched.
> Tests: `test_cron_default_reasoning.py`, `test_background_review_separate_effort.py`.

### 04 Photon iMessage: tapbacks, link bubbles, status stickers, stream refresh (optional)

**What it does.** Several improvements to the Photon (iMessage) platform, each off unless the
profile's `platforms.photon.extra` turns it on:

- **Tapbacks that mean something** (`reactions: meaning`). 👀 while the agent works, ❤️ for a thanks,
  👍 for a plain "ok", nothing on a hello. At the end: ✅ only when a to-do was really closed this turn,
  ⏰ only when a reminder was set *and read back*, both proven from saved records, never from the
  model's words. A turn's only reply that just says "Got it" is sent as the 👍 tapback instead of a
  text. While a background helper works on your ask, the 👀 stays until its result arrives. Optional:
  `tapback_names` (names you might add, "thanks Sam") and `tapback_life_cli` (default `zeus-life`).
  The `reactions` value may also come from `PHOTON_REACTIONS` as before.
- **Link bubbles** (`link_bubbles: true`). A link at the end of a line is sent as its own message so
  iMessage shows a preview card. No words are dropped or reordered.
- **Status stickers** (optional). Small stickers such as "Need your OK", "Pick one", "Reminder set" or
  "Price dropped" on your message, placed only from saved records and never during quiet hours.
  They stay off unless a settings file at `~/.config/photon-status-stickers/<project>-<port>.json`
  turns them on. Zeus setup never writes that file, so they are **off by default**, and they should stay
  off on public Hermes v0.23.4: they read a per-send delivery journal and an owner-admission record that
  this Hermes version does not write, so they would never place a sticker. A missing or broken sticker
  bundle never stops the iMessage connection.
- **Quiet a recipient's automatic tapbacks** on a shared line (`automatic_reactions_ignored_users`).
- **Stream refresh.** A long-quiet inbound stream is refreshed in place instead of restarting the
  whole connection, and outbound sends stay available. The quiet check moves from 10 minutes to
  6 hours. Probe ids use Spectrum's `spc-msg-` id format.

**Draft upstream pull request**

> **Photon: meaningful tapbacks, link bubbles, optional status stickers, in-place stream refresh**
>
> - `reactions: meaning` adds a third reaction mode beside off and on, readable from
>   `platforms.photon.extra.reactions` or `PHOTON_REACTIONS`. Opening tapbacks come from the user's
>   words; final tapbacks (✅, ⏰) need durable evidence read from state.db and the profile's
>   life-engine ledger, read-only. A turn whose only reply is an acknowledgement answers with a 👍.
> - `link_bubbles: true` sends line-ending links as separate messages through `send_chunks`, so a
>   failure part-way is a partial delivery and nothing already delivered is re-sent.
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

- The maintainer's own Hermes also carries a larger set of local changes (a shared conversation across
  surfaces, a per-send delivery journal, human-request priority, reviewed cron retries and more).
  Several Zeus plugins call into those; on v0.23.4 those calls are missing, so those plugin paths
  need further work before Zeus runs fully on public Hermes.
- Work model tiers, a per-profile busy note and a watchdog fix from the earlier set are not needed on
  this version (not used by Zeus, already in Hermes, or the code they fixed is not in Hermes).
- Photon polls and the Slack shared-room work are not part of Zeus.

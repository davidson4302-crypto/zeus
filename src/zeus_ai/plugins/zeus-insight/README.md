# Zeus preparation and learning support

A native Hermes general plugin. It keeps **derived procedural guidance**, source receipts, candidate ideas and artifact checks. Personal facts and corrections stay in the retained unified-memory provider. Durable preparation stays in `zeus_work`. Intentionally enabled general routines use native cron. The plugin owns no model loop, sender, scanner or separate scheduler.

Dependencies: `aaa-zeus-scope` must register `zeus_scope` first; `zeus-work` supplies outcome proof and sourced derived preparation. Native registration uses `register(ctx)`, supported turn/tool/output hooks, tool-execution middleware, `ctx.dispatch_tool`, and `plugins.plugin_storage.plugin_db`. Every call resolves the active native profile home. Required native tools are `zeus_work`, `skill_manage`, `skills_list`, `skill_view`, and `cronjob_manage` when the corresponding feature is used.

Suggested profile settings, applied only by the integration owner:

```yaml
plugins:
  entries:
    zeus-work:
      settings:
        derived_preparation:
          enabled: true
          policy_id: <stable owner-approved goal ID>
          source_ref: <actual approval audit reference>
          timezone: <owner timezone, e.g. America/New_York>
          max_per_day: 3
    zeus-insight:
      enabled: true
      settings:
        artifact_roots:
          - <actual active profile>/workspace
          - <actual active profile>/plugin-data/zeus-work/artifacts
        quiet_review:
          enabled: true
          timezone: <optional; defaults to owner.timezone>
skills:
  create_dir: <actual active profile>/learned-skills
```

The work artifact directory is `<profile>/plugin-data/zeus-work/artifacts`. Empty allowed roots fail closed. Installation folder: `<profile>/plugins/zeus-insight`. Derived state: `<profile>/plugin-data/zeus-insight/data.db`. Native skill creation must resolve inside this profile; a shared or symlinked global skills root is rejected. The plugin does not install itself or rewrite profile configuration.

`zeus_insight` actions:

| Action | Meaning |
|---|---|
| sources | Native observed source receipts in this session, without source contents. Each includes native session/call reference, hash and time. |
| propose | Source-linked idea, lesson, briefing, feed item, memory-review suggestion or routine. Rank is explicitly `2*relevance + feasibility + effort_saved`, each 0..5, with the reason visible. |
| list/get | Fresh derived items; query matching is transparent word overlap. Listing or reading is never positive feedback. |
| transition | Accepted, dismissed, expired, adopted or retired. Acceptance/adoption links a current authenticated owner quote; its interpretation remains the agent's, not an oracle judgment. |
| artifact | Check actual bounded UTF-8 file, required content and all source IDs. Save its hash. This is mechanical verification, not independent quality judgment. |
| attention | Fresh checked artifact plus mute/window/repetition policy. Returns eligibility, never delivery or authorization. A current explicit result request may bypass proactive mute for this reply. |
| policy | Suggestions wait through quiet hours (`quiet_hours.start` to `quiet_hours.end`, default 9 p.m. to 9 a.m.) on the owner’s local clock, including requested replies and deadline warnings. A current instruction quote can change the topic mute or deadline label. |
| work_plan | Suggested `zeus_work create` payload for an accepted insight. It is not dispatched; native work must independently bind owner authority and the actual full completion criteria. |
| use/outcome | Record lesson application while native work is active; later require same revision completed, current artifact hashes and independent source evidence after use. No causal improvement claim. |
| promote | An adopted lesson with successful outcomes on two distinct work IDs becomes a versioned reusable procedure. |
| package_skill / skill_status / retire_skill | Recheck prior work, create only a new owned profile-local native skill, verify discovery and exact file through native list/view, or explicitly retire just that unchanged generated skill. Original procedure/provenance remain. Discovery is not successful execution. |
| quiet_reviews / quiet_review_start | Inspect bounded substantial-signal intents or reconcile their exact `zeus_work create_derived` assignment and native continuation. Intent, scheduled work and performed review are separate. |
| routine_plan | Prepare schedule/window/delivery without enabling it. |
| routine_enable / routine_pause / routine_resume / routine_status | Explicitly enable a current accepted routine through native paused create, exact reconciliation and resume; persist pause before revoking its native fire owner; inspect exact canonical occurrences and final/delivery receipts. No duplicate scheduler or blind recreate after uncertainty. |
| routine_result | Privately retrieve a final by matching its exact occurrence, recorded response hash and native persisted assistant message. Native execution and destination delivery remain separate. |
| audit | Return the selected item's source-linked lifecycle events, privately. |

Lessons require the exact current admitted native correction quote. Only the quote hash is persisted. Provenance stays explicit: native CLI/TUI input is `local_operator_turn` with no claimed human author; an exactly proved Desktop submit is `desktop_submit_turn`; an accepted private remote owner turn is `owner_turn`. A local operator compatibility test is not proof that the owner supplied a new human correction. Adopted relevant procedural guidance may appear in private current-turn context, with source IDs and an optional native skill name. The native skill is a generic wrapper that retrieves the current scoped procedure; it copies no private guidance into a public skill index or preload. Native `skill_view` of owned learned skills is also gated against public, expired or retired access. Context selection is bounded to two matches. Static guidance is 604 characters.

All reads and writes require the shared native scope gate. No separately maintained audience allowlist exists. Remote sessions without an accepted private owner lease fail closed. Raw tool results, source user text, personal facts and copied credentials are not source-receipt fields. Do not put personal facts into `guidance`; use retained memory.

Expiry is mandatory: feed items at most seven days, briefings thirty days, other items one year. Identical normalized title/kind/scope is deduplicated even when dismissed. This is exact token normalization, not semantic novelty detection. An old terminal proposal cannot be revived by a repeated request.

After a substantive private turn with an observed source-bound parent work record, one quiet-review intent may be created and routed to `zeus_work create_derived` plus its native continuation. It is bounded to one per parent revision and three per local day. Original authority remains a parent reference; the trigger is explicitly internal, never a fabricated user. Derived work cannot create grandchildren, outside effects or notifications. The work engine requires the sourced root approval policy above. A pending intent or scheduled timer never counts as performed review.

Routines require an accepted current proposal, an explicit current enable instruction, native schedule, `input_window_hours` from 1 to 720 and `delivery: local|origin`. Default output stays local. `origin` additionally requires a current verified private remote chat and explicit delivery quote; arbitrary historical/broadcast destinations are unavailable. A read-only native script binds each claimed occurrence and computes its input window from the scheduled instant. Native continuation history supports non-repetition. Pause closes the saved gate before pausing/revoking the scheduler's fire claim. Native middleware and output transform refuse later work/returns after pause, expiry, contract change or lost occurrence. Already-started transports remain uncertain rather than being resent.

There is no automatic hourly Feed, nightly study clock, daily skill writer, or opaque profile-image daemon. A current authorized research assignment can produce a briefing through native work. A native final mentioning a checked artifact records `return_prepared`; transport delivery remains separate. Silence never becomes positive feedback or another send.

Verification: the plugin's engine, protocol and extension tests (when present). These use temporary artifacts/databases and fake native adapters. They cover source/learning state, real byte checks, scope protocol, topic/urgency, bounded quiet delegation, native adapter arguments, private skill discovery/retirement, routine admission/pause and exact final-history hash matching. None proves native activation, a human correction, completed review, useful research, actual skill use, routine execution or delivered output.

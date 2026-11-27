---
name: company-records
description: Use when saving a goal, a fact or preference, or a promised check.
version: 1.0.0
author: Zeus
license: MIT
metadata:
  hermes:
    tags: [goals, memory, checks, verification, records]
    related_skills: [outbound-messages-and-files, scheduled-checks-and-followups]
---

# Records that prove themselves

Record only what a readback or an admitted receipt proves. Three kinds of record share that rule: write the exact source, then prove the saved state with a fresh read before calling it saved.

- Requested outcomes: the `zeus_work` tool. Calls and data shapes: `references/goals-tool.md`.
- Facts, preferences, and corrections: the agent's memory tool. Classification and receipt states: `references/shared-memory-filing.md`.
- Promised later checks and file-existence checks: `references/checks-and-followups.md`.

Sending mail or a file is a separate class: see outbound-messages-and-files.

## When to Use

- A request needs multi-step work: create the work item before the first step.
- The user states a personal fact, a standing preference, or a correction that should be remembered.
- The user asks for a later check, or you are about to promise one.
- You are about to report that a file, record, or outcome exists or changed.

## Procedure

1. Requested outcome.
   - `zeus_work` `status`: look for an existing work item with the same outcome before creating one.
   - `zeus_work` `create`: a title, the exact request, criteria (each with an id, a description and, for a file, the artifact or destination plus checks such as `contains` or `sha256`), and steps that name the criteria they need.
   - After each verified milestone: `zeus_work` `checkpoint` with the id, step and next action; then `status` to confirm the fields.
   - `zeus_work` `complete` only after every criterion has a fresh read. Use `report` for honest partial progress.
2. Fact, preference, or correction.
   - Search memory with a distinctive phrase from the statement. For a correction, find the existing entry.
   - Save the whole statement as received, with its kind (fact, preference, correction) per the reference.
   - Read it back. Only a stored entry you can read back counts as saved.
3. Promised later check: follow the scheduled-checks-and-followups skill. Schedule the check where the result should land (by default, a post in the requesting chat), read the schedule back, and promise only what that readback shows. Never offer "message me at that time" in place of scheduling.
4. File-existence check. Test each candidate path, search the Hermes tree by exact filename if no candidate exists, then report the paths, the search scope, the time, and the result.

## Always-on rules

- Record the exact original request before multi-step work. Never open a second work item for the same request.
- A write is not a save. Confirm each field you changed with a fresh read before you report it.
- Work is complete only when every criterion is verified. A worker's success message or a closed task is not proof.
- Memory says saved only for an entry you read back. Anything queued or pending is not saved; say so.
- Never promise a later automatic check without a scheduled job readback and an exact due time.
- Quote what you checked: the identifier, the field, the value read, and the time read.
- When a recorded rule needed an interpretation, state the reading in one line so the user can correct it before anything acts on it.

## Pitfalls

- Rephrasing the user's words to make them fit gives the record a sentence the user never said. Save the exact text.
- Reusing an earlier reading for a later time. A check reports the state at the moment it runs, and files and records change between checks; run it again at the requested time.
- Treating a work item with a due time in its text as scheduled. Creating a work item does not schedule anything; only a scheduled job readback says a check will run.
- Keeping a blocker only in a field that may not persist. Repeat the blocker wording in the next action.

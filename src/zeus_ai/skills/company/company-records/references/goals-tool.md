# zeus_work: source-verified outcomes

Use `status` for fresh state. `checkpoint`, `verify` and `complete` re-read the exact requested outputs. Read the returned status; a worker claim or a scheduled job's success is not destination proof.

## Creating work

Use it for a direct, substantive assignment. Quick factual questions, acknowledgements, examples, quotes and forwarded text are not work items. Standing rules are separate from finite commitments. Undated actions can stay undated.

`criteria` is a list of objects, not descriptions alone. Each has an `id`, a `description` and, when there is a file to check, an `artifact` (a name in the work item's managed folder) or a `destination` (an exact absolute path the user asked for), plus `checks`:
- `contains` / `not_contains`: text that must or must not be in the final bytes.
- `sha256`: exact bytes.
- `json_equals` / `json_count`: a value or a count at a JSON path.
- `min_bytes`: the file is not empty or truncated.

`steps` name the criteria they need (`criteria`), and may depend on other steps (`depends_on`). For reading or research, keep a verifiable evidence artifact.

Outside effects (a send, a booking) are never proven by the work item alone. Read the destination. Never resend to create a missing receipt.

## Existing work

- `checkpoint`: id, step_id, next_action. It refuses a step whose criteria do not independently pass.
- `report`: id, disposition (`unfinished`, `needs_person`, `uncertain`), next_action, and for `needs_person` a short plain `owner_message`. It saves progress and never claims completion.
- `revise`: changes the same work item after an explicit scope correction from the user.
- `schedule`: saves a durable native continuation for unfinished work. A time written in prose is not a schedule.
- `stop` / `resume`: stop takes effect at once; resume needs an explicit current request.
- `history`: the append-only revisions.
- `doctor`: finds missing wake-ups or stranded workers.

## Reading the result

A failed read is unknown, not failed work. Previously verified completion remains history if its source later becomes unavailable; do not reopen and resend it.

Hard operational rules, voice and saved-state data arrive separately in the prompt. Do not append them to articles or deliverables.

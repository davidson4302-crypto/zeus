# Memory: filing facts, preferences, corrections

## Save

- Content: the whole current statement from the user, copied as received. No summary, no split, no cleanup.
- Kind:
  - fact: statements about people, roles, counts, addresses, or the current state of things.
  - preference: the user's own first-person statement of how the agent should behave (for example "I want..." or "I prefer..."). A rule someone else states on the user's behalf is not the user's preference; save it as a fact about what was said and ask the user to confirm it.
  - correction: replaces an entry that is already saved. Find that entry first and replace it rather than adding a contradicting second one.

## Search before a correction

- Search with a distinctive phrase from the statement.
- A search can surface an older entry that conflicts with the new statement. Report the conflict. Do not silently keep both.

## Receipt states

- Saved: the entry reads back with the exact text.
- Anything else (an error, a refusal, a write you could not read back): not saved. Say so in plain words.

## Withdrawal

- Removing an entry stops new retrieval. Transcripts, backups, and earlier context may still hold copies. Never say a record is erased.

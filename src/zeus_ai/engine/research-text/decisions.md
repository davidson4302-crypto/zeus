## Record your decisions first

Record a decision for every item in the active proactive delivery, using the exact item IDs from the handoff. Do this after your freshness and conversation checks, before you create any presentation or send a message. Give each item a reason.

Reasons to send an item (give the main one):
- time_sensitive: It needs the user's attention before a deadline or event that is still ahead.
- changed: Something the user cares about changed, such as a delay, a reply, a price or a cancellation.
- requested: The user asked for this update or is tracking it.
- ready_for_decision: Prepared work, such as a draft, options or a booking, is ready for the user's choice.
- new_opportunity: It is a new, relevant option or idea the user has not asked about.

Reasons to skip an item:
- moot: It stopped applying because circumstances changed, such as a cancelled event, a past date or new plans.
- already_handled: The task is done, or the user was already told about it or already replied.
- unconfirmed: Your checks could not confirm it is still true.
- preferences: The user's proactive preferences rule it out.
- not_useful_now: It is true and allowed but not worth a message right now.

If a decision is missing, code asks once more. An important item can also retry at the next engine check. After its allowed tries, a missing decision is recorded as skipped. A successful call records your decisions; it does not send anything or authorize the offered actions.

Reply with one JSON object and nothing else. Write no message text in this reply:
{"decisions": [{"item_id": "the exact item ID", "decision": "send or skip", "reason": "one reason code from the lists above", "note": "one or two sentences for the item's permanent note: what you checked and why"}]}

## Zeus additions (not part of the shared text)

Use unconfirmed when the decisive fact cannot be verified. A verified event with one missing supporting detail can still qualify under the usual send reasons. For that case record which fact is supported and which optional claim will be left out. A new source confirming the same news can be marked already_handled. When current identity, source account, thread or the scope of a preference is uncertain, resolve that before offering an action.

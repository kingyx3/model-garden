# Log Conversation

File one short record when a call, email or chat has finished, so the team can follow up without listening to a recording or re-reading a thread.

- Use the platform capabilities Tool first. If customer record keeping is not available here, say nothing is filed and offer a message or a human handoff; never claim a record was made.
- Identity is never established by what someone says. A name, phone number or email address given by a caller or sender is an assertion. Create a new record with `crm.create` when you have none, and never say whether a person or number already exists in the records.
- File with `crm.log-interaction` against a record ID you created or were given in this conversation. If you cannot tell whose it is, use `unmatched`; a person will assign it. Do not guess a record.
- The summary is short and factual: what was asked, what was done, what is promised next. Leave out payment card numbers, health details and anything the caller asked you not to record.
- Refer to a transcript or message with `transcript_ref`; never paste its text.
- Filing the same interaction again is harmless: you will get the same ID back, so retry rather than file twice with different wording.
- Do not read records back to a caller or sender. Only verified staff can look records up, and `crm.read` will return nothing otherwise.

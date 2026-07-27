# Workflow Guard policies

`policies.json` contains one object per protected agent. The plugin engine is generic; domain-specific behavior belongs in these policy objects and, when needed, the classifier adapter.

## Required fields

- `policy_id`: stable unique policy identifier.
- `agent_id`: Evonic agent ID to which the policy applies.
- `threshold`: number of unique countable outcomes that locks the subject.
- `monitored_tools`: tools whose results are classified.
- `mutating_tools`: tools blocked after lock.
- `countable_reason_codes`: finite allowlist of user-correctable failure codes.
- `fixed_response`: exact response returned at threshold and after lock.

## Optional fields

- `enabled`: defaults to `true`.
- `success_statuses`: statuses that close the active workflow as submitted.
- `escalation_destination`: logical destination included in the outbox.
- `suppress_intermediate`: defaults to `true`; prevents intermediate channel messages while this policy is active so threshold output remains exact.
- `counter_strategy`: descriptive strategy. The current engine supports cumulative counting per epoch.
- `identity_resolver`: descriptive resolver. The current engine uses trusted channel identity.

## Adding another agent

1. Add a separate policy with a unique `policy_id` and the target `agent_id`.
2. Ensure the target tools return structured outcomes or extend `classifier.py` with a narrowly scoped adapter.
3. Add only sanitized reason codes to `countable_reason_codes`; infrastructure and internal errors must never be countable.
4. Configure a fixed response and escalation destination.
5. Add classifier, threshold, deduplication, lock, and reopen tests before enabling enforcement.

Never derive subject identity or attempt identity from LLM-provided arguments. The engine uses trusted runtime channel/contact and message/attachment metadata.

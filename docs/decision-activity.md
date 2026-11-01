# Decision Activity API and Privacy Policy

Decision Activity provides bounded diagnostics for Decision Runtime invocations. It is an administrative observability feed, not a prompt, provider-response, or general logging store.

## Recorded event fields

Each event has an opaque ID, timestamp, source (`production`, `playground`, or `profile_test`), profile key/version, selected model ID and adapter/provider key, result class, typed error code, normalized confidence, total latency, attempt count, fallback status/reason, and optional correlation ID.

A detail event may include `outcome_summary`: a **strict allowlist snapshot** of normalized result field values only. The field name must match the Decision Schema-style lowercase key pattern, there are at most 32 fields, scalar strings are capped at 256 characters, and aggregate stored rows are capped at 12 KiB. Nested objects, arrays, provider candidates/logits, arbitrary payloads, and invalid keys are dropped. Keys resembling secret names are also dropped.

### Recorded input (so an event can be reproduced)

While the **Record inputs** setting is on (the default; System Settings → Decision Models → Activity, or `PUT /api/admin/decision-activity/settings {"record_input": false}`), each event also keeps the invocation's **validated `input_context`**, so the event can be reproduced in the Playground. It is stored only if it passes the checks the runtime applies before calling a provider:

- a request refused for carrying a secret-like key or value (`secret_detected`) is never recorded at all, and the store re-checks keys and text anywhere in the input;
- the input is capped at 16 KiB; larger inputs are withheld (`input_status: too_large`);
- when recording is off, an event says so (`input_status: off`); an event recorded before this feature has no input.

`input_status` is `recorded`, `withheld` (credential-like text), `too_large`, `invalid`, `off`, or absent. The list endpoint never returns inputs (only `has_input`); the detail endpoint returns `input_context`. Redacting an event clears its input; purge and clear delete it with the row.

The store still never accepts or records policy metadata, full prompts, provider responses, credentials, free-form provider error text, candidates or logits.

## Administration UI and safe reruns

**System Settings → Decision Models → Activity** offers the same source, profile, model, result/error class, fallback, correlation ID, and time-range filters as the API. It uses opaque cursor pagination and presents loading, empty, error, redaction, and retention-aware states. Events are listed in a table. The detail panel shows the recorded **Input**, the normalized **Output** and the diagnostic fields.

The UI never uses `outcome_summary` (the answer) as an input payload. **Test in Playground** selects the event's profile and fills the Playground's input with the event's recorded `input_context` exactly, so the invocation can be reproduced; it still never runs on its own: the administrator must choose **Run safe test**. It is unavailable, with the reason shown, when no input was recorded or the profile is no longer a saved profile (for example a direct-model Playground test). If the profile has changed since the event, the panel says so, because the result may differ.

## Runtime emission

`RuntimeRequest.activity_source` identifies the operational origin and defaults to `production`. Playground calls must set `playground`; saved profile-test callers must set `profile_test`.

The runtime emits an event for returned successes, failures, and terminal cancellations. Event persistence is wrapped in a broad best-effort boundary: database/telemetry failure is logged with only the profile key and cannot change, fail, or otherwise modify the decision returned to the consumer.

## Administrative API

All endpoints are protected by Evonic's global authenticated administrative middleware.

- `GET /api/admin/decision-activity`: list events. Supports `source`, `profile_key`, `model_id`, `result_class`, `error_code`, `fallback_used`, `correlation_id`, `from`, and `to` filters. Results are deterministically sorted by `(created_at DESC, id DESC)` and use opaque `cursor` pagination with a maximum page size of 100.
- `GET /api/admin/decision-activity/<event_id>`: retrieve the bounded detail record.
- `POST /api/admin/decision-activity/<event_id>/redact`: irreversibly clears the stored summary/correlation ID and hides the row from observability queries.
- `POST /api/admin/decision-activity/purge` with `{"before": <Unix timestamp>}`: deletes all older records and reports the delete count.

## Retention operations

Retention is an explicit, operationally invocable purge by timestamp so deployments can schedule it according to their policy. Redaction is available for an individual record. Indexes cover the primary administration queries: global time order, profile/time, model/time, and correlation/time.

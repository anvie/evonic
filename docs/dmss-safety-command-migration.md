# DMSS `safety.command` migration

## Purpose

DMSS is migrating its model selection and configuration to the built-in
`safety.command` Decision Profile. The profile performs bounded inference over
command text only. It never executes commands, tools, or provider-supplied
instructions. HMADS continues to own deterministic rules, secret detection,
circuit breaking, enforcement boundaries, and the final safety floor.

## Precedence and rollback

1. A persisted `safety.command` profile is the preferred source.
2. Disabled, missing, invalid, unavailable, secret-bearing, timed-out, malformed,
or low-confidence profile results return the deterministic HMADS verdict.
3. While no `safety.command` profile is provisioned, DMSS uses the
`decim_safety.*` settings, including the decision model selected on the Safety
page (`decim_safety.model_id`). Its endpoint and credentials come from that
model's provider; DMSS no longer stores a raw endpoint URL.
4. To roll back, disable the profile or set its mode to `off`; DMSS immediately
returns to the unchanged legacy settings path. Do **not** remove legacy settings
in this migration.

## Modes

- **off**: profile is inactive; legacy settings retain transitional ownership.
- **shadow**: the model result is recorded as bounded Decision Activity, but
  HMADS alone determines the outcome.
- **enforce**: normalized `allow`, `review`, and `block` map to `safe`,
  `requires_approval`, and `dangerous`. A profile result may make a verdict more
  restrictive but cannot weaken a stricter HMADS verdict.

## Monitoring and rollout gates

Monitor Decision Activity for profile key `safety.command`, alongside DMSS
telemetry. Correlate using the correlation ID and inspect only the sanitized
outcome metadata: decision source, mode, migration source, fallback reason,
model/provider identifiers, confidence, and latency. Neither stream stores raw
commands, provider responses, credentials, or tool output.

Before retiring legacy settings, require sustained shadow agreement, tested
provider-outage and low-confidence fallback, a compatible decision-capable model
in the model registry, an administrator-reviewed profile configuration, and a
verified rollback drill. Legacy settings must remain until those gates are
formally approved.

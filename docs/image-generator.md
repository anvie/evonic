# Image Generator: administration, pilot, and rollback

The **Image Generator** skill creates validated image artifacts through either the approved Google Gemini cloud provider or an administrator-approved local AUTOMATIC1111 endpoint. It is disabled by default and its tool is loaded lazily. No agent can enable the feature, supply an endpoint, or read a configured secret.

## Administrator setup

1. Enable the `image-generator` skill only for the pilot agents.
2. Set `allowed_providers` to the exact provider IDs required: `google-gemini` and/or `automatic1111`.
3. Set `default_provider` to one of those enabled IDs. When omitted, every tool call must explicitly select a provider; there is intentionally no fallback.
4. Keep `allow_local_providers` disabled unless the local provider is needed.
5. Start with `max_images_per_request=1`, `requests_per_minute=6`, `max_concurrent_requests=1`, and `images_per_day=20`. These controls apply per agent in the running service process. Use provider-side project quotas/budgets as the authoritative cross-process and billing protection.
6. Do not enable `mock_enabled` outside automated tests or isolated development.

### Google Gemini

Add `google-gemini` to `allowed_providers`, select the supported `google_gemini_model`, and store the credential in the write-only `google_gemini_api_key` setting. Never put the credential in prompts, logs, agent instructions, source control, or a non-secret setting. Google receives the generation prompt and returns the generated media; review its current terms, retention controls, regional requirements, and per-image pricing before enabling it.

### Local AUTOMATIC1111

Local generation is an SSRF-sensitive administrative integration. To enable it, set all of the following:

- `allow_local_providers=true`
- `allowed_providers` includes `automatic1111`
- `automatic1111_endpoint` is the fixed base URL
- `automatic1111_trusted_hosts` contains the endpoint hostname exactly

Put the service on a private, access-controlled network and expose only the approved API host. Do not use a broad DNS name, redirecting proxy, user-controlled endpoint, or an endpoint that can reach internal administrative services. Restrict firewall egress, patch the image server, authenticate it at the network boundary, and monitor it independently. Agents cannot override its URL, step count, headers, or request schema.

## User behavior

After an administrator enables the skill, an agent can call `generate_image` with a prompt and optionally `provider`, `model`, `size`, `count`, `output_format`, `negative_prompt`, and `seed`. The explicit provider overrides the configured default only when it is allowlisted. Unsupported options and unsafe output are rejected rather than silently changed.

The result contains artifact metadata only: filename, MIME type, byte size, and dimensions. It never returns credentials, raw provider responses, image bytes, provider URLs, or the prompt in telemetry. Generated artifacts are subject to the normal Evonic artifact access controls.

## Safety and cost controls

The executor enforces prompt limits, an absolute ten-image request ceiling, per-agent request/concurrency/image quota limits, bounded provider timeouts, and strict byte/dimension/pixel limits. It rejects redirects, private-network artifact URLs, unsafe filenames, mismatched MIME data, malformed images, and decompression-bomb warnings. Provider errors are mapped to stable safe codes such as `rate_limited`, `quota_exceeded`, `content_rejected`, and `artifact_invalid`.

Operational telemetry records only an opaque agent identifier, selected provider, outcome, and image count. It deliberately excludes prompts, generated URLs, raw provider payloads, request IDs, and all secrets.

## Controlled pilot

1. Enable the skill for one non-production pilot agent; keep the global default disabled.
2. Allow only `google-gemini`, configure a small provider-side budget/quota, and use the conservative runtime limits above.
3. Test successful output, invalid option rejection, rate limits, quota limits, provider timeout/error mapping, artifact validation, and agent authorization.
4. Review the minimal audit events and provider billing daily. Increase scope only after the pilot has stable error rates, acceptable cost, and a security review of any local endpoint.
5. Add the local provider only in a separate review after its network controls are verified.

## Rollback and troubleshooting

**Immediate rollback:** disable the `image-generator` skill for the pilot agents (or remove the provider from `allowed_providers`). This prevents new tool calls. Then revoke the cloud credential or block local endpoint egress if compromise is suspected. Preserve ordinary service logs according to retention policy; do not add prompt or secret logging during incident response.

| Symptom | Action |
| --- | --- |
| `provider_configuration` | Configure a non-empty default provider or specify an enabled provider explicitly. |
| `provider_disabled` | Confirm the provider is in `allowed_providers`; for local and mock providers, confirm their separate opt-in flags. |
| `permission_denied` | For local providers, verify the exact trusted hostname and local-provider opt-in. For cloud, ensure the endpoint is public HTTPS. |
| `rate_limited` / `quota_exceeded` | Wait for the configured window, reduce usage, or adjust approved limits and provider-side budget. |
| `artifact_invalid` | Treat the provider output as unsafe; inspect provider health/configuration without recording raw image URLs or credentials. |
| `provider_unavailable` | Check the provider status, DNS/firewall policy, configured timeout, and cloud project quota. |

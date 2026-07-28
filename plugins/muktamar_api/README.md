# Muktamar Photo Validation API

This plugin exposes the existing `validate_photo` implementation to trusted external services.

## Endpoint

```http
POST /plugin/muktamar-api/v1/photo/validate
Authorization: Bearer <API key>
Content-Type: multipart/form-data

photo=<image file>
draft_id=<positive integer>
```

The upload is written to a private temporary file, passed through the normal Evonic tool registry, and removed in a `finally` block. The response deliberately omits internal paths, fingerprints, and provider details:

```json
{"accepted": true, "reason_code": "OK", "user_message": "...", "checks": {}}
```

## Configuration

Enable the plugin, set `API_KEYS` to a comma-separated list of long random secrets, and set `AGENT_ID` to the enabled Muktamar agent owning the registration draft. `MUKTAMAR_API_KEYS` can be used as an environment fallback. Do not commit keys or log request authorization headers.

The endpoint is service-to-service authenticated, not browser-public. Put it behind HTTPS and a reverse proxy with rate limiting. Rotate keys by temporarily configuring both old and new keys, then removing the old key.

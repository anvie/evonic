# Muktamar Photo Validation API

This plugin exposes standalone photo validation to trusted external services.

## Endpoint

```http
POST /plugin/muktamar-api/v1/photo/validate
Authorization: Bearer <API key>
Content-Type: multipart/form-data

photo=<image file>
(no draft_id; validation is stateless)
```

The upload is written to a private temporary file, validated directly without registration state or an agent, and removed in a `finally` block. Responses are normalized:

```json
{"success": true, "message": "Foto sudah sesuai standar."}
```

Rejected photos return `success: false`; operational failures use the same shape with an error status.

## Configuration

Enable the plugin and set `API_KEYS` to a comma-separated list of long random secrets. `MUKTAMAR_API_KEYS` can be used as an environment fallback. `MAX_UPLOAD_BYTES` defaults to 8388608. No `AGENT_ID` is needed. Do not commit keys or log request authorization headers.

The endpoint is service-to-service authenticated, not browser-public. Put it behind HTTPS and a reverse proxy with rate limiting. Rotate keys by temporarily configuring both old and new keys, then removing the old key.

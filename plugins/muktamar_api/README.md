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

The upload is written to a private temporary file, validated directly without registration state or an agent, and removed in a `finally` block. Photos must be portrait and at least 200 × 300 pixels after EXIF orientation is applied. Responses are normalized:

```json
{
  "success": true,
  "reason_code": ["OK"],
  "message": "Foto sudah sesuai standar."
}
```

Rejected photos return every validation failure as a stable string array so clients can map each enum to a precise message:

```json
{
  "success": false,
  "reason_code": ["APPROPRIATE_POSE", "APPROPRIATE_BACKGROUND"],
  "message": "Pose dan latar foto tidak sesuai."
}
```

Legacy scalar validator codes are normalized to a one-item array. Operational failures use an HTTP error status and an `error` field instead of validation reason codes.

## Configuration

Enable the plugin and set `API_KEYS` to a comma-separated list of long random secrets. `MUKTAMAR_API_KEYS` can be used as an environment fallback. `MAX_UPLOAD_BYTES` defaults to 8388608. No `AGENT_ID` is needed. Do not commit keys or log request authorization headers.

The endpoint is service-to-service authenticated, not browser-public. Put it behind HTTPS and a reverse proxy with rate limiting. Rotate keys by temporarily configuring both old and new keys, then removing the old key.

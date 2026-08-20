# Google Vertex AI as a Provider

Evonic's built-in provider presets (`openrouter`, `togetherai`, `ollama`, `ollama_cloud`,
`opencode_zen`, `opencode_go`, `deepseek`, `llama.cpp`, `custom`) don't include Google
Vertex AI, because Vertex's project-based auth model (short-lived OAuth access tokens,
~1 hour expiry) doesn't fit the static-`api_key` shape every other provider uses.

This adds a `google_oauth` auth mode so a `custom` provider can point at Vertex AI's
OpenAI-compatible endpoint and stay authenticated automatically, refreshing the access
token in the background using a Google Cloud [Application Default Credentials
(ADC)](https://cloud.google.com/docs/authentication/application-default-credentials)
refresh token — no manual token pasting after initial setup.

> This is a **project-based Vertex AI** setup (billed to a GCP project, full model
> access). It is a different product from a plain Gemini API key
> (`AIza...`, from [AI Studio](https://aistudio.google.com/apikey)) and from Vertex AI
> **Express Mode** API keys (`AQ...`) — both of those are simpler static-key auth, but
> Express Mode currently only exposes Google's native `generateContent` REST API, not
> the OpenAI-compatible `/chat/completions` endpoint this integration uses. If your key
> starts with `AIza`, follow the *Gemini API key* path below instead — it works with the
> existing `custom` provider type today, with no code changes needed.

## Option A — Gemini API key (simplest, no code changes)

If you have a static Gemini API key (`AIza...`) from AI Studio, this already works with
Evonic's stock `custom` provider type:

```
Provider type: custom
base_url:      https://generativelanguage.googleapis.com/v1beta/openai
api_key:       <your Gemini API key>
api_format:    openai
```

Add it from **Settings → Providers → Add Provider** in the web UI, then add a model
under it (e.g. `model_name: gemini-2.5-flash`).

## Option B — Vertex AI via ADC OAuth (project-based, auto-refreshing)

Use this when you only have `gcloud` project access (no static API key), via:

```bash
gcloud auth application-default login
```

This writes a refresh token to
`~/.config/gcloud/application_default_credentials.json`. Evonic can use that refresh
token to keep a Vertex AI provider authenticated indefinitely.

### 1. Read your ADC credentials

```bash
cat ~/.config/gcloud/application_default_credentials.json
```

You need `refresh_token` from this file, plus your GCP **project ID** and a
**region** that has your target model enabled (e.g. `us-central1`,
`asia-southeast1` — check
[Model Garden](https://console.cloud.google.com/vertex-ai/model-garden) for
regional availability).

### 2. Create the provider and model

There is currently no `./evonic` CLI subcommand or Settings UI flow for the
`google_oauth` auth mode — create the rows directly against the database, the same way
`routes/providers.py` and `routes/models.py` do internally:

```python
from models.db import db

PROJECT = "<your-gcp-project-id>"
REGION = "asia-southeast1"          # any Vertex AI region with your model enabled
REFRESH_TOKEN = "<refresh_token from application_default_credentials.json>"

BASE_URL = (
    f"https://{REGION}-aiplatform.googleapis.com/v1beta1/"
    f"projects/{PROJECT}/locations/{REGION}/endpoints/openapi"
)

db.create_provider({
    "id": "gemini_vertex",
    "name": "Gemini (Vertex AI)",
    "type": "remote",
    "base_url": BASE_URL,
    "api_format": "openai",
    "enabled": 1,
})
db.update_provider("gemini_vertex", {
    "auth_type": "google_oauth",
    "refresh_token": REFRESH_TOKEN,
    "token_expires_at": 0,   # force a refresh on first use
})

db.create_model({
    "name": "Gemini 2.5 Flash (Vertex)",
    "type": "remote",
    "provider": "gemini_vertex",
    "base_url": BASE_URL,
    "api_key": "",              # left empty on purpose — see note below
    "model_name": "google/gemini-2.5-flash",  # publisher-prefixed: <publisher>/<model>
    "api_format": "openai",
    "vision_supported": 1,
    "enabled": 1,
})
```

Run it with the project's own interpreter so it picks up the right dependencies and
DB path: `.venv/bin/python3 your_script.py` (or `venv/bin/python3`, whichever exists).

**Leave the model's own `api_key` empty.** `resolve_model_config()` only falls back to
the provider's `api_key` when the model's own field is empty — since the provider's key
is the one that gets auto-refreshed, a non-empty model-level key would silently pin the
model to a stale, soon-to-expire token forever.

**Model name must be publisher-prefixed** (`google/gemini-2.5-flash`, not
`gemini-2.5-flash`) — Vertex's OpenAI-compat endpoint rejects a bare model name with
`400 Malformed publisher model ... expected '<publisher>/<model>'`.

### 3. Assign the model to an agent

From the web UI: agent's Settings → Model, or via the DB / `db.update_agent(agent_id,
{"model_id": "gemini_vertex/google/gemini-2.5-flash"})` (use the actual model `id`
returned by `create_model`).

### How the auto-refresh works

`backend/provider/oauth_google.py` mirrors the existing
`backend/provider/oauth_codex.py` OAuth pattern already used for the OpenAI Codex
provider:

- `get_valid_token(db, provider_id)` reads the provider row; if `auth_type` isn't
  `google_oauth` it returns `None` immediately (no-op for every other provider).
- If the stored access token is missing, has no known expiry, or is within 5 minutes of
  expiring, it exchanges the stored `refresh_token` for a new access token via
  `POST https://oauth2.googleapis.com/token` and persists the new token +
  `token_expires_at` back to the provider row.
- `backend/llm_client.py` calls this once, right before building request headers, in
  both `chat_completion()` and `test_connection()` — a cheap, indexed provider-row
  lookup that's a no-op for the ~20+ other provider configs in a typical install.

The `CLIENT_ID`/`CLIENT_SECRET` constants in `oauth_google.py` are Google's well-known
public "gcloud" installed-app OAuth client — the same one every
`gcloud auth application-default login` refresh token is issued under (visible in your
own `application_default_credentials.json`). They are not a per-project secret.

### Caveat

If you ever run `gcloud auth application-default revoke` (or otherwise invalidate the
ADC credential) on the machine that generated the refresh token, the stored
`refresh_token` stops working and `get_valid_token()` will start returning the last
known (expired) access token. Re-run `gcloud auth application-default login` and update
the provider's `refresh_token` to fix it.

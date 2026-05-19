# Model Router API Plugin

Expose Evonic LLM models via an OpenAI-compatible REST API. Bearer-token-authenticated model completions with quota, rate-limit, and usage logging.

## Configuration

The plugin has one config variable: **ROUTER_MODEL_LIST**.

### ROUTER_MODEL_LIST

A comma-separated list of `model_name` values from your LLM Models settings page. Only models listed here can be called via the API. Leave empty to allow all enabled models.

**Auto-populated on install:** When you install the plugin, this field is automatically filled with all currently enabled models from your system. You can edit it to restrict which models are accessible via the API.

**Where to find your model names:**

Go to **Admin → Settings → LLM Models** in the Evonic web UI. Each model has a `model_name` field — use those exact values.

**Examples:**

| Use case | Value |
|---|---|
| Allow all enabled models | *(leave empty)* |
| A few popular models | `deepseek-v4-flash,moonshotai/kimi-k2-thinking,Qwen3.6-27B-MTP` |
| Single model only | `deepseek-v4-flash` |

---

## Consumer Endpoints

These endpoints use **Bearer token** authentication. No web session is required.

### `POST /plugin/model-router/v1/chat/completions`

OpenAI-compatible chat completion. Routes directly to the LLM model specified by the `model` field.

**Request:**

```bash
curl -X POST http://localhost:8080/plugin/model-router/v1/chat/completions \
  -H "Authorization: Bearer <your-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [
      {"role": "user", "content": "Hello! Who are you?"}
    ]
  }'
```

**Streaming mode (`"stream": true`):**

Returns an SSE (Server-Sent Events) stream with OpenAI-compatible chunk objects, ending with `data: [DONE]`.

### `GET /plugin/model-router/v1/models`

List available models filtered by the token's scope.

---

## Admin Endpoints

These endpoints use the **web session** authentication (cookies) — the same session used when logged into the Evonic admin panel.

### `GET /api/model-router/admin`

Renders the token management dashboard (HTML page).

### `GET /api/model-router/admin/tokens`

List all bearer tokens. Optionally filter by status.

### `POST /api/model-router/admin/tokens`

Create a new bearer token. The plaintext token is returned **only once**.

### `PUT /api/model-router/admin/tokens/<id>`

Update a token's mutable fields. All fields are optional.

### `DELETE /api/model-router/admin/tokens/<id>`

Permanently delete a token.

### `GET /api/model-router/admin/tokens/<id>/stats`

Return detailed usage statistics for a specific token.

### `GET /api/model-router/admin/tokens/<id>/logs`

Return the usage log for a specific token.

---

## Token Lifecycle

1. **Create** — Admin creates a token. Plaintext returned once.
2. **Use** — Consumer sends requests with `Authorization: Bearer <plaintext>`.
3. **Monitor** — Admin views stats, quota usage, and last-used timestamps.
4. **Suspend** — Admin sets `status: "suspended"` to temporarily block a token.
5. **Reset** — Admin regenerates the secret key (old key invalidated). New plaintext returned once.
6. **Delete** — Admin permanently removes the token and its usage logs.

## Error Response Format

All error responses follow this structure:

```json
{
  "error": "Human-readable error message"
}
```

Quota-exceeded errors (429) include extra fields:

```json
{
  "error": "Quota exceeded",
  "quota_limit": 10000,
  "quota_used": 10000
}
```


## Usage Examples

### Sync request

```bash
curl -X POST http://localhost:8080/plugin/model-router/v1/chat/completions \
  -H "Authorization: Bearer sk-your-token-here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [
      {"role": "user", "content": "Hello! Who are you?"}
    ]
  }'
```

### Streaming request

```bash
curl -X POST http://localhost:8080/plugin/model-router/v1/chat/completions \
  -H "Authorization: Bearer sk-your-token-here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [
      {"role": "user", "content": "Tell me a story..."}
    ],
    "stream": true
  }' | while read -r line; do
    echo "$line" | sed 's/^data: //' | jq .
  done
```

### List available models

```bash
curl http://localhost:8080/plugin/model-router/v1/models \
  -H "Authorization: Bearer sk-your-token-here" | jq .
```

### OpenAI Python SDK compatible

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-your-token-here",
    base_url="http://localhost:8080/plugin/model-router/v1"
)

response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

## Token Management (Admin)

All admin endpoints require web session authentication (cookies).

### Creating a token

```bash
curl -X POST http://localhost:8080/api/model-router/admin/tokens \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My App",
    "quota_limit": 10000,
    "expires_at": "2026-12-31T23:59:59Z",
    "allowed_models": ["deepseek-v4-flash", "moonshotai/kimi-k2-thinking"]
  }'
```

Response includes the plaintext token — copy it immediately, it won't be shown again.

### Admin dashboard

Visit `http://localhost:8080/api/model-router/admin` in your browser (must be logged in) to manage tokens, view stats, and check usage logs.

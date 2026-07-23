"""
describe_image.py — dedicated image description tool using a separate vision model.

Agents use this tool to analyze images rather than having images auto-fed to the
main LLM. The vision model is selected via a configurable priority chain:
  1. agent-level `vision_model_id` column
  2. system config `vision_model_id` (app_settings)
  3. system fallback `vision_fallback_model_id` (app_settings)
  4. agent's current model (if vision_supported)
  5. all enabled models with `vision_supported = 1` in `llm_models`

On connection errors, the tool automatically falls back to the next
vision-capable model in priority order.

The `vision_enabled` flag on the agent gates access to this tool entirely:
when `vision_enabled = 0`, the tool returns an error.
"""

from __future__ import annotations

import base64
import difflib
import mimetypes
import os
from typing import Any, Dict, Optional

from backend.llm_client import LLMClient

# Image MIME types the tool supports
_SUPPORTED_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
})


def _resolve_vision_models(agent: dict) -> tuple[list, Optional[str]]:
    """Resolve vision models to use for image description, ordered by priority.

    Returns a list so the caller can fallback to the next model on connection errors.

    Priority:
      1. Agent-level vision_model_id (from agent_context)
      2. System config vision_model_id (app_settings)
      3. System fallback vision_fallback_model_id (app_settings)
      4. Agent's current model (if vision_supported)
      5. All enabled models with vision_supported = 1

    Returns:
        (models_list, error_string).  Exactly one will be non-None/empty.
        models_list is a deduplicated list of model dicts in priority order.
    """
    from models.db import db

    models = []
    seen_ids = set()

    def _add_model(model):
        """Add model if not seen before (dedup by id, fallback to name)."""
        model_id = model.get("id") or model.get("name", "")
        if model_id and model_id not in seen_ids:
            seen_ids.add(model_id)
            models.append(model)

    # Priority 1: agent-level config (from context dict)
    vision_model_id = agent.get("vision_model_id")
    if vision_model_id:
        model = db.get_model_by_id(vision_model_id)
        if model and model.get("enabled"):
            _add_model(model)

    # Priority 2: system config
    system_vision_id = db.get_setting("vision_model_id")
    if system_vision_id and system_vision_id != vision_model_id:
        model = db.get_model_by_id(system_vision_id)
        if model and model.get("enabled"):
            _add_model(model)

    # Priority 3: system fallback model (configured in System Settings → General)
    fallback_vision_id = db.get_setting("vision_fallback_model_id")
    if fallback_vision_id and fallback_vision_id not in seen_ids:
        model = db.get_model_by_id(fallback_vision_id)
        if model and model.get("enabled") and model.get("vision_supported"):
            _add_model(model)

    # Priority 4: agent's current model (natural fallback before global auto-detect).
    _agent_db_id = agent.get("_db_agent_id") or agent.get("id")
    agent_model = db.get_agent_model(_agent_db_id)
    if agent_model and agent_model.get("vision_supported"):
        _add_model(agent_model)

    # Priority 5: all enabled vision-capable models
    all_models = db.get_enabled_llm_models()
    for model in all_models:
        if model.get("vision_supported"):
            _add_model(model)

    if models:
        return models, None

    return [], (
        "No vision-capable model is available. "
        "Please configure a vision model in System Settings (requires vision_supported=1)."
    )


def _find_closest_attachment(agent_id: str, orig_path: str) -> Optional[str]:
    """Search the agent's data/attachments directory for a close filename match.

    Uses difflib.SequenceMatcher on the basenames.  Returns the first file
    whose similarity ratio exceeds 0.7, or None if no good match is found.
    """
    if not agent_id:
        return None
    orig_basename = os.path.basename(orig_path)
    if not orig_basename:
        return None
    attachment_dir = os.path.join("data", "attachments", agent_id)
    if not os.path.isdir(attachment_dir):
        return None
    best_ratio = 0.0
    best_path: Optional[str] = None
    for dirpath, _dirnames, filenames in os.walk(attachment_dir):
        for fname in filenames:
            ratio = difflib.SequenceMatcher(None, orig_basename.lower(), fname.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_path = os.path.abspath(os.path.join(dirpath, fname))
    if best_ratio > 0.7 and best_path:
        return best_path
    return None


def execute(agent: dict, args: dict) -> Any:
    """Analyze an image file and return a text description.

    Args:
        agent: Agent context dict (must contain at least 'id').
        args:
            path (str, required): Absolute or relative path to the image file.
            query (str, optional): Specific question about the image.
                If omitted, a general description is returned.

    Returns:
        str: Plain-text description of the image, or an error message.
    """
    # Guard against malformed tool calls where the LLM passes a dict/list
    # instead of a string.  (non-string truthy values would bypass the
    # `or ""` short-circuit and crash on .strip())
    path = args.get("path")
    path = path.strip() if isinstance(path, str) else ""
    query = args.get("query")
    query = query.strip() if isinstance(query, str) else ""

    # --- Gate: vision_enabled ---
    # The agent_context dict includes vision_enabled when the runtime builds it.
    vision_enabled = agent.get("vision_enabled", 1)
    if not vision_enabled:
        return (
            "Error: Image analysis is not enabled for this agent "
            "(vision_enabled=0). Enable it in the agent's settings to use "
            "the describe_image tool."
        )

    # --- Validate path ---
    if not path:
        return "Error: 'path' parameter is required. Provide the file path to the image."

    # Resolve /_self/ virtual paths (e.g. /_self/artifacts/foo.webp)
    agent_id = (agent or {}).get("id", "")
    if agent_id:
        from backend.tools._workspace import is_self_path, resolve_self_path, effective_agent_id
        if is_self_path(path):
            resolved = resolve_self_path(effective_agent_id(agent), path)
            if resolved:
                path = resolved

    if not os.path.isfile(path):
        suggestion = _find_closest_attachment(agent_id, path)
        if suggestion:
            return f"Error: File not found: {path}. Did you mean: {suggestion}?"
        return f"Error: File not found: {path}"

    # If the agent operates in a remote workplace (SSH/tunnel/etc.), ensure the
    # image file is also available on the remote filesystem so the agent can
    # reference it via bash, runpy, or other backend-routed tools.
    try:
        from backend.tools._ensure_workplace_file import ensure_workplace_file
        ensure_workplace_file(path, agent)
    except (ImportError, RuntimeError):
        pass  # Non-critical: file is already accessible from the host side

    file_size = os.path.getsize(path)
    if file_size > 10 * 1024 * 1024:  # 10 MB
        return f"Error: Image file is {file_size / (1024*1024):.1f} MB, which exceeds the 10 MB limit."

    # --- Detect MIME type ---
    mime_type, _ = mimetypes.guess_type(path)
    # mimetypes may not know .webp or other newer formats on some systems.
    # Fall back to extension-based detection when mimetypes returns unknown.
    if not mime_type:
        _ext_map = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp',
        }
        mime_type = _ext_map.get(os.path.splitext(path)[1].lower())
    if not mime_type or mime_type not in _SUPPORTED_IMAGE_TYPES:
        detected = mime_type or "unknown"
        return (
            f"Error: Unsupported image type '{detected}'. "
            f"Supported formats: JPEG, PNG, GIF, WebP, BMP."
        )

    # --- Read image and encode as base64 ---
    try:
        with open(path, "rb") as f:
            image_data = f.read()
    except PermissionError:
        return f"Error: Permission denied — cannot read: {path}"
    except Exception as e:
        return f"Error: Failed to read image: {e}"

    image_b64 = base64.b64encode(image_data).decode("utf-8")

    # --- Resolve vision models (ordered list for fallback) ---
    vision_models, error = _resolve_vision_models(agent)
    if error:
        return f"Error: {error}"

    # --- Build the vision request ---
    # Use base64 JPEG encoding for the data URL regardless of source format;
    # most vision models handle the standard image/jpeg MIME fine.
    data_url = f"data:image/jpeg;base64,{image_b64}"

    system_prompt = (
        "You are a helpful image analysis assistant. "
        "Describe the image clearly and concisely. "
        "If asked a specific question, answer it directly based on what you see."
    )

    if query:
        user_text = (
            f"Please analyze this image and answer the following question: {query}"
        )
    else:
        user_text = "Please describe this image in detail. What do you see?"

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]

    # --- Call vision models with fallback on connection errors ---
    result = None
    connection_failures = 0
    last_error = None

    for vision_model in vision_models:
        model_name = vision_model.get("name", vision_model.get("id", "unknown"))
        try:
            client = LLMClient(model_config=vision_model)
            # Enforce a 2-minute (120s) maximum timeout for vision model calls,
            # regardless of the model's configured timeout.
            if client.timeout is None or client.timeout > 120:
                client.timeout = 120
            result = client.chat_completion(
                messages=messages,
                enable_thinking=False,  # No need for reasoning on vision task
            )
        except Exception as e:
            # Unexpected exception — treat as connection failure and try next
            connection_failures += 1
            last_error = str(e)
            continue

        if result.get("success"):
            break  # Success — use this result

        # Check if this is a connection error we should fallback from
        error_type = result.get("error_type", "")
        error_detail = result.get("error_detail", "")
        if error_type == "connection_error":
            connection_failures += 1
            last_error = error_detail or f"connection to {model_name}"
            continue  # Try next model

        # Non-connection error — fail immediately (auth, rate limit, API error, etc.)
        return f"Error: Vision model call failed ({error_type}): {error_detail}"

    if result is None or not result.get("success"):
        if connection_failures >= len(vision_models):
            return (
                "Error: All vision-capable models failed with connection errors. "
                "Please check your network and LLM server status."
            )
        return f"Error: Vision model call failed: {last_error or 'unknown error'}"

    # Extract text content from the nested API response.
    # result["response"] is the raw API dict: {"choices": [{"message": {"content": "..."}}]}
    response_data = result.get("response", {})
    choices = response_data.get("choices", [])
    if not choices:
        return "Error: Vision model returned no choices in response."

    message = choices[0].get("message", {})
    content = message.get("content", "")
    if not content:
        return "Error: Vision model returned an empty response."

    # Strip any thinking tags that may have been included
    from backend.llm_client import strip_thinking_tags
    cleaned, _ = strip_thinking_tags(content)
    return cleaned.strip()

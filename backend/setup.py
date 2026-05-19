"""
Shared setup orchestration for Evonic first-time onboarding.
Used by both the CLI wizard (evonic setup) and the web API (/api/setup).
"""

import glob
import json
import os
import re
import shutil
import secrets
import subprocess
import tempfile

import requests

import config
from models.db import db

# ---------------------------------------------------------------------------
# Provider defaults
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS = {
    "openrouter": {
        "type": "remote",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_required": True,
        "placeholder_model": "openai/gpt-4o-mini",
        "label": "OpenRouter",
        "description": "Cloud · API key required",
    },
    "togetherai": {
        "type": "remote",
        "base_url": "https://api.together.xyz/v1",
        "api_key_required": True,
        "placeholder_model": "meta-llama/Llama-3-70b-chat-hf",
        "label": "Together AI",
        "description": "Cloud · API key required",
    },
    "ollama": {
        "type": "local",
        "base_url": "http://localhost:11434/v1",
        "api_key_required": False,
        "placeholder_model": "llama3",
        "label": "Ollama",
        "description": "Local · no API key needed",
    },
    "ollama_cloud": {
        "type": "remote",
        "base_url": "https://ollama.com/api",
        "api_key_required": True,
        "placeholder_model": "gpt-oss:120b",
        "label": "Ollama Cloud",
        "description": "Cloud · API key required",
        "api_format": "ollama",
    },
    "opencode_zen": {
        "type": "remote",
        "base_url": "https://opencode.ai/zen/v1",
        "api_key_required": True,
        "placeholder_model": "qwen3.6-plus",
        "label": "OpenCode Zen",
        "description": "Cloud · API key required",
    },
    "opencode_go": {
        "type": "remote",
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key_required": True,
        "placeholder_model": "kimi-k2.6",
        "label": "OpenCode Go",
        "description": "Cloud · API key required",
        # Go's catalog is dominated by always-thinking models (Kimi K2, DeepSeek V4,
        # MiniMax M2). The upstream rejects requests that omit reasoning_content on
        # prior assistant messages, so default thinking on for this provider.
        "default_thinking": True,
    },
    "deepseek": {
        "type": "remote",
        "base_url": "https://api.deepseek.com",
        "api_key_required": True,
        "placeholder_model": "deepseek-v4-pro",
        "label": "DeepSeek",
        "description": "Cloud · API key required",
    },
    "llama.cpp": {
        "type": "local",
        "base_url": "http://localhost:8080/v1",
        "api_key_required": False,
        "placeholder_model": "default",
        "label": "llama.cpp",
        "description": "Local · no API key needed",
    },
    "custom": {
        "type": "remote",
        "base_url": "",
        "api_key_required": False,
        "placeholder_model": "",
        "label": "Custom",
        "description": "Any OpenAI-compatible endpoint",
    },
}

# ---------------------------------------------------------------------------
# Tone/style presets
# ---------------------------------------------------------------------------

LANGUAGE_PRESETS = {
    "english": {
        "label": "English",
        "description": "Always respond in English",
        "instruction": "Always respond in English.",
    },
    "indonesian": {
        "label": "Bahasa Indonesia",
        "description": "Always respond in Bahasa Indonesia",
        "instruction": "Always respond in Bahasa Indonesia.",
    },
    "adaptive": {
        "label": "Adaptive",
        "description": "Follow the language the user uses",
        "instruction": "Respond in the same language the user uses. If the user mixes languages, you may mix too.",
    },
}



# ---------------------------------------------------------------------------
# Notes.md template
# ---------------------------------------------------------------------------

_NOTES_MD_TEMPLATE = """# Notes.md -- User Preferences & Instructions

This file stores your user's personal preferences, tastes, language
preferences, and communication style instructions.

## What to store here

- User's preferred language (e.g. "User prefers Bahasa Indonesia")
- Communication style preferences (e.g. "User likes concise answers",
  "User dislikes emoji")
- Personal instructions (e.g. "Call the user 'Pak'")
- Tastes and preferences (e.g. "User prefers bullet points over paragraphs")
- Execution instructions (e.g. "Always use tmux/screen/nohup for long-running programs like cmake/make build, unit testing, benchmarking, etc.")

## What NOT to store here (use `remember` instead)

- Factual/memorization data: addresses, phone numbers, email, birthday
- Secret/sensitive data: passwords, tokens, PINs, secret codes, bank accounts

## Usage

- Read this file: read("notes.md")
- Update via write_file with path /_self/kb/notes.md
- Update immediately when the user gives a new preference
- Prioritize notes.md over `remember` for non-factual preference information
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def check_docker_available() -> dict:
    """Check if Docker CLI is available and daemon is running.
    Returns {'available': bool, 'message': str}."""
    # 1. Check if 'docker' command exists
    if shutil.which("docker") is None:
        return {"available": False, "message": "Docker CLI not found in PATH"}

    # 2. Check if Docker daemon is running
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return {"available": True, "message": "Docker is available and running"}
        return {
            "available": False,
            "message": f"Docker daemon not running: {result.stderr.strip()[:200]}",
        }
    except subprocess.TimeoutExpired:
        return {"available": False, "message": "Docker daemon unresponsive (timeout)"}
    except FileNotFoundError:
        return {"available": False, "message": "Docker CLI not found"}
    except Exception as e:
        return {"available": False, "message": f"Docker check failed: {e}"}


def build_sandbox_image() -> dict:
    """Build the Docker sandbox image from docker/tools/Dockerfile.
    Returns {'success': bool, 'message': str}."""
    dockerfile_path = os.path.join(config.BASE_DIR, "docker", "tools", "Dockerfile")
    if not os.path.isfile(dockerfile_path):
        return {
            "success": False,
            "message": f"Dockerfile not found at {dockerfile_path}",
        }

    image_tag = config.SANDBOX_IMAGE
    try:
        result = subprocess.run(
            [
                "docker",
                "build",
                "-t",
                image_tag,
                "-f",
                dockerfile_path,
                os.path.join(config.BASE_DIR, "docker", "tools"),
            ],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min timeout for image build
        )
        if result.returncode == 0:
            return {
                "success": True,
                "message": f"Docker image {image_tag} built successfully",
            }
        # Capture last few lines of error
        stderr_tail = (
            "\n".join(result.stderr.strip().split("\n")[-5:])
            if result.stderr.strip()
            else "(no output)"
        )
        return {
            "success": False,
            "message": f"Docker build failed (exit {result.returncode}): {stderr_tail}",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Docker build timed out after 10 minutes"}
    except Exception as e:
        return {"success": False, "message": f"Docker build error: {e}"}


def test_connection(base_url: str, api_key: str = None) -> dict:
    """
    Test connectivity to an LLM endpoint.
    For Ollama Cloud (ollama.com), hits /tags.
    For OpenAI-compatible endpoints, hits /models.
    Returns {'success': bool, 'message': str}.
    """
    if not base_url:
        return {"success": False, "message": "Base URL is required"}

    # Ollama Cloud uses native API (/tags), not OpenAI-compatible (/models)
    if "ollama.com" in base_url:
        url = base_url.rstrip("/") + "/tags"
    else:
        url = base_url.rstrip("/") + "/models"

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            try:
                data = resp.json()
                # Ollama Cloud returns {"models": [...]}, OpenAI-compatible returns {"data": [...]}
                if "ollama.com" in base_url:
                    models = data.get("models", [])
                else:
                    models = data.get("data", data) if isinstance(data, dict) else data
                count = len(models) if isinstance(models, list) else "?"
                return {
                    "success": True,
                    "message": f"Connected ({count} models available)",
                }
            except Exception:
                return {"success": True, "message": "Connected"}
        elif resp.status_code == 401:
            return {
                "success": False,
                "message": "Authentication failed — check your API key",
            }
        else:
            return {
                "success": False,
                "message": f"Server returned HTTP {resp.status_code}",
            }
    except requests.exceptions.ConnectionError:
        return {
            "success": False,
            "message": "Connection refused — is the server running?",
        }
    except requests.exceptions.Timeout:
        return {"success": False, "message": "Connection timed out"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def build_system_prompt(tone_text: str = "") -> str:
    """
    Build the super agent system prompt from the default template.

    Reads defaults/super_agent_system_prompt.md and replaces the
    {communication_style} placeholder with the given tone_text.
    If the placeholder is absent, the template is returned as-is.
    """
    default_path = os.path.join(
        config.BASE_DIR, "defaults", "super_agent_system_prompt.md"
    )
    base_prompt = ""
    if os.path.isfile(default_path):
        with open(default_path, "r", encoding="utf-8") as f:
            base_prompt = f.read()

    if "{communication_style}" in base_prompt:
        return base_prompt.replace("{communication_style}", tone_text.strip())
    return base_prompt


def _derive_agent_id(name: str) -> str:
    """Derive a valid agent ID from a display name."""
    slug = re.sub(r"[^a-z0-9_]", "_", name.lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "admin"


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_setup(
    provider: str,
    model_name: str,
    base_url: str,
    api_key: str,
    agent_name: str,
    agent_id: str = None,
    description: str = "",
    language: str = "english",
    sandbox_enabled: bool = False,
    password: str = "",
) -> dict:
    """
    Execute first-time setup:
    1. Create LLM model entry in DB and set as default
    2. Build system prompt
    3. Create super agent with is_super=True
    4. Write SYSTEM.md
    5. Assign default tools
    6. Store settings (including sandbox default)

    Returns {'success': True, 'agent_id': str} or {'error': str}.
    """
    from routes.agents import _ensure_kb_dir, _write_system_prompt

    # Validate language
    if language not in LANGUAGE_PRESETS:
        language = "english"

    # Derive agent ID if not provided
    if not agent_id:
        agent_id = _derive_agent_id(agent_name)

    # Validate
    if not agent_name.strip():
        return {"error": "Agent name is required"}
    if not re.match(r"^[a-z0-9_]+$", agent_id):
        return {
            "error": "Agent ID must be lowercase alphanumeric and underscores only (snake_case)"
        }
    if db.has_super_agent():
        return {"error": "Super agent already exists"}
    if db.get_agent(agent_id):
        return {"error": f'Agent ID "{agent_id}" already exists'}
    if not provider or provider not in PROVIDER_DEFAULTS:
        return {"error": f"Unknown provider: {provider}"}
    if not model_name.strip():
        return {"error": "Model name is required"}

    provider_cfg = PROVIDER_DEFAULTS[provider]

    # Resolve base_url: use user-provided or fall back to provider default
    resolved_base_url = (base_url or provider_cfg["base_url"]).rstrip("/")

    try:
        # 0. Generate SECRET_KEY if not already set — critical for session security
        if not os.getenv("SECRET_KEY"):
            _key = secrets.token_urlsafe(48)
            env_path = os.path.join(config.BASE_DIR, ".env")
            _update_env_var(env_path, "SECRET_KEY", _key)
            os.environ["SECRET_KEY"] = _key

        # 1. Create model in DB as default
        model_id = f"setup_{provider}"
        # Derive api_format: ollama + local → openai, ollama + remote → ollama native
        if provider == "ollama" and "ollama.com" in resolved_base_url:
            model_api_format = "ollama"
            model_type = "remote"
        else:
            model_api_format = provider_cfg.get("api_format", "openai")
            model_type = provider_cfg["type"]
        db.create_model(
            {
                "id": model_id,
                "name": f"{provider_cfg['label']} ({model_name})",
                "type": model_type,
                "provider": provider,
                "base_url": resolved_base_url,
                "api_key": api_key or "",
                "model_name": model_name,
                "is_default": 1,
                "enabled": 1,
                "api_format": model_api_format,
                "thinking": 1 if provider_cfg.get("default_thinking") else 0,
            }
        )

        # 2. Build system prompt
        system_prompt = build_system_prompt()

        # 3. Create super agent
        _ensure_kb_dir(agent_id)
        db.create_agent(
            {
                "id": agent_id,
                "name": agent_name.strip(),
                "description": "Evonic Super Agent",
                "system_prompt": system_prompt,
                "model": None,
                "is_super": True,
                "workspace": config.BASE_DIR,
                "sandbox_enabled": 1 if sandbox_enabled else 0,
            }
        )

        # 4. Write SYSTEM.md on disk
        _write_system_prompt(agent_id, system_prompt)

        # 4.5 Copy default knowledge base files
        _default_kb = os.path.join(config.BASE_DIR, 'defaults', 'super_agent_kb_evonic.md')
        if os.path.isfile(_default_kb):
            _kb_dir = os.path.join(config.BASE_DIR, "agents", agent_id, "kb")
            os.makedirs(_kb_dir, exist_ok=True)
            shutil.copy2(_default_kb, os.path.join(_kb_dir, "evonic.md"))

        # 4.5.1 Copy howto-create-schedule.md (scheduler/reminder guide)
        _scheduler_kb = os.path.join(config.BASE_DIR, 'defaults', 'howto-create-schedule.md')
        if os.path.isfile(_scheduler_kb):
            _kb_dir = os.path.join(config.BASE_DIR, "agents", agent_id, "kb")
            os.makedirs(_kb_dir, exist_ok=True)
            shutil.copy2(_scheduler_kb, os.path.join(_kb_dir, "howto-create-schedule.md"))

        # 4.6 Create notes.md template for user preferences
        _notes_md_path = os.path.join(config.BASE_DIR, "agents", agent_id, "kb", "notes.md")
        if not os.path.isfile(_notes_md_path):
            os.makedirs(os.path.dirname(_notes_md_path), exist_ok=True)
            with open(_notes_md_path, 'w', encoding='utf-8') as _f:
                _f.write(_NOTES_MD_TEMPLATE)

        # 5. Assign default tools
        db.set_agent_tools(
            agent_id, ["bash", "runpy", "patch", "write_file", "read_file"]
        )

        # 6. Store settings
        db.set_setting("super_agent_id", agent_id)
        db.set_setting("agent_language", language)
        db.set_setting("sandbox_default_enabled", "1" if sandbox_enabled else "0")

        # 7. Enable all installed plugins
        for manifest_path in glob.glob(
            os.path.join(config.BASE_DIR, "plugins", "*", "plugin.json")
        ):
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
                plugin_id = manifest.get("id", "")
                if plugin_id:
                    db.set_setting(f"plugin_enabled:{plugin_id}", "1")
            except (json.JSONDecodeError, IOError):
                pass

        # 8. Enable all installed skills
        for manifest_path in glob.glob(
            os.path.join(config.BASE_DIR, "skills", "*", "skill.json")
        ):
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
                skill_id = manifest.get("id", "")
                if skill_id:
                    db.set_setting(f"skill_enabled:{skill_id}", "1")
            except (json.JSONDecodeError, IOError):
                pass

        # 9. Persist admin password to .env
        if password:
            from werkzeug.security import generate_password_hash

            pw_hash = generate_password_hash(password, method="pbkdf2:sha256")
            env_path = os.path.join(config.BASE_DIR, ".env")
            _update_env_var(env_path, "ADMIN_PASSWORD_HASH", pw_hash)

        return {"success": True, "agent_id": agent_id}

    except Exception as e:
        # Roll back partial DB state so retries work:
        # the agent (created at step 3) and model (created at step 1).
        # Use raw SQL because db.delete_agent() refuses to delete super agents.
        try:
            with db._connect() as conn:
                conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
                conn.execute("DELETE FROM agent_tools WHERE agent_id = ?", (agent_id,))
                conn.commit()
        except Exception:
            pass
        try:
            with db._connect() as conn:
                conn.execute("DELETE FROM llm_models WHERE id = ?", (model_id,))
                conn.commit()
        except Exception:
            pass
        return {"error": str(e)}


def run_reconfigure(
    provider: str,
    model_name: str,
    base_url: str,
    api_key: str,
    language: str = "english",
    sandbox_enabled: bool = False,
    password: str = "",
) -> dict:
    """
    Reconfigure an existing Evonic setup:
    1. Update or create LLM model entry in DB and set as default
    2. Build new system prompt with language
    3. Update super agent's system prompt
    4. Update agent sandbox setting
    5. Store settings (language, sandbox)
    6. Update admin password if provided

    Returns {'success': True, 'agent_id': str} or {'error': str}.
    Errors if super agent does not exist (must run setup first).
    """
    from routes.agents import _write_system_prompt

    # Must have super agent
    if not db.has_super_agent():
        return {"error": 'Super agent does not exist. Run "evonic setup" first.'}

    # Validate language
    if language not in LANGUAGE_PRESETS:
        language = "english"

    # Validate provider and model
    if not provider or provider not in PROVIDER_DEFAULTS:
        return {"error": f"Unknown provider: {provider}"}
    if not model_name.strip():
        return {"error": "Model name is required"}

    provider_cfg = PROVIDER_DEFAULTS[provider]
    resolved_base_url = (base_url or provider_cfg["base_url"]).rstrip("/")

    try:
        # 1. Update or create model in DB as default
        model_id = f"setup_{provider}"
        # Derive api_format: ollama + local → openai, ollama + remote → ollama native
        if provider == "ollama" and "ollama.com" in resolved_base_url:
            model_api_format = "ollama"
            model_type = "remote"
        else:
            model_api_format = provider_cfg.get("api_format", "openai")
            model_type = provider_cfg["type"]
        model_data = {
            "id": model_id,
            "name": f"{provider_cfg['label']} ({model_name})",
            "type": model_type,
            "provider": provider,
            "base_url": resolved_base_url,
            "api_key": api_key or "",
            "model_name": model_name,
            "is_default": 1,
            "enabled": 1,
            "api_format": model_api_format,
        }
        existing_model = db.get_model_by_id(model_id)
        if existing_model:
            # Update preserves the user's manual thinking toggle
            db.update_model(model_id, model_data)
        else:
            model_data["thinking"] = 1 if provider_cfg.get("default_thinking") else 0
            db.create_model(model_data)

        # 2. Build new system prompt (with language)
        base_prompt = build_system_prompt()
        lang_cfg = LANGUAGE_PRESETS.get(language, LANGUAGE_PRESETS["english"])
        full_prompt = base_prompt + "\n" + lang_cfg["instruction"] + "\n"

        # 3. Get super agent and update
        super_agent = db.get_super_agent()
        agent_id = super_agent["id"]

        # Write SYSTEM.md on disk
        _write_system_prompt(agent_id, full_prompt)

        # Update system_prompt in DB directly (update_agent does not allow it)
        with db._connect() as conn:
            conn.execute(
                "UPDATE agents SET system_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (full_prompt, agent_id),
            )
            conn.commit()

        # 4. Update agent sandbox setting
        db.update_agent(agent_id, {"sandbox_enabled": 1 if sandbox_enabled else 0})

        # 5. Store settings
        db.set_setting("agent_language", language)
        db.set_setting("sandbox_default_enabled", "1" if sandbox_enabled else "0")

        # 6. Update admin password if provided
        if password:
            from werkzeug.security import generate_password_hash

            pw_hash = generate_password_hash(password, method="pbkdf2:sha256")
            env_path = os.path.join(config.BASE_DIR, ".env")
            _update_env_var(env_path, "ADMIN_PASSWORD_HASH", pw_hash)

        return {"success": True, "agent_id": agent_id}

    except Exception as e:
        return {"error": str(e)}


def _update_env_var(env_path, key, value):
    """Update or add an environment variable in a .env file.

    Uses atomic write (write-to-temp-then-rename) so the .env file is
    never left empty or truncated if the process crashes mid-write.
    """
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(f"{key}={value}\n")
        return

    with open(env_path, "r") as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"{key} "):
            lines[i] = f"{key}={value}\n"
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{key}={value}\n")

    # Atomic write: write to temp file in same directory, then rename.
    # os.replace() is atomic on POSIX and Windows (same filesystem).
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(env_path),
                                        prefix=".env.")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.writelines(lines)
        os.replace(tmp_path, env_path)
    except Exception:
        # Clean up temp file on failure — don't leave litter behind.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

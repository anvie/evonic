"""Evonic CLI commands — start, stop, status, plugin, and skill management."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime

# Ensure the project root is on sys.path so we can import backend modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add bundled libraries to sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for lib_dir in ("lib",):
    lib_path = os.path.join(ROOT, lib_dir)
    if os.path.isdir(lib_path) and lib_path not in sys.path:
        sys.path.insert(0, lib_path)


# PID file location.
#
# After migration to the release-based layout the canonical run/ directory
# lives at ``<app_root>/shared/run/`` (supervisor writes the daemon PID
# there). Pre-migration installs still use ``<install_root>/run/``. Resolve
# whichever is present so ``evonic status``/``evonic stop`` find a daemon
# regardless of whether it was launched by the legacy in-process path or by
# supervisor.
def _resolve_pid_dir():
    shared_run = os.path.join(ROOT, "shared", "run")
    if os.path.isdir(shared_run):
        return shared_run
    return os.path.join(ROOT, "run")


PID_DIR = _resolve_pid_dir()
PID_FILE = os.path.join(PID_DIR, "evonic.pid")


# Banner colors by day of week (0=Monday, 1=Tuesday, ...)
_DAY_COLORS = [
    "\033[91m",  # Monday   - Bright Red
    "\033[35m",  # Tuesday  - Magenta
    "\033[32m",  # Wednesday - Green
    "\033[93m",  # Thursday - Bright Yellow
    "\033[34m",  # Friday   - Blue
    "\033[36m",  # Saturday - Cyan
    "\033[93m",  # Sunday   - Bright Yellow (gold)
]
_RESET = "\033[0m"

EVONIC_BANNER = (
    _DAY_COLORS[datetime.now().weekday()]
    + r"""

         ░░░░░░░░░░░░░░░░░░░░░░░░
       ░░▒▒███████████████████▒▒░░
     ░░▒▒██▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓██▒▒░░      ___________                  .__.
     ░░▒▒██▓█████████████████▓██▒▒░░      \_   _____/__  ______   ____ |__| _____
     ░░▒▒██▓█████ ██  ██ ████▓██▒▒░░       |    __)_\  \/ /    \ /    \|  |/ ____\
     ░░▒▒██▓█████████████████▓██▒▒░░       |        \\   (  ()  )   |  \  \  \____
     ░░▒▒███████████████████████▒▒░░      /_______  / \_/ \____/|___|  /__|\___  /
       ░░▒▒░░░░░░░░░░░░░░░░░░▒▒░░                 \/                 \/        \/
        ▓▓ ░░▓▓ ░░ ▓▓ ░░▓▓ ░░▓▓
      ▒▒ ░░ ▒▒ ▓▓  ▒▒  ▓▓   ▒▒▒
        ░░ ░▒░  ▓▓  ▒▒  ▓▓  ░▓
          ▒▒ ▒▒░▒▒  ▒▒░░▒  ▒▒
            ░░    ▓▓▓▓    ▓▓
              ▒▒        ▒▒

"""
    + _RESET
)


def _is_setup_done():
    """Check if evonic setup has been completed (super agent exists)."""
    try:
        db = _get_db()
        return db.has_super_agent()
    except Exception:
        return False


def _get_pid():
    """Read PID from file. Returns None if not found or file doesn't exist."""
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, "r") as f:
            content = f.read().strip()
            if not content:
                return None
            return int(content)
    except (ValueError, IOError):
        return None


def _is_running(pid):
    """Check if a process with the given PID is actually running."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # Signal 0 checks if process exists
        return True
    except OSError:
        return False


def _write_pid(pid):
    """Write PID to file."""
    os.makedirs(PID_DIR, exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(pid))


def _remove_pid():
    """Remove PID file."""
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)


def start_server(port=None, host=None, debug=None, daemon=False):
    """Start the Flask server. Runs in foreground by default; use daemon=True to background."""
    # Check if setup is complete
    # if not _is_setup_done():
    #    print("Evonic has not been set up yet.")
    #    print("Please run 'evonic setup' first to configure your platform.")
    #    sys.exit(1)

    # Check if already running
    existing_pid = _get_pid()
    if _is_running(existing_pid):
        print(f"Server is already running (PID: {existing_pid})")
        try:
            import config

            print(f"Port: {port or config.PORT}")
        except Exception:
            pass
        return

    # Import config to get defaults
    try:
        import config

        if port is None:
            port = config.PORT
        if host is None:
            host = config.HOST
        if debug is None:
            debug = config.DEBUG
    except Exception:
        pass

    # Daemon (background) mode: spawn detached subprocess
    if daemon:
        # Check if in release mode (self-update capable)
        current_link = os.path.join(ROOT, "current")
        sup_cfg_path = os.path.join(ROOT, "supervisor", "config.json")
        release_mode = os.path.islink(current_link) and os.path.exists(sup_cfg_path)

        if release_mode:
            # Start supervisor daemon (handles release + self-update)
            sup_script = os.path.join(ROOT, "supervisor", "supervisor.py")
            proc = subprocess.Popen(
                [sys.executable, sup_script],
                start_new_session=True,
            )
            time.sleep(2)
            if _is_running(proc.pid):
                print(f"Supervisor started (PID: {proc.pid})")
                print(
                    f"Server will run from the current release with automatic self-update"
                )
            else:
                print("Failed to start supervisor. Check the log for details.")
                sys.exit(1)
            return

        # Flat mode (no releases): run app.py directly (legacy behavior)
        env = os.environ.copy()
        if port is not None:
            env["PORT"] = str(port)
        if host is not None:
            env["HOST"] = host
        if debug is not None:
            env["DEBUG"] = "1" if debug else "0"

        # Create run directory for PID file and logs
        os.makedirs(PID_DIR, exist_ok=True)

        app_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "app.py"
        )
        proc = subprocess.Popen(
            [sys.executable, app_path],
            env=env,
            stdout=open(os.path.join(PID_DIR, "server.log"), "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,  # Detach from terminal
        )

        time.sleep(1)

        if _is_running(proc.pid):
            _write_pid(proc.pid)
            print(f"Server started in background (PID: {proc.pid})")
            print(f"Host: {host}")
            print(f"Port: {port}")
            print(f"URL: http://{host if host != '0.0.0.0' else 'localhost'}:{port}")
            if debug:
                print("Debug mode: ON")
        else:
            print("Failed to start server. Check run/server.log for details.")
            sys.exit(1)
        return

    # Foreground mode (default): run in-process
    env = os.environ.copy()
    if port is not None:
        env["PORT"] = str(port)
    if host is not None:
        env["HOST"] = host
    if debug is not None:
        env["DEBUG"] = "1" if debug else "0"
    os.environ.update(env)

    if debug:
        print("Debug mode: ON")

    # Foreground release-mode parity: if the app was migrated to release-based
    # layout, exec the release's python on its app.py (mirrors daemon path).
    # Falls through to legacy in-process import when no release is staged.
    current_link = os.path.join(ROOT, "current")
    sup_cfg_path = os.path.join(ROOT, "supervisor", "config.json")
    release_mode = os.path.islink(current_link) and os.path.exists(sup_cfg_path)
    if release_mode:
        release_path = os.path.realpath(current_link)
        if sys.platform == "win32":
            release_py = os.path.join(release_path, ".venv", "Scripts", "python.exe")
        else:
            release_py = os.path.join(release_path, ".venv", "bin", "python")
        release_app = os.path.join(release_path, "app.py")
        if os.path.exists(release_py) and os.path.exists(release_app):
            print(EVONIC_BANNER)
            print(f"Starting server (Ctrl+C to stop)")
            print(f"Host: {host}")
            print(f"Port: {port}")
            print(f"URL: http://{host if host != '0.0.0.0' else 'localhost'}:{port}")
            os.chdir(release_path)
            os.execv(release_py, [release_py, release_app])
            # execv replaces the process; lines below unreachable.

    try:
        from app import app
    except ModuleNotFoundError as e:
        print(f"\nError: Missing required dependency: {e.name}")
        print("Please install dependencies first:")
        print("  pip install -r requirements.txt")
        print("\nPlease run the setup:")
        print("  evonic setup")
        sys.exit(1)

    print(EVONIC_BANNER)

    print(f"Starting server (Ctrl+C to stop)")
    print(f"Host: {host}")
    print(f"Port: {port}")
    print(f"URL: http://{host if host != '0.0.0.0' else 'localhost'}:{port}")

    app.run(host=host, port=port, debug=debug, use_reloader=False)


def stop_server():
    """Stop the running server."""
    pid = _get_pid()

    if pid is None:
        print("No PID file found. Server may not be running.")
        # Clean up stale PID file if exists
        _remove_pid()
        return

    if not _is_running(pid):
        print(f"Process {pid} is not running. Cleaning up stale PID file.")
        _remove_pid()
        return

    # Try graceful shutdown first (SIGTERM)
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sending SIGTERM to server (PID: {pid})...")
    except OSError as e:
        print(f"Failed to send SIGTERM: {e}")
        _remove_pid()
        return

    # Wait for process to stop (max 10 seconds)
    for i in range(10):
        time.sleep(1)
        if not _is_running(pid):
            print(f"Server stopped (PID: {pid})")
            _remove_pid()
            return

    # Force kill if still running. SIGKILL is POSIX-only, so on Windows we
    # use `taskkill /F` (the same approach as supervisor/supervisor.py).
    print("Server didn't stop gracefully. Force-killing...")
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, signal.SIGKILL)
        time.sleep(1)
        print(f"Server killed (PID: {pid})")
    except OSError as e:
        print(f"Failed to force-kill: {e}")

    _remove_pid()

    # Also stop supervisor if running
    spid = _get_supervisor_pid()
    if spid and _is_running(spid):
        try:
            os.kill(spid, signal.SIGTERM)
            print(f"Sending SIGTERM to supervisor (PID: {spid})...")
        except OSError as e:
            print(f"Failed to send SIGTERM to supervisor: {e}")


def status_server():
    """Check if the server is running."""
    pid = _get_pid()

    if pid is None:
        print("Server is not running (no PID file)")
        return 1

    if _is_running(pid):
        print(f"Server is running (PID: {pid})")
        # Try to get port from config
        try:
            import config

            print(f"Port: {config.PORT}")
            print(f"URL: http://localhost:{config.PORT}")
        except Exception:
            pass
        return 0
    else:
        print(f"Server is not running (stale PID file for PID {pid})")
        _remove_pid()
        return 1


def restart_server():
    """Stop the running server, then start it again in daemon mode."""
    print("Stopping server...")
    stop_server()

    # Small delay to ensure ports are freed
    time.sleep(1)

    print("Starting server...")
    start_server(daemon=True)


# ─── Plugin Management ────────────────────────────────────────────────────────


def _get_plugin_manager():
    """Lazily create a PluginManager instance."""
    from backend.plugin_manager import PluginManager

    return PluginManager()


def plugin_list():
    """List all installed plugins in a table format."""
    pm = _get_plugin_manager()
    plugins = pm.list_plugins()

    if not plugins:
        print("No plugins installed.")
        return

    # Column widths
    id_width = max(len("ID"), max((len(p.get("id", "")) for p in plugins), default=2))
    name_width = max(
        len("Name"), max((len(p.get("name", "")) for p in plugins), default=4)
    )
    ver_width = max(
        len("Version"),
        max((len(str(p.get("version", ""))) for p in plugins), default=7),
    )
    status_width = len("Status")
    events_width = len("Events")

    header = (
        f"{'ID':<{id_width}}  {'Name':<{name_width}}  {'Version':<{ver_width}}  "
        f"{'Status':<{status_width}}  {'Events':>{events_width}}"
    )
    sep = "-" * len(header)

    print(header)
    print(sep)

    for p in plugins:
        pid = p.get("id", "")
        pname = p.get("name", pid)
        ver = p.get("version", "-")
        status = "enabled" if p.get("enabled") else "disabled"
        events = p.get("event_count", 0)

        print(
            f"{pid:<{id_width}}  {pname:<{name_width}}  {ver:<{ver_width}}  "
            f"{status:<{status_width}}  {events:>{events_width}}"
        )


def plugin_install(source):
    """Install a plugin from a zip file or directory path."""
    if not source:
        print("Error: source path is required.")
        print("Usage: evonic plugin install <path-to-zip-or-directory>")
        sys.exit(1)

    # Resolve to absolute path
    source = os.path.abspath(source)

    if not os.path.exists(source):
        print(f"Error: path not found: {source}")
        sys.exit(1)

    pm = _get_plugin_manager()

    if source.endswith(".zip") and os.path.isfile(source):
        result = pm.install_plugin(source)
    elif os.path.isdir(source):
        result = pm.install_plugin_from_dir(source)
    else:
        # Try as zip first, then as directory
        if os.path.isfile(source):
            result = pm.install_plugin(source)
        else:
            print(f"Error: source is not a valid file or directory: {source}")
            sys.exit(1)

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)

    plugin_id = result.get("id", "unknown")
    plugin_name = result.get("name", plugin_id)
    version = result.get("version", "?")
    print(f"Plugin installed: {plugin_name} ({plugin_id}) v{version}")


def plugin_uninstall(name):
    """Uninstall a plugin by its ID."""
    if not name:
        print("Error: plugin name is required.")
        print("Usage: evonic plugin uninstall <plugin-id>")
        sys.exit(1)

    pm = _get_plugin_manager()
    result = pm.uninstall_plugin(name)

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)

    print(f"Plugin uninstalled: {name}")


def plugin_enable(plugin_id):
    """Enable a plugin by its ID."""
    if not plugin_id:
        print("Error: plugin-id is required.")
        print("Usage: evonic plugin enable <plugin-id>")
        sys.exit(1)

    pm = _get_plugin_manager()
    result = pm.set_plugin_enabled(plugin_id, enabled=True)

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)

    print(f"Plugin enabled: {plugin_id}")


def plugin_disable(plugin_id):
    """Disable a plugin by its ID."""
    if not plugin_id:
        print("Error: plugin-id is required.")
        print("Usage: evonic plugin disable <plugin-id>")
        sys.exit(1)

    pm = _get_plugin_manager()
    result = pm.set_plugin_enabled(plugin_id, enabled=False)

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)

    print(f"Plugin disabled: {plugin_id}")


def plugin_reload(plugin_id):
    """Reload a plugin by its ID (useful during development)."""
    if not plugin_id:
        print("Error: plugin_id is required")
        return
    
    pm = _get_plugin_manager()
    try:
        pm.reload_plugin(plugin_id)
        print(f"Plugin reloaded: {plugin_id}")
    except Exception as e:
        print(f"Error reloading plugin: {e}")


def plugin_hotreload_enable(plugin_id=None):
    """Enable hot reload for a plugin or globally."""
    from backend.plugin_hot_reload import hot_reload_manager
    
    if plugin_id:
        # Enable for specific plugin
        success = hot_reload_manager.enable_for_plugin(plugin_id)
        if success:
            print(f"Hot reload enabled for plugin: {plugin_id}")
            print("Plugin will automatically reload when files change")
        else:
            print(f"Error: Could not enable hot reload for plugin: {plugin_id}")
    else:
        # Enable globally
        hot_reload_manager.enable_globally()
        print("Hot reload enabled globally")
        print("Use 'plugin hotreload-enable <plugin_id>' to watch specific plugins")


def plugin_hotreload_disable(plugin_id=None):
    """Disable hot reload for a plugin or globally."""
    from backend.plugin_hot_reload import hot_reload_manager
    
    if plugin_id:
        # Disable for specific plugin
        success = hot_reload_manager.disable_for_plugin(plugin_id)
        if success:
            print(f"Hot reload disabled for plugin: {plugin_id}")
        else:
            print(f"Hot reload was not enabled for plugin: {plugin_id}")
    else:
        # Disable globally
        hot_reload_manager.disable_globally()
        print("Hot reload disabled globally")


def plugin_hotreload_status():
    """Show hot reload status."""
    from backend.plugin_hot_reload import hot_reload_manager
    
    status = hot_reload_manager.get_status()
    
    print("\n=== Plugin Hot Reload Status ===")
    print(f"Globally enabled: {status['enabled']}")
    print(f"Active watchers: {status['active_watchers']}")
    
    if status['watched_plugins']:
        print(f"\nWatched plugins ({len(status['watched_plugins'])}):")
        for plugin_id in sorted(status['watched_plugins']):
            print(f"  - {plugin_id}")
    else:
        print("\nNo plugins being watched")
    
    if status['pending_reloads']:
        print(f"\nPending reloads ({len(status['pending_reloads'])}):")
        for plugin_id in status['pending_reloads']:
            print(f"  - {plugin_id}")
    
    print()


def plugin_new():
    """Interactive wizard to scaffold a new plugin project."""
    import re

    PLUGINS_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "plugins"
    )

    print("")
    print("  \u26a1 Evonic Plugin Scaffolder")
    print("  \u2500" * 28)
    print("")

    name = ""
    while not name.strip():
        name = input("  Plugin name (e.g. My Cool Plugin): ").strip()
    plugin_id = re.sub(r"[^a-z0-9_]", "_", name.lower().replace(" ", "_"))
    plugin_id = re.sub(r"_+", "_", plugin_id).strip("_")
    if not plugin_id:
        plugin_id = "unnamed_plugin"

    print(f"  Plugin ID:  {plugin_id}")
    print("")

    description = (
        input("  Description: ").strip() or f"A simple {name} plugin for Evonic"
    )
    author = input("  Author / contact email: ").strip() or "you@example.com"

    dest = os.path.join(PLUGINS_DIR, plugin_id)
    if os.path.exists(dest):
        print(f"\n  Error: Directory already exists: {dest}")
        sys.exit(1)

    os.makedirs(dest)

    manifest = {
        "id": plugin_id,
        "name": name.strip(),
        "version": "1.0.0",
        "description": description,
        "author": author,
        "enabled": True,
        "events": [],
        "nav_items": [],
    }
    import json

    with open(os.path.join(dest, "plugin.json"), "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    handler_py = (
        '"""Handler for the ' + name + ' plugin."""\n\n\n'
        "def on_load(sdk):\n"
        '    """Called when the plugin is loaded. Use sdk.log() for logging."""\n'
        '    sdk.log("' + name + ' plugin loaded (v1.0.0)")\n\n\n'
        "def on_unload(sdk):\n"
        '    """Called when the plugin is unloaded."""\n'
        '    sdk.log("' + name + ' plugin unloaded")\n\n\n'
        "def on_tool_call(tool_name, args, sdk):\n"
        '    """Handle a tool call from an agent.\n\n'
        "    Args:\n"
        "        tool_name: Name of the tool being called.\n"
        "        args: Dictionary of arguments passed to the tool.\n"
        "        sdk: Plugin SDK instance for logging, config access, etc.\n\n"
        "    Returns:\n"
        "        A string response to send back to the agent.\n"
        '    """\n'
        '    sdk.log(f"Tool called: {tool_name} with args={args}")\n'
        '    return f"Executed {tool_name} with {len(args)} argument(s)"\n'
    )
    with open(os.path.join(dest, "handler.py"), "w") as f:
        f.write(handler_py)

    readme = f"""# {name}

**Plugin ID:** `{plugin_id}`
**Version:** 1.0.0
**Author:** {author}

## Description

{description}

## Installation

```bash
evonic plugin install plugins/{plugin_id}
```

## Development

### Structure

```
plugins/{plugin_id}/
\u251c\u2500\u2500 plugin.json      # Plugin manifest
\u251c\u2500\u2500 handler.py       # Event and tool handlers
\u2514\u2500\u2500 README.md        # This file
```

### Adding tools

Add tool definitions to the `tools` key in `plugin.json` and implement
the handler logic in `handler.py`.
"""
    with open(os.path.join(dest, "README.md"), "w") as f:
        f.write(readme.lstrip("\n"))

    with open(os.path.join(dest, ".gitignore"), "w") as f:
        f.write("__pycache__/\n*.pyc\n")

    print(f"\n  \u2705 Plugin scaffold created: plugins/{plugin_id}/")
    print(f"     \u251c\u2500\u2500 plugin.json")
    print(f"     \u251c\u2500\u2500 handler.py")
    print(f"     \u251c\u2500\u2500 README.md")
    print(f"     \u2514\u2500\u2500 .gitignore")
    print("")
    print(f"  Next steps:")
    print(f"    1. cd plugins/{plugin_id}")
    print(f"    2. Edit plugin.json to add tools, events, routes, etc.")
    print(f"    3. Implement handlers in handler.py")
    print(f"    4. Install the plugin: evonic plugin install plugins/{plugin_id}")
    print("")


# ─── Skill Management ──────────────────────────────────────────────────────────

# Built-in/core skills that cannot be removed via CLI
_SKILL_CORE_IDS = {"hello_world"}


def _get_skills_manager():
    """Lazily create a SkillsManager instance."""
    from backend.skills_manager import SkillsManager

    return SkillsManager()


def skill_list():
    """List all installed skills in a table format."""
    sm = _get_skills_manager()
    skills = sm.list_skills()

    if not skills:
        print("No skills installed.")
        return

    # Column widths
    id_width = max(len("ID"), max((len(s.get("id", "")) for s in skills), default=2))
    name_width = max(
        len("Name"), max((len(s.get("name", "")) for s in skills), default=4)
    )
    ver_width = max(
        len("Version"), max((len(str(s.get("version", ""))) for s in skills), default=7)
    )
    status_width = len("Status")
    tools_width = len("Tools")

    header = (
        f"{'ID':<{id_width}}  {'Name':<{name_width}}  {'Version':<{ver_width}}  "
        f"{'Status':<{status_width}}  {'Tools':>{tools_width}}"
    )
    sep = "-" * len(header)

    print(header)
    print(sep)

    for s in skills:
        sid = s.get("id", "")
        sname = s.get("name", sid)
        ver = s.get("version", "-")
        status = "enabled" if s.get("enabled") else "disabled"
        tools = s.get("tool_count", 0)

        print(
            f"{sid:<{id_width}}  {sname:<{name_width}}  {ver:<{ver_width}}  "
            f"{status:<{status_width}}  {tools:>{tools_width}}"
        )


def skill_add(source):
    """Install a skill from local path, zip file, or GitHub URL."""
    import tempfile

    sm = _get_skills_manager()

    # Check if source is a GitHub URL
    temp_dir = None
    temp_zip = None
    actual_source = source

    if source.startswith(
        ("https://github.com/", "http://github.com/", "git@github.com:")
    ):
        print(f"Cloning from GitHub: {source}")
        temp_dir = tempfile.mkdtemp()

        # Convert GitHub URL to git clone URL
        if source.startswith("git@github.com:"):
            git_url = source.replace("git@github.com:", "git@github.com:")
        elif source.startswith(("https://github.com/", "http://github.com/")):
            git_url = source
        else:
            git_url = source

        # Remove .git suffix if present
        git_url = git_url.rstrip(".git")

        try:
            result = subprocess.run(
                ["git", "clone", git_url, temp_dir],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                print(f"Error cloning repository: {result.stderr.strip()}")
                sys.exit(1)
        except subprocess.TimeoutExpired:
            print("Error: Git clone timed out after 120 seconds")
            sys.exit(1)
        except FileNotFoundError:
            print("Error: git command not found. Please install git first.")
            sys.exit(1)

        actual_source = temp_dir
    elif source.endswith(".zip"):
        if not os.path.isfile(source):
            print(f"Error: File not found: {source}")
            sys.exit(1)
        actual_source = source
    elif os.path.isdir(source):
        actual_source = source
    else:
        print(f"Error: Invalid source. Must be a local path, .zip file, or GitHub URL.")
        print("Examples:")
        print("  evonic skill add ./my-skill")
        print("  evonic skill add /path/to/skill.zip")
        print("  evonic skill add https://github.com/user/repo")
        sys.exit(1)

    # Install the skill
    try:
        if actual_source.endswith(".zip"):
            result = sm.install_skill(actual_source)
        else:
            result = sm.install_skill_from_dir(actual_source)
    finally:
        # Clean up temp dir if we cloned from GitHub
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)

    skill_id = result.get("id", "unknown")
    skill_name = result.get("name", skill_id)
    version = result.get("version", "?")
    print(f"Skill installed: {skill_name} ({skill_id}) v{version}")


def skill_get(skill_id):
    """Show details of a specific skill."""
    if not skill_id:
        print("Error: skill_id is required.")
        print("Usage: evonic skill get <skill_id>")
        sys.exit(1)

    sm = _get_skills_manager()
    skill = sm.get_skill(skill_id)

    if skill is None:
        print(f"Error: Skill not found: {skill_id}")
        sys.exit(1)

    print(f"ID:        {skill.get('id', '')}")
    print(f"Name:      {skill.get('name', '')}")
    print(f"Version:   {skill.get('version', '-')}")
    print(f"Status:    {'enabled' if skill.get('enabled') else 'disabled'}")
    print(f"Description: {skill.get('description', 'N/A')}")

    # Tools
    tools = skill.get("tools", [])
    if tools:
        print(f"\nTools ({len(tools)}):")
        for t in tools:
            tname = t.get("name", "")
            tdesc = t.get("description", "")
            print(f"  - {tname}")
            if tdesc:
                print(f"    {tdesc}")

    # Variables
    variables = skill.get("variables", [])
    if variables:
        print(f"\nVariables ({len(variables)}):")
        for v in variables:
            vname = v.get("name", v.get("key", ""))
            vdesc = v.get("description", "")
            vdefault = v.get("default", "")
            print(f"  - {vname}")
            if vdesc:
                print(f"    {vdesc}")
            if vdefault is not None:
                print(f"    Default: {vdefault}")


def skill_rm(skill_id):
    """Uninstall a skill by its ID."""
    if not skill_id:
        print("Error: skill_id is required.")
        print("Usage: evonic skill rm <skill_id>")
        sys.exit(1)

    # Protect core/built-in skills
    if skill_id in _SKILL_CORE_IDS:
        print(f"Error: Cannot remove built-in skill: {skill_id}")
        sys.exit(1)

    sm = _get_skills_manager()
    result = sm.uninstall_skill(skill_id)

    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)

    print(f"Skill uninstalled: {skill_id}")


# ─── Skillset Management ───────────────────────────────────────────────────────────────


def _get_skillsets():
    """Lazily import skillsets module."""
    from backend import skillsets

    return skillsets


def skillset_list():
    """List all available skillset templates in a table format."""
    mod = _get_skillsets()
    skillsets = mod.list_skillsets()

    if not skillsets:
        print("No skillset templates available.")
        return

    # Column widths
    id_width = max(len("ID"), max((len(s.get("id", "")) for s in skillsets), default=2))
    name_width = max(
        len("Name"), max((len(s.get("name", "")) for s in skillsets), default=4)
    )
    desc_width = max(
        len("Description"),
        max((len(s.get("description", "")) for s in skillsets), default=11),
    )
    tools_width = len("Tools")
    skills_width = len("Skills")

    header = (
        f"{'ID':<{id_width}}  {'Name':<{name_width}}  {'Description':<{desc_width}}  "
        f"{'Tools':>{tools_width}}  {'Skills':>{skills_width}}"
    )
    sep = "-" * len(header)

    print(header)
    print(sep)

    for s in skillsets:
        sid = s.get("id", "")
        sname = s.get("name", sid)
        desc = s.get("description", "")
        tools = s.get("tools_count", 0)
        skills = s.get("skills_count", 0)

        print(
            f"{sid:<{id_width}}  {sname:<{name_width}}  {desc:<{desc_width}}  "
            f"{tools:>{tools_width}}  {skills:>{skills_width}}"
        )


def skillset_get(skillset_id):
    """Show details of a specific skillset template."""
    if not skillset_id:
        print("Error: skillset_id is required.")
        print("Usage: evonic skillset get <skillset_id>")
        sys.exit(1)

    mod = _get_skillsets()
    skillset = mod.get_skillset(skillset_id)

    if skillset is None:
        print(f"Error: Skillset not found: {skillset_id}")
        sys.exit(1)

    print(f"ID:          {skillset.get('id', '')}")
    print(f"Name:        {skillset.get('name', '')}")
    print(f"Description: {skillset.get('description', 'N/A')}")
    print(f"Model:       {skillset.get('model', '(default)')}")

    # System prompt (truncated)
    sp = skillset.get("system_prompt", "")
    if sp:
        if len(sp) > 200:
            print(f"\nSystem Prompt: {sp[:200]}...")
        else:
            print(f"\nSystem Prompt: {sp}")

    # Tools
    tools = skillset.get("tools", [])
    if tools:
        print(f"\nTools ({len(tools)}):")
        for t in tools:
            print(f"  - {t}")

    # Skills
    skills = skillset.get("skills", [])
    if skills:
        print(f"\nSkills ({len(skills)}):")
        for sk in skills:
            print(f"  - {sk}")

    # KB files
    kb_files = skillset.get("kb_files", {})
    if kb_files:
        print(f"\nKB Files ({len(kb_files)}):")
        for k, v in kb_files.items():
            print(f"  - {k}")


def skillset_apply(skillset_id, agent_id, name=None, description=None, model=None):
    """Create a new agent from a skillset template."""
    if not skillset_id:
        print("Error: skillset_id is required.")
        print("Usage: evonic skillset apply <skillset_id> --agent-id <id>")
        sys.exit(1)

    if not agent_id:
        print("Error: --agent-id is required.")
        print("Usage: evonic skillset apply <skillset_id> --agent-id <id>")
        sys.exit(1)

    mod = _get_skillsets()
    skillset = mod.get_skillset(skillset_id)

    if skillset is None:
        print(f"Error: Skillset not found: {skillset_id}")
        sys.exit(1)

    # Build agent data
    agent_data = {
        "id": agent_id,
    }
    if name:
        agent_data["name"] = name
    if description:
        agent_data["description"] = description
    if model:
        agent_data["model"] = model

    # Resolve the skillset to get actual tool IDs
    resolved = mod.resolve_skillset(skillset_id)
    if resolved:
        agent_data["tools"] = resolved.get("resolved_tools", [])

        unresolved = resolved.get("unresolved_tools", [])
        if unresolved:
            print(
                f"Warning: {len(unresolved)} tool(s) not found and will be skipped: {', '.join(unresolved)}"
            )

    # Apply skillset defaults
    merged = mod.apply_skillset(skillset_id, agent_data)

    # Create the agent via platform API
    try:
        import requests

        import config

        base_url = f"http://{getattr(config, 'HOST', 'localhost')}:{getattr(config, 'PORT', 8080)}"

        payload = {
            "id": merged.get("id", ""),
            "name": merged.get("name", ""),
            "description": merged.get("description", ""),
            "system_prompt": merged.get("system_prompt", ""),
        }
        if merged.get("model"):
            payload["model"] = merged["model"]

        resp = requests.post(
            f"{base_url}/api/agent/create",
            json=payload,
            timeout=15,
        )

        if resp.status_code == 409:
            print(f"Error: Agent '{agent_id}' already exists.")
            sys.exit(1)
        resp.raise_for_status()

        # Assign tools
        tools = merged.get("tools", [])
        if tools:
            resp2 = requests.post(
                f"{base_url}/api/agent/{agent_id}/tools",
                json={"tool_ids": tools},
                timeout=15,
            )
            if resp2.status_code != 200:
                print(f"Warning: Failed to assign tools: {resp2.text}")

        # Enable skills
        skills = merged.get("skills", [])
        for skill_id in skills:
            resp3 = requests.post(
                f"{base_url}/api/agent/{agent_id}/skill/{skill_id}/enable",
                timeout=15,
            )
            if resp3.status_code != 200:
                print(f"Warning: Failed to enable skill '{skill_id}': {resp3.text}")

        agent_name = merged.get("name", agent_id)
        print(f"Agent created: {agent_name} ({agent_id}) from skillset '{skillset_id}'")

    except requests.exceptions.ConnectionError:
        print("Error: Cannot connect to Evonic server. Is it running?")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


# ─── Agent Management ────────────────────────────────────────────────────────────────


def _get_db():
    """Lazily create a Database instance."""
    from models.db import db

    return db


def agent_list():
    """List all agents in a table format."""
    db = _get_db()
    agents = db.get_agents()

    if not agents:
        print("No agents found.")
        return

    # Column widths
    id_width = max(len("ID"), max((len(a.get("id", "")) for a in agents), default=2))
    name_width = max(
        len("Name"), max((len(a.get("name", "")) for a in agents), default=4)
    )
    status_width = len("Status")
    tools_width = len("Tools")
    channels_width = len("Channels")

    header = (
        f"{'ID':<{id_width}}  {'Name':<{name_width}}  {'Status':<{status_width}}  "
        f"{'Tools':>{tools_width}}  {'Channels':>{channels_width}}"
    )
    sep = "-" * len(header)

    print(header)
    print(sep)

    for a in agents:
        aid = a.get("id", "")
        aname = a.get("name", aid)
        status = "enabled" if a.get("enabled", True) else "disabled"
        tool_count = len(db.get_agent_tools(aid))
        channels = db.get_channels(aid)
        channel_count = len(channels)

        print(
            f"{aid:<{id_width}}  {aname:<{name_width}}  {status:<{status_width}}  "
            f"{tool_count:>{tools_width}}  {channel_count:>{channels_width}}"
        )


def agent_get(agent_id):
    """Show details of a specific agent."""
    if not agent_id:
        print("Error: agent_id is required.")
        print("Usage: evonic agent get <agent_id>")
        sys.exit(1)

    db = _get_db()
    agent = db.get_agent(agent_id)

    if agent is None:
        print(f"Error: Agent not found: {agent_id}")
        sys.exit(1)

    print(f"ID:          {agent.get('id', '')}")
    print(f"Name:        {agent.get('name', '')}")
    print(f"Description: {agent.get('description', 'N/A')}")
    print(f"Status:      {'enabled' if agent.get('enabled', True) else 'disabled'}")
    print(f"Super:       {'yes' if agent.get('is_super', False) else 'no'}")
    model = agent.get("model") or "(default)"
    print(f"Model:       {model}")

    # System prompt (truncated)
    sp = agent.get("system_prompt", "")
    if sp:
        if len(sp) > 200:
            print(f"\nSystem Prompt: {sp[:200]}...")
        else:
            print(f"\nSystem Prompt: {sp}")

    # Tools
    tools = db.get_agent_tools(agent_id)
    if tools:
        print(f"\nTools ({len(tools)}):")
        for t in tools:
            print(f"  - {t}")

    # Channels
    channels = db.get_channels(agent_id)
    if channels:
        print(f"\nChannels ({len(channels)}):")
        for c in channels:
            cname = c.get("name", c.get("type", ""))
            print(f"  - {cname}")


def agent_add(agent_id, name, description=None, model=None, skillset=None):
    """Create a new agent, optionally from a skillset template."""
    if not agent_id:
        print("Error: agent_id is required.")
        print(
            "Usage: evonic agent add <id> --name <name> [--description] [--model] [--skillset]"
        )
        sys.exit(1)

    if not name:
        print("Error: --name is required.")
        print("Usage: evonic agent add <id> --name <name>")
        sys.exit(1)

    import re

    agent_id = agent_id.strip().lower()
    if not re.match(r"^[a-z0-9_]+$", agent_id):
        print(
            "Error: Invalid ID. Use only lowercase alphanumeric characters and underscores (snake_case)."
        )
        sys.exit(1)

    db = _get_db()
    if db.get_agent(agent_id):
        print(f"Error: Agent '{agent_id}' already exists.")
        sys.exit(1)

    # Resolve skillset if provided
    resolved_tools = []
    system_prompt = ""
    skills = []

    if skillset:
        from backend import skillsets as ss_mod

        skillset_data = ss_mod.get_skillset(skillset)
        if skillset_data is None:
            print(f"Error: Skillset not found: {skillset}")
            sys.exit(1)

        system_prompt = skillset_data.get("system_prompt", "")
        skills = skillset_data.get("skills", [])

        resolved = ss_mod.resolve_skillset(skillset)
        if resolved:
            resolved_tools = resolved.get("resolved_tools", [])
            unresolved = resolved.get("unresolved_tools", [])
            if unresolved:
                print(
                    f"Warning: {len(unresolved)} tool(s) not found and will be skipped: {', '.join(unresolved)}"
                )

    # Create agent directory and KB
    AGENTS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents"
    )
    agent_dir = os.path.join(AGENTS_DIR, agent_id)
    kb_dir = os.path.join(agent_dir, "kb")
    os.makedirs(kb_dir, exist_ok=True)

    # Create workspace directory at shared/agents/[agent-id]
    workspace_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "shared",
        "agents",
        agent_id,
    )
    os.makedirs(workspace_dir, exist_ok=True)

    # Write system prompt file
    sp_path = os.path.join(agent_dir, "SYSTEM.md")
    with open(sp_path, "w", encoding="utf-8") as f:
        f.write(system_prompt)

    # Create in DB
    try:
        db.create_agent(
            {
                "id": agent_id,
                "name": name,
                "description": description or "",
                "system_prompt": system_prompt,
                "model": model or None,
                "workspace": workspace_dir,
            }
        )

        # Assign tools from skillset
        if resolved_tools:
            db.set_agent_tools(agent_id, resolved_tools)

        print(f"Agent created: {name} ({agent_id})")
        if skillset:
            print(f"  Applied skillset: {skillset}")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def agent_enable(agent_id):
    """Enable an agent."""
    if not agent_id:
        print("Error: agent_id is required.")
        print("Usage: evonic agent enable <agent_id>")
        sys.exit(1)

    db = _get_db()
    if not db.get_agent(agent_id):
        print(f"Error: Agent not found: {agent_id}")
        sys.exit(1)

    db.update_agent(agent_id, {"enabled": True})
    print(f"Agent enabled: {agent_id}")


def agent_disable(agent_id):
    """Disable an agent."""
    if not agent_id:
        print("Error: agent_id is required.")
        print("Usage: evonic agent disable <agent_id>")
        sys.exit(1)

    db = _get_db()
    agent = db.get_agent(agent_id)

    if not agent:
        print(f"Error: Agent not found: {agent_id}")
        sys.exit(1)

    if agent.get("is_super"):
        print("Error: Super agent cannot be disabled.")
        sys.exit(1)

    db.update_agent(agent_id, {"enabled": False})
    print(f"Agent disabled: {agent_id}")


def agent_remove(agent_id):
    """Remove an agent with interactive confirmation."""
    if not agent_id:
        print("Error: agent_id is required.")
        print("Usage: evonic agent remove <agent_id>")
        sys.exit(1)

    db = _get_db()
    agent = db.get_agent(agent_id)

    if not agent:
        print(f"Error: Agent not found: {agent_id}")
        sys.exit(1)

    if agent.get("is_super"):
        print("Error: Super agent cannot be deleted.")
        sys.exit(1)

    # Show agent details and ask for confirmation
    aname = agent.get("name", agent_id)
    status = "enabled" if agent.get("enabled", True) else "disabled"
    print(f"Agent to remove:")
    print(f"  ID:        {agent_id}")
    print(f"  Name:      {aname}")
    print(f"  Status:    {status}")

    try:
        response = input("Are you sure? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        print("Aborted.")
        sys.exit(1)

    if response not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

    try:
        db.delete_agent(agent_id)
        agent_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "agents",
            agent_id,
        )
        if os.path.isdir(agent_dir):
            shutil.rmtree(agent_dir)
        print(f"Agent removed: {agent_id}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


# ─── Model Management ────────────────────────────────────────────────────────────────


def model_list():
    """List all LLM models in a table format."""
    db = _get_db()
    models = db.get_llm_models()

    if not models:
        print("No models configured.")
        return

    # Column widths
    id_width = max(len("ID"), max((len(m.get("id", "")) for m in models), default=2))
    name_width = max(
        len("Name"), max((len(m.get("name", "")) for m in models), default=4)
    )
    provider_width = max(
        len("Provider"),
        max((len(str(m.get("provider", ""))) for m in models), default=8),
    )

    header = (
        f"{'ID':<{id_width}}  {'Name':<{name_width}}  {'Provider':<{provider_width}}"
    )
    sep = "-" * len(header)

    print(header)
    print(sep)

    for m in models:
        mid = m.get("id", "")
        mname = m.get("name", mid)
        provider = m.get("provider", "") or ""

        print(f"{mid:<{id_width}}  {mname:<{name_width}}  {provider:<{provider_width}}")


def model_get(model_id):
    """Show details of a specific model."""
    if not model_id:
        print("Error: model_id is required.")
        print("Usage: evonic model get <model_id>")
        sys.exit(1)

    db = _get_db()
    model = db.get_model_by_id(model_id)

    if model is None:
        print(f"Error: Model not found: {model_id}")
        sys.exit(1)

    print(f"ID:          {model.get('id', '')}")
    print(f"Name:        {model.get('name', '')}")
    print(f"Type:        {model.get('type', '')}")
    print(f"Provider:    {model.get('provider', '')}")
    print(f"Model Name:  {model.get('model_name', '')}")
    print(f"Base URL:    {model.get('base_url', '') or '(default)'}")
    print(
        f"API Key:     {'***' + (model.get('api_key', '') or '')[-6:] if model.get('api_key') else '(none)'}"
    )
    print(f"Max Tokens:  {model.get('max_tokens', 32768)}")
    print(f"Timeout:     {model.get('timeout', 60)}")
    print(f"Temperature: {model.get('temperature', 'N/A')}")
    print(f"Thinking:    {'yes' if model.get('thinking', 0) else 'no'}")
    print(f"Default:     {'yes' if model.get('is_default', 0) else 'no'}")


def model_add(model_id, name, provider, api_key=None, base_url=None):
    """Add a new LLM model."""
    if not model_id:
        print("Error: model_id is required.")
        print(
            "Usage: evonic model add <id> --name <name> --provider <provider> [--api-key] [--base-url]"
        )
        sys.exit(1)

    if not name:
        print("Error: --name is required.")
        print("Usage: evonic model add <id> --name <name> --provider <provider>")
        sys.exit(1)

    if not provider:
        print("Error: --provider is required.")
        print("Usage: evonic model add <id> --name <name> --provider <provider>")
        sys.exit(1)

    db = _get_db()
    if db.get_model_by_id(model_id):
        print(f"Error: Model '{model_id}' already exists.")
        sys.exit(1)

    model_data = {
        "id": model_id,
        "name": name,
        "type": "chat",
        "provider": provider,
        "model_name": model_id,
        "api_key": api_key or "",
        "base_url": base_url or "",
        "is_default": 0,
    }

    try:
        created_id = db.create_model(model_data)
        print(f"Model added: {name} ({created_id})")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def model_rm(model_id):
    """Remove a model with interactive confirmation."""
    if not model_id:
        print("Error: model_id is required.")
        print("Usage: evonic model rm <model_id>")
        sys.exit(1)

    db = _get_db()
    model = db.get_model_by_id(model_id)

    if model is None:
        print(f"Error: Model not found: {model_id}")
        sys.exit(1)

    # Show model details and ask for confirmation
    mname = model.get("name", model_id)
    provider = model.get("provider", "")
    print(f"Model to remove:")
    print(f"  ID:        {model_id}")
    print(f"  Name:      {mname}")
    print(f"  Provider:  {provider}")

    try:
        response = input("Are you sure? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        print("Aborted.")
        sys.exit(1)

    if response not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

    try:
        db.delete_model(model_id)
        print(f"Model removed: {model_id}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


# ─── Setup Wizard ─────────────────────────────────────────────────────────────


def _install_dependencies():
    """Install Python dependencies from requirements.txt."""
    req_file = os.path.join(ROOT, "requirements.txt")
    if not os.path.exists(req_file):
        print("  Warning: requirements.txt not found, skipping dependency install.")
        return True
    print("  Installing dependencies...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", req_file],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("  Error installing dependencies:")
        print(result.stdout[-2000:] if result.stdout else "")
        print(result.stderr[-2000:] if result.stderr else "")
        return False
    print("  Dependencies installed.")
    return True


def setup_wizard():
    """Interactive first-time setup wizard for Evonic (CLI)."""
    import getpass

    # ── Pipe-safe input: when stdin is piped (e.g., curl | bash → evonic setup),
    #    rebind sys.stdin to /dev/tty so input() and getpass.getpass()
    #    read directly from the terminal instead of hitting EOF. ──
    if not sys.stdin.isatty():
        try:
            sys.stdin = open("/dev/tty", "r")
        except OSError:
            pass  # No /dev/tty available; existing EOFError handlers will abort gracefully

    db = _get_db()
    if db.has_super_agent():
        print("Setup is already complete. Super agent already exists.")
        sys.exit(0)

    # ── Banner ──
    print()
    print("  Welcome to Evonic Setup")
    print("  " + "=" * 22)
    print()

    # ── Step 0: Install dependencies ──
    if not _install_dependencies():
        sys.exit(1)
    print()

    from backend.setup import (
        PROVIDER_DEFAULTS,
        build_sandbox_image,
        check_docker_available,
        run_setup,
        test_connection,
    )

    # ── Step 1: Provider ──
    providers = list(PROVIDER_DEFAULTS.items())
    print("  Select your LLM provider:")
    print()
    for i, (pid, p) in enumerate(providers, 1):
        print(f"    [{i}] {p['label']:<12} {p['description']}")
    print()
    try:
        choice = input("  Choice [1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(providers):
            raise ValueError
    except ValueError:
        print("  Invalid choice.")
        sys.exit(1)

    provider_id, provider_cfg = providers[idx]
    print(f"\n  Selected: {provider_cfg['label']}")
    # ── Step 2: Base URL ──
    default_url = provider_cfg["base_url"]
    print()
    if provider_id == "custom":
        try:
            print("  eg: http://192.168.1.7:8080/v1")
            base_url = input("  Base URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if not base_url:
            print("  Base URL is required for custom provider.")
            sys.exit(1)
    else:
        try:
            entered = input(f"  Base URL [{default_url}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        base_url = entered or default_url

    # ── Step 3: API Key ──
    api_key = ""
    if provider_cfg["api_key_required"]:
        try:
            api_key = getpass.getpass("  API Key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if not api_key:
            print("  API key is required for this provider.")
            sys.exit(1)
    else:
        try:
            api_key = getpass.getpass(
                "  API Key (optional, press Enter to skip): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)

    # ── Step 4: Model name ──
    placeholder = provider_cfg["placeholder_model"]
    try:
        model_name = input(f"  Model name [{placeholder}]: ").strip() or placeholder
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    if not model_name:
        print("  Model name is required.")
        sys.exit(1)

    # ── Step 5: Test connection ──
    print()
    print("  Testing connection...", end=" ", flush=True)
    result = test_connection(base_url, api_key or None)
    if result["success"]:
        print(f"OK — {result['message']}")
    else:
        print(f"FAILED — {result['message']}")
        try:
            cont = input("  Continue anyway? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if cont not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(1)

    # ── Step 6: Super Agent name ──
    print()
    try:
        agent_name = input("  Super Agent name [Admin]: ").strip() or "Admin"
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)

    import re

    default_id = re.sub(r"[^a-z0-9_]", "_", agent_name.lower())
    default_id = re.sub(r"_+", "_", default_id).strip("_") or "admin"
    try:
        agent_id = input(f"  Agent ID [{default_id}]: ").strip().lower() or default_id
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    if not re.match(r"^[a-z0-9_]+$", agent_id):
        print(
            "  Agent ID must be lowercase alphanumeric and underscores only (snake_case)."
        )
        sys.exit(1)

    # ── Step 6: Docker Sandbox Detection ──
    sandbox_enabled = False
    print()
    docker_status = check_docker_available()
    if docker_status["available"]:
        print(f"  Docker detected — {docker_status['message']}")
        print()
        print("  Fitur sandbox execution memerlukan Docker.")
        try:
            build_choice = (
                input(
                    "  Apakah Anda ingin menyiapkan Docker image terlebih dahulu? [Y/n]: "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if build_choice in ("", "y", "yes"):
            print()
            print("  Building Docker sandbox image...", end=" ", flush=True)
            build_result = build_sandbox_image()
            if build_result["success"]:
                print("Done!")
                print(f"  {build_result['message']}")
                sandbox_enabled = True
            else:
                print("FAILED")
                print(f"  {build_result['message']}")
                print("  Sandbox execution will be disabled.")
        else:
            print("  Skipping Docker setup. Sandbox execution will be disabled.")
    else:
        print(f"  Docker not available — {docker_status['message']}")
        print("  Sandbox execution will be disabled.")

    # ── Step 7: Confirm ──
    print()
    print("  Setup Summary")
    print("  " + "─" * 30)
    print(f"  Provider       : {provider_cfg['label']}")
    print(f"  Base URL       : {base_url}")
    print(f"  Model          : {model_name}")
    print(f"  Agent          : {agent_name} ({agent_id})")
    print(f"  Sandbox        : {'Enabled' if sandbox_enabled else 'Disabled'}")
    print()
    try:
        confirm = input("  Proceed? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    if confirm in ("n", "no"):
        print("  Aborted.")
        sys.exit(0)

    # ── Execute setup ──
    print()
    print("  Creating platform...", end=" ", flush=True)
    outcome = run_setup(
        provider=provider_id,
        model_name=model_name,
        base_url=base_url,
        api_key=api_key,
        agent_name=agent_name,
        agent_id=agent_id,
        sandbox_enabled=sandbox_enabled,
    )
    if outcome.get("error"):
        print(f"FAILED\n  Error: {outcome['error']}")
        sys.exit(1)

    print("Done!")
    print()
    print(f"  Super agent '{agent_name}' created successfully.")

    # ── Step 8: Telegram Binding ──
    bot_token = ""
    print()
    print("  Telegram Integration")
    print("  " + "─" * 30)
    print("  Connect a Telegram bot so you can chat with your")
    print("  super agent directly through Telegram.")
    print()
    try:
        telegram_choice = input("  Connect Telegram bot? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Skipped.")
        telegram_choice = "n"
    if telegram_choice in ("y", "yes"):
        try:
            bot_token = getpass.getpass("  Telegram Bot Token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if bot_token:
            try:
                db.create_channel(
                    {
                        "agent_id": agent_id,
                        "type": "telegram",
                        "name": "Telegram Bot",
                        "config": {"bot_token": bot_token, "mode": "restricted"},
                        "enabled": True,
                    }
                )
                print("  Telegram bot connected successfully.")
                print(f"  You can now chat with '{agent_name}' via Telegram.")
            except Exception as e:
                print(f"  Failed to connect Telegram bot: {e}")
        else:
            print("  No token provided. Skipping Telegram setup.")
    else:
        print("  Skipped. You can add Telegram later from the web dashboard.")

    # ── Step 10: Password Setup ──
    from werkzeug.security import generate_password_hash

    print()
    print("  Set Web Dashboard Password")
    print("  " + "─" * 30)
    print("  This password is used to log in to the web dashboard.")
    print()
    env_path = os.path.join(ROOT, ".env")
    while True:
        try:
            pw1 = getpass.getpass("  Password: ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if not pw1:
            print(
                "  Warning: No password set. Web dashboard will be accessible without login."
            )
            break
        if len(pw1) < 6:
            print("  Error: Password must be at least 6 characters. Try again.")
            continue
        try:
            pw2 = getpass.getpass("  Confirm password: ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if pw1 != pw2:
            print("  Error: Passwords do not match. Try again.")
            continue
        new_hash = generate_password_hash(pw1, method="pbkdf2:sha256")
        _update_env_var(env_path, "ADMIN_PASSWORD_HASH", new_hash)
        print("  Password set successfully.")
        break

    # -- Generate supervisor/config.json for self-update support --
    print()
    print("  Setting up supervisor for self-update...", end=" ", flush=True)

    # Resolve the server port from config (which loads .env) so the
    # supervisor health check probes the correct port (not hardcoded 8080).
    try:
        import config
        server_port = int(getattr(config, 'PORT', 8080))
    except Exception:
        server_port = 8080

    sup_cfg = {
        "app_root": ROOT,
        "poll_interval": 300,
        "health_port": server_port,
        "health_temp_port": 18080,
        "health_timeout": 10,
        "monitor_duration": 60,
        "keep_releases": 3,
        "python_bin": "python3",
        "uv_bin": None,
        "telegram_bot_token": bot_token,
        "telegram_chat_id": "",
    }
    sup_cfg_dir = os.path.join(ROOT, "supervisor")
    os.makedirs(sup_cfg_dir, exist_ok=True)
    sup_cfg_path = os.path.join(sup_cfg_dir, "config.json")
    import json

    with open(sup_cfg_path, "w") as f:
        json.dump(sup_cfg, f, indent=4)
    print("Done!")

    print()
    print(f"  Start the server with: evonic start")
    print()


# ─── Password Setup ──────────────────────────────────────────────────────────


def pass_setup():
    """Set or change the admin password used for web dashboard authentication."""
    import getpass

    from werkzeug.security import check_password_hash, generate_password_hash

    env_path = os.path.join(ROOT, ".env")

    try:
        import config

        current_hash = config.ADMIN_PASSWORD_HASH
    except Exception:
        current_hash = os.getenv("ADMIN_PASSWORD_HASH", "")

    if not current_hash:
        print("No admin password is set. Create a new password.")
        pw1 = getpass.getpass("New password: ")
        if not pw1:
            print("Error: Password cannot be empty.")
            sys.exit(1)
        if len(pw1) < 6:
            print("Error: Password must be at least 6 characters.")
            sys.exit(1)
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("Error: Passwords do not match.")
            sys.exit(1)
        new_hash = generate_password_hash(pw1, method="pbkdf2:sha256")
        _update_env_var(env_path, "ADMIN_PASSWORD_HASH", new_hash)
        print("Password set successfully.")
    else:
        print("Admin password is already configured.")
        try:
            choice = input("Change password? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if choice not in ("y", "yes"):
            print("No changes made.")
            return

        old_pw = getpass.getpass("Current password: ")
        if not check_password_hash(current_hash, old_pw):
            print("Error: Incorrect password.")
            sys.exit(1)

        pw1 = getpass.getpass("New password: ")
        if not pw1:
            print("Error: Password cannot be empty.")
            sys.exit(1)
        if len(pw1) < 6:
            print("Error: Password must be at least 6 characters.")
            sys.exit(1)
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("Error: Passwords do not match.")
            sys.exit(1)

        new_hash = generate_password_hash(pw1, method="pbkdf2:sha256")
        _update_env_var(env_path, "ADMIN_PASSWORD_HASH", new_hash)
        print("Password changed successfully.")


def _reconfigure_supervisor_wizard():
    """Interactive wizard for reconfiguring the supervisor daemon.

    Prompts user for poll interval, health check port, release retention,
    and optional Telegram notification settings. Saves the result to
    supervisor/config.json.
    """
    import json

    sup = _load_supervisor_module()
    cfg_path = os.path.join(ROOT, "supervisor", "config.json")

    # Load existing config if available, otherwise start from defaults
    cfg = sup.load_config(cfg_path)

    # --- Banner ---
    print()
    print("  Evonic Supervisor Reconfigure")
    print("  " + "=" * 30)
    print()
    print("  Configure the supervisor daemon that manages the server")
    print("  process, self-updates, and health checks.")
    print()

    # --- Step 1: Poll interval ---
    print("  Poll interval \u2014 how often (in seconds) the supervisor checks")
    print("  for new releases on GitHub.")
    print()
    current_poll = cfg.get("poll_interval", 300)
    try:
        poll_input = input(f"  Poll interval [{current_poll}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    if poll_input:
        try:
            poll_interval = int(poll_input)
            if poll_interval < 60:
                print("  Minimum poll interval is 60 seconds.")
                poll_interval = 60
        except ValueError:
            print("  Invalid value. Using default.")
            poll_interval = current_poll
    else:
        poll_interval = current_poll

    # --- Step 2: Health check port ---
    print()
    print("  Health check port \u2014 the supervisor probes this port to")
    print("  determine whether the server is responsive after a swap.")
    print()
    # Resolve default from config.PORT if available, otherwise 8080
    try:
        import config
        _default_health_port = int(getattr(config, 'PORT', 8080))
    except Exception:
        _default_health_port = 8080
    current_health = cfg.get('health_port', _default_health_port)
    try:
        health_input = input(f"  Health check port [{current_health}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    if health_input:
        try:
            health_port = int(health_input)
            if health_port < 1 or health_port > 65535:
                print("  Port must be 1\u201365535. Using default.")
                health_port = current_health
        except ValueError:
            print("  Invalid value. Using default.")
            health_port = current_health
    else:
        health_port = current_health

    # --- Step 3: Keep releases ---
    print()
    print("  Release retention \u2014 how many past releases to keep")
    print("  (older ones are pruned after a successful update).")
    print()
    current_keep = cfg.get("keep_releases", 3)
    try:
        keep_input = input(f"  Keep releases [{current_keep}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    if keep_input:
        try:
            keep_releases = int(keep_input)
            if keep_releases < 1:
                print("  Must keep at least 1 release. Using default.")
                keep_releases = current_keep
        except ValueError:
            print("  Invalid value. Using default.")
            keep_releases = current_keep
    else:
        keep_releases = current_keep

    # --- Step 4: Telegram notifications (optional) ---
    print()
    print("  Telegram notifications \u2014 optionally notify a chat when")
    print("  the supervisor performs an update or encounters an error.")
    print()
    current_token = cfg.get("telegram_bot_token", "")
    current_chat = cfg.get("telegram_chat_id", "")
    masked_token = (
        ("***" + current_token[-4:])
        if len(current_token) > 4
        else (current_token or "(not set)")
    )
    print(f"  Current bot token : {masked_token}")
    print(f"  Current chat ID   : {current_chat or '(not set)'}")
    print()
    try:
        use_telegram = (
            input("  Configure Telegram notifications? [y/N]: ").strip().lower()
        )
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)

    telegram_bot_token = current_token
    telegram_chat_id = current_chat

    if use_telegram in ("y", "yes"):
        try:
            token_input = input(f"  Bot token [{masked_token}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if token_input:
            telegram_bot_token = token_input
        try:
            chat_input = input(f"  Chat ID [{current_chat or ''}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if chat_input:
            telegram_chat_id = chat_input

    # --- Summary ---
    print()
    print("  Supervisor Config Summary")
    print("  " + "\u2500" * 30)
    print(f"  Poll interval    : {poll_interval} seconds")
    print(f"  Health check port: {health_port}")
    print(f"  Keep releases    : {keep_releases}")
    masked_final = (
        ("***" + telegram_bot_token[-4:])
        if len(telegram_bot_token) > 4
        else telegram_bot_token
    )
    print(f"  Telegram token   : {masked_final or '(not set)'}")
    print(f"  Telegram chat ID : {telegram_chat_id or '(not set)'}")
    print()
    try:
        confirm = input("  Proceed? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    if confirm in ("n", "no"):
        print("  Aborted.")
        sys.exit(0)

    # --- Save ---
    cfg["poll_interval"] = poll_interval
    cfg["health_port"] = health_port
    cfg["keep_releases"] = keep_releases
    cfg["telegram_bot_token"] = telegram_bot_token
    cfg["telegram_chat_id"] = telegram_chat_id

    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)

    print()
    print(f"  Supervisor config saved to {cfg_path}")
    print()


def reconfigure_wizard(supervisor=False):
    """Interactive reconfigure wizard for Evonic (CLI).

    Args:
        supervisor: If True, run the supervisor-specific reconfigure wizard
                    instead of the full platform reconfigure wizard.
    """
    if supervisor:
        return _reconfigure_supervisor_wizard()

    import getpass

    db = _get_db()
    if not db.has_super_agent():
        print("Setup has not been completed yet. No super agent exists.")
        print("Please run 'evonic setup' first to configure your platform.")
        sys.exit(1)

    from backend.setup import (
        LANGUAGE_PRESETS,
        PROVIDER_DEFAULTS,
        build_sandbox_image,
        check_docker_available,
        run_reconfigure,
        test_connection,
    )

    # ── Load current configuration from DB ──
    super_agent = db.get_super_agent()
    agent_id = super_agent["id"]

    current_language = db.get_setting("agent_language", "english")
    current_sandbox = db.get_setting("sandbox_default_enabled", "0") == "1"

    # Determine current provider/model by checking which setup_* model exists
    current_provider = "ollama"
    current_model_name = ""
    current_base_url = ""
    for pid in PROVIDER_DEFAULTS:
        model = db.get_model_by_id(f"setup_{pid}")
        if model:
            current_provider = pid
            current_model_name = model.get("model_name", "")
            current_base_url = model.get("base_url", "")
            break

    # ── Banner ──
    print()
    print("  Evonic Reconfigure")
    print("  " + "=" * 20)
    print()

    # ── Step 1: Provider ──
    providers = list(PROVIDER_DEFAULTS.items())
    print("  Select your LLM provider:")
    print()
    for i, (pid, p) in enumerate(providers, 1):
        mark = " (current)" if pid == current_provider else ""
        print(f"    [{i}] {p['label']:<12} {p['description']}{mark}")
    print()
    current_idx = 1
    for i, (pid, _) in enumerate(providers, 1):
        if pid == current_provider:
            current_idx = i
            break
    try:
        choice = input(f"  Choice [{current_idx}]: ").strip() or str(current_idx)
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(providers):
            raise ValueError
    except ValueError:
        print("  Invalid choice.")
        sys.exit(1)

    provider_id, provider_cfg = providers[idx]
    print(f"\n  Selected: {provider_cfg['label']}")
    # ── Step 2: Base URL ──
    # If provider changed, use the new provider's default; otherwise use current
    if provider_id == current_provider and current_base_url:
        default_url = current_base_url
    else:
        default_url = provider_cfg["base_url"]
    print()
    if provider_id == "custom":
        try:
            print("  eg: http://192.168.1.7:8080/v1")
            prompt = f"  Base URL [{default_url}]: " if default_url else "  Base URL: "
            base_url = input(prompt).strip() or default_url
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if not base_url:
            print("  Base URL is required for custom provider.")
            sys.exit(1)
    else:
        try:
            entered = input(f"  Base URL [{default_url}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        base_url = entered or default_url

    # ── Step 3: API Key ──
    api_key = ""
    if provider_cfg["api_key_required"]:
        try:
            api_key = getpass.getpass("  API Key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if not api_key:
            print("  API key is required for this provider.")
            sys.exit(1)
    else:
        try:
            api_key = getpass.getpass(
                "  API Key (optional, press Enter to skip): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)

    # ── Step 4: Model name ──
    if provider_id == current_provider and current_model_name:
        placeholder = current_model_name
    else:
        placeholder = provider_cfg["placeholder_model"]
    try:
        model_name = input(f"  Model name [{placeholder}]: ").strip() or placeholder
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    if not model_name:
        print("  Model name is required.")
        sys.exit(1)

    # ── Step 5: Test connection ──
    print()
    print("  Testing connection...", end=" ", flush=True)
    result = test_connection(base_url, api_key or None)
    if result["success"]:
        print(f"OK — {result['message']}")
    else:
        print(f"FAILED — {result['message']}")
        try:
            cont = input("  Continue anyway? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if cont not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(1)

    # ── Step 6: Language ──
    languages = list(LANGUAGE_PRESETS.items())
    print()
    print("  Response language:")
    print()
    current_lang_idx = 1
    for i, (lid, l) in enumerate(languages, 1):
        mark = " (current)" if lid == current_language else ""
        print(f"    [{i}] {l['label']:<14} {l['description']}{mark}")
        if lid == current_language:
            current_lang_idx = i
    print()
    try:
        lang_choice = input(f"  Choice [{current_lang_idx}]: ").strip() or str(
            current_lang_idx
        )
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    try:
        lidx = int(lang_choice) - 1
        if lidx < 0 or lidx >= len(languages):
            raise ValueError
    except ValueError:
        print("  Invalid choice.")
        sys.exit(1)

    language_id, _ = languages[lidx]

    # ── Step 6: Docker Sandbox ──
    sandbox_enabled = current_sandbox
    print()
    docker_status = check_docker_available()
    if docker_status["available"]:
        print(f"  Docker detected — {docker_status['message']}")
        print()
        sandbox_label = "enabled" if current_sandbox else "disabled"
        print(f"  Sandbox execution is currently {sandbox_label}.")
        try:
            build_choice = input("  Toggle sandbox execution? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)
        if build_choice in ("y", "yes"):
            sandbox_enabled = not current_sandbox
            if sandbox_enabled:
                print()
                print("  Building Docker sandbox image...", end=" ", flush=True)
                build_result = build_sandbox_image()
                if build_result["success"]:
                    print("Done!")
                    print(f"  {build_result['message']}")
                else:
                    print("FAILED")
                    print(f"  {build_result['message']}")
                    print("  Sandbox execution will remain disabled.")
                    sandbox_enabled = False
            else:
                print("  Sandbox execution disabled.")
    else:
        print(f"  Docker not available — {docker_status['message']}")
        print("  Sandbox execution will be disabled.")
        sandbox_enabled = False

    # ── Step 7: Confirm ──
    print()
    print("  Reconfigure Summary")
    print("  " + "─" * 30)
    print(f"  Provider       : {provider_cfg['label']}")
    print(f"  Base URL       : {base_url}")
    print(f"  Model          : {model_name}")
    print(f"  Language       : {LANGUAGE_PRESETS[language_id]['label']}")
    print(f"  Sandbox        : {'Enabled' if sandbox_enabled else 'Disabled'}")
    print()
    try:
        confirm = input("  Proceed? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Aborted.")
        sys.exit(1)
    if confirm in ("n", "no"):
        print("  Aborted.")
        sys.exit(0)

    # ── Execute reconfigure ──
    print()
    print("  Reconfiguring platform...", end=" ", flush=True)
    outcome = run_reconfigure(
        provider=provider_id,
        model_name=model_name,
        base_url=base_url,
        api_key=api_key,
        language=language_id,
        sandbox_enabled=sandbox_enabled,
    )
    if outcome.get("error"):
        print(f"FAILED\n  Error: {outcome['error']}")
        sys.exit(1)

    print("Done!")
    print()
    print("  Platform reconfigured successfully.")
    print()

    # ── Step 8: Optional password change ──
    try:
        import config

        current_hash = config.ADMIN_PASSWORD_HASH
    except Exception:
        current_hash = ""
    if current_hash:
        try:
            pw_choice = input("  Change admin password? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Done.")
            return
        if pw_choice not in ("y", "yes"):
            print("  Password unchanged.")
            print()
            return
    else:
        print("  No admin password set. Create one for the web dashboard.")
        try:
            pw_choice = input("  Set password? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Done.")
            return
        if pw_choice in ("n", "no"):
            print("  Password skipped.")
            print()
            return

    import getpass as gp

    from werkzeug.security import generate_password_hash

    env_path = os.path.join(ROOT, ".env")
    while True:
        try:
            pw1 = gp.getpass("  Password: ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return
        if not pw1:
            print(
                "  Warning: Password not set. Web dashboard can be accessed without login."
            )
            break
        if len(pw1) < 6:
            print("  Error: Password must be at least 6 characters. Try again.")
            continue
        try:
            pw2 = gp.getpass("  Confirm password: ")
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            return
        if pw1 != pw2:
            print("  Error: Passwords do not match. Try again.")
            continue
        new_hash = generate_password_hash(pw1, method="pbkdf2:sha256")

        _update_env_var(env_path, "ADMIN_PASSWORD_HASH", new_hash)
        print("  Password set successfully.")
        break
    print()


def _update_env_var(env_path, key, value):
    """Update or add an environment variable in a .env file.

    Delegates to backend.setup._update_env_var which uses atomic
    write (write-to-temp-then-rename) to prevent partial .env writes.
    """
    from backend.setup import _update_env_var as _impl
    _impl(env_path, key, value)


# ─── Update / Self-Update ──────────────────────────────────────────────────────


def _load_supervisor_module():
    """Import supervisor.py from the supervisor/ directory."""
    sup_path = os.path.join(ROOT, "supervisor")
    if sup_path not in sys.path:
        sys.path.insert(0, sup_path)
    import importlib

    return importlib.import_module("supervisor")


def _get_supervisor_pid():
    """Read the running supervisor's PID, or None."""
    sup_pid_file = os.path.join(ROOT, "supervisor", "run", "supervisor.pid")
    if not os.path.exists(sup_pid_file):
        return None
    try:
        with open(sup_pid_file) as f:
            return int(f.read().strip())
    except (ValueError, IOError):
        return None


def update_server(
    check_only=False, force=False, tag=None, rollback_flag=False, nightly=False
):
    """
    Trigger or run a self-update.

    Modes:
    - check_only: fetch tags, report what is available, no update applied
    - rollback_flag: swap back to the previous release
    - default: signal running supervisor (SIGUSR1) or run update inline
    - tag: target a specific tag instead of latest
    - nightly: fetch origin/main and run full update lifecycle (no tags)
    """
    sup = _load_supervisor_module()
    cfg_path = os.path.join(ROOT, "supervisor", "config.json")
    cfg = sup.load_config(cfg_path)
    app_root = cfg["app_root"]

    # Update root project first to keep CLI/supervisor code up-to-date
    print("Updating root project from origin/main...")
    rc, out, err = sup._git(app_root, ['pull', '--ff-only', 'origin', 'main'])
    if rc != 0:
        print(f"Git pull failed: {err or out}")
        sys.exit(1)
    print("Root project updated.")

    if rollback_flag:
        print("Rolling back to previous release...")
        ok = sup.rollback(app_root, cfg, None)
        sys.exit(0 if ok else 1)

    if check_only:
        if nightly:
            print("Fetching origin/main...")
            ok, err = sup.git_fetch_branch(app_root, "main")
            if not ok:
                print(f"Fetch failed: {err}")
                sys.exit(1)
            rc, sha, _ = sup._git(app_root, ["rev-parse", "--short", "origin/main"])
            current = sup.get_current_release(app_root)
            print(f"Current      : {current or '(none — flat repo mode)'}")
            print(f"origin/main  : {sha if rc == 0 else 'unknown'}")
            return
        print("Fetching tags...")
        sup.git_fetch_tags(app_root)
        current = sup.get_current_release(app_root)
        latest = sup.get_latest_tag(app_root)
        print(f"Current : {current or '(none — flat repo mode)'}")
        print(f"Latest  : {latest or '(no tags found)'}")
        if latest and latest != current:
            print(f"Update available: {current} -> {latest}")
        elif latest:
            print("Already up to date.")
        return

    if nightly:
        # Nightly: always run inline (no supervisor signal)
        print("Fetching origin/main (nightly)...")
        ok, err = sup.git_fetch_branch(app_root, "main")
        if not ok:
            print(f"Fetch failed: {err}")
            sys.exit(1)
        rc, sha, _ = sup._git(app_root, ["rev-parse", "--short", "origin/main"])
        print(f"Updating to nightly (origin/main @ {sha if rc == 0 else 'unknown'})...")
        ok = sup.run_update("main", cfg, None, skip_verify=True, nightly=True)
        sys.exit(0 if ok else 1)

    # Signal running supervisor for immediate check
    if not sup.is_windows():
        spid = _get_supervisor_pid()
        if spid and _is_running(spid):
            try:
                os.kill(spid, signal.SIGUSR1)
                print(f"Sent update trigger to supervisor (PID {spid})")
                return
            except OSError:
                pass

    # Supervisor not running — run update inline
    print("Supervisor not running. Running update inline...")
    sup.git_fetch_tags(app_root)
    target = tag or sup.get_latest_tag(app_root)
    if not target:
        print("No tags found — nothing to update.")
        sys.exit(1)

    current = sup.get_current_release(app_root)
    if target == current and not force:
        print(f"Already at {target}.")
        return

    print(f"Updating to {target}...")
    ok = sup.run_update(target, cfg, None, skip_verify=force)
    sys.exit(0 if ok else 1)


# ═══════════════════════════════════════════════════════════════════
# Doctor — system diagnostics
# ═══════════════════════════════════════════════════════════════════

# ANSI helpers
_G = "\033[32m"  # green
_R = "\033[31m"  # red
_Y = "\033[33m"  # yellow
_B = "\033[34m"  # blue
_C = "\033[36m"  # cyan
_W = "\033[37m"  # white (bright)
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"

_PASS = f"{_G}✓{_RESET}"
_FAIL = f"{_R}✗{_RESET}"
_WARN = f"{_Y}⚠{_RESET}"
_INFO = f"{_B}ℹ{_RESET}"


def _section(title):
    print(f"\n{_BOLD}{_C}══ {title} ══{_RESET}")


def _ok(msg=""):
    line = f"  {_PASS}  {msg}" if msg else f"  {_PASS}"
    print(line)
    return "pass"


def _fail(msg=""):
    line = f"  {_FAIL}  {msg}" if msg else f"  {_FAIL}"
    print(line)
    return "fail"


def _warn(msg=""):
    line = f"  {_WARN}  {msg}" if msg else f"  {_WARN}"
    print(line)
    return "warn"


def _info(msg):
    print(f"  {_INFO}  {msg}")


def doctor_command(quick=False):
    """Run comprehensive system health diagnostics."""
    import importlib
    import json
    import platform

    print(f"\n{_BOLD}{_C}🩺  Evonic Doctor{_RESET}")
    print(f"{_DIM}System diagnostics & health check{_RESET}")

    results = []

    # ── 1. Environment Check ──────────────────────────────────
    _section("1. Environment Check")

    # Python version
    py_ver = platform.python_version()
    major, minor, *_ = py_ver.split(".")
    if int(major) >= 3 and int(minor) >= 9:
        results.append(_ok(f"Python {py_ver}"))
    else:
        results.append(_warn(f"Python {py_ver} — 3.9+ recommended"))

    # OS info
    os_info = f"{platform.system()} {platform.release()} ({platform.machine()})"
    _info(f"OS: {os_info}")

    # Key environment variables
    important_vars = [
        "PORT",
        "HOST",
        "SECRET_KEY",
        "DEBUG",
        "ADMIN_PASSWORD_HASH",
        "SANDBOX_NETWORK",
        "LOG_FULL_THINKING",
        "LOG_FULL_RESPONSE",
    ]
    for var in important_vars:
        val = os.getenv(var)
        if val is None:
            results.append(_warn(f"Env {var} not set"))
        else:
            masked = (
                "***"
                if var in ("SECRET_KEY", "ADMIN_PASSWORD_HASH") and len(val) > 4
                else val
            )
            _info(f"  {var}={masked}")

    # Dependencies check
    try:
        import flask

        flask_ver = getattr(flask, "__version__", "?")
        _info(f"  flask=={flask_ver}")
    except ImportError:
        results.append(_fail("flask not installed"))

    try:
        import requests

        requests_ver = getattr(requests, "__version__", "?")
        _info(f"  requests=={requests_ver}")
    except ImportError:
        results.append(_fail("requests not installed"))

    try:
        import anthropic

        anthro_ver = getattr(anthropic, "__version__", "?")
        _info(f"  anthropic=={anthro_ver}")
    except ImportError:
        _info("  anthropic (optional, not installed)")

    # DB driver check
    try:
        import sqlite3

        results.append(_ok("sqlite3 available"))
    except ImportError:
        results.append(_fail("sqlite3 not available"))

    # ── 2. Configuration Check ────────────────────────────────
    _section("2. Configuration Check")

    config_files = {
        ".env": "Environment variables",
        "config.py": "App configuration",
    }

    for fname, desc in config_files.items():
        fpath = os.path.join(ROOT, fname)
        if os.path.isfile(fpath):
            _info(f"  {fname} — {desc} (found)")
            if fname == ".env":
                try:
                    with open(fpath) as f:
                        lines = [
                            l.strip() for l in f if l.strip() and not l.startswith("#")
                        ]
                    if lines:
                        results.append(_ok(f"{fname} ({len(lines)} vars)"))
                    else:
                        results.append(_warn(f"{fname} is empty"))
                except Exception as e:
                    results.append(_fail(f"Cannot read {fname}: {e}"))
        else:
            results.append(_warn(f"{fname} not found — {desc}"))

    # Config.py integrity check
    try:
        import config

        required_attrs = ["BASE_DIR", "DB_PATH", "PORT", "HOST", "SECRET_KEY"]
        missing = [a for a in required_attrs if not hasattr(config, a)]
        if missing:
            results.append(_warn(f"config.py missing: {', '.join(missing)}"))
        else:
            results.append(_ok("config.py valid"))
    except Exception as e:
        results.append(_fail(f"config.py error: {e}"))

    # .env valid UTF-8
    env_path = os.path.join(ROOT, ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path, encoding="utf-8") as f:
                f.read()
            results.append(_ok(".env readable (UTF-8)"))
        except UnicodeDecodeError:
            results.append(_fail(".env encoding error — not valid UTF-8"))
        except Exception as e:
            results.append(_fail(f".env read error: {e}"))

    # ── 3. Connection Check ───────────────────────────────────
    _section("3. Connection Check")

    # Database
    try:
        from models.db import db

        db_path = getattr(db, "db_path", config.DB_PATH)
        if os.path.isfile(db_path):
            try:
                with db._connect() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                results.append(_ok(f"Database ({db_path})"))
            except Exception as e:
                results.append(_fail(f"Database query failed: {e}"))
        else:
            results.append(_warn(f"Database file not found: {db_path}"))
    except Exception as e:
        results.append(_fail(f"Database init failed: {e}"))

    # Redis (check if configured)
    redis_url = os.getenv("REDIS_URL", "")
    if redis_url:
        try:
            import redis

            r = redis.from_url(redis_url)
            r.ping()
            results.append(_ok(f"Redis ({redis_url})"))
        except ImportError:
            results.append(_warn("Redis configured but redis-py not installed"))
        except Exception as e:
            results.append(_fail(f"Redis error: {e}"))
    else:
        _info("  Redis not configured (ok if not needed)")

    # External internet connectivity
    try:
        r = requests.get("https://httpbin.org/status/200", timeout=5)
        if r.status_code == 200:
            results.append(_ok("Internet connectivity"))
        else:
            results.append(_warn(f"Internet check HTTP {r.status_code}"))
    except requests.exceptions.Timeout:
        results.append(_warn("Internet check timed out"))
    except Exception as e:
        results.append(_warn(f"Internet check failed: {e}"))

    # ── 4. Service Check ──────────────────────────────────────
    _section("4. Service Check")

    pid = _get_pid()
    if pid and _is_running(pid):
        results.append(_ok(f"Server running (PID {pid})"))

        import config

        port = getattr(config, "PORT", 8080)
        _info(f"  Port: {port}")

        # Try health endpoint
        try:
            hr = requests.get(f"http://localhost:{port}/api/health", timeout=5)
            if hr.status_code == 200:
                results.append(_ok("Health endpoint OK"))
            else:
                results.append(_warn(f"Health endpoint HTTP {hr.status_code}"))
        except Exception:
            # Try Flisk health route
            try:
                hr = requests.get(f"http://localhost:{port}/health", timeout=5)
                if hr.status_code == 200:
                    results.append(_ok("Health endpoint OK"))
                else:
                    results.append(_warn(f"Health endpoint HTTP {hr.status_code}"))
            except Exception:
                results.append(
                    _warn("Health endpoint unreachable (server may be starting)")
                )

        # Port binding check
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        try:
            s.connect(("localhost", port))
            s.close()
            results.append(_ok(f"Port {port} is bound"))
        except Exception:
            results.append(_warn(f"Port {port} check failed"))
    else:
        results.append(_info("Server not running — skipping live checks"))
        _info("  Port binding: not checked")

    # ── 5. File/Folder Check ──────────────────────────────────
    _section("5. File/Folder Check")

    important_dirs = {
        "logs": "Application logs",
        "data": "Persistent data",
        "plugins": "Plugin directory",
        "skills": "Skills directory",
        "agents": "Agent data",
        "skillsets": "Skillset templates",
        "templates": "Web templates",
    }

    for dname, desc in important_dirs.items():
        dpath = os.path.join(ROOT, dname)
        if os.path.isdir(dpath):
            readable = os.access(dpath, os.R_OK)
            writable = os.access(dpath, os.W_OK)
            if readable and writable:
                _info(f"  {dname}/ — {desc} (rw)")
            elif readable:
                results.append(_warn(f"{dname}/ — {desc} (read-only)"))
            else:
                results.append(_fail(f"{dname}/ — {desc} (no read access)"))
        else:
            results.append(_warn(f"{dname}/ missing — {desc}"))

    # PID directory (run/)
    pid_dir = os.path.join(ROOT, "run")
    if os.path.isdir(pid_dir):
        _info(f"  run/ — PID files (exists)")
    else:
        _info(f"  run/ — PID files (not created yet)")

    # ── 6. Agent & Skill Health Check ─────────────────────────
    _section("6. Agent & Skill Health Check")

    try:
        from models.db import db

        agents = db.get_agents()
        if not agents:
            results.append(_warn("No agents configured"))
        else:
            enabled = [a for a in agents if a.get("enabled")]
            disabled = [a for a in agents if not a.get("enabled")]
            super_agents = [a for a in agents if a.get("is_super")]

            results.append(
                _ok(
                    f"{len(agents)} agent(s) — {len(enabled)} enabled, {len(disabled)} disabled"
                )
            )

            for a in agents:
                aid = a.get("id", "?")
                aname = a.get("name", aid)
                status = "enabled" if a.get("enabled") else "disabled"
                sicon = _PASS if a.get("enabled") else _WARN
                tools = db.get_agent_tools(aid)
                skills = (
                    db.get_agent_skills(aid) if hasattr(db, "get_agent_skills") else []
                )
                model_id = a.get("default_model_id") or a.get("model") or "none"
                has_model = "✓" if model_id and model_id != "none" else "✗"
                _info(
                    f"    {sicon} {aname} ({aid}) — model:{has_model} tools:{len(tools)} skills:{len(skills)}"
                )

                if not a.get("enabled"):
                    results.append("skip")
                elif not model_id or model_id == "none":
                    results.append(_warn(f"Agent '{aid}' has no model assigned"))
    except Exception as e:
        results.append(_fail(f"Agent check failed: {e}"))

    # Skills check
    try:
        from backend.skills_manager import SkillsManager

        sm = SkillsManager()
        skills = sm.list_skills()
        if not skills:
            results.append(_info("No skills installed"))
        else:
            enabled_skills = [s for s in skills if s.get("enabled")]
            disabled_skills = [s for s in skills if not s.get("enabled")]
            results.append(
                _ok(
                    f"{len(skills)} skill(s) — {len(enabled_skills)} enabled, {len(disabled_skills)} disabled"
                )
            )

            for s in skills:
                sid = s.get("id", "?")
                sname = s.get("name", sid)
                status = "enabled" if s.get("enabled") else "disabled"
                sicon = _PASS if s.get("enabled") else _WARN
                tools = s.get("tool_count", 0)
                _info(f"    {sicon} {sname} ({sid}) — tools:{tools}")

            # Check for corrupted manifests
            skills_dir = os.path.join(ROOT, "skills")
            if os.path.isdir(skills_dir):
                for entry in os.listdir(skills_dir):
                    epath = os.path.join(skills_dir, entry)
                    manifest = os.path.join(epath, "skill.json")
                    if os.path.isdir(epath) and os.path.isfile(manifest):
                        try:
                            with open(manifest) as f:
                                json.load(f)
                        except json.JSONDecodeError:
                            results.append(
                                _fail(f"Corrupted skill manifest: {entry}/skill.json")
                            )
    except Exception as e:
        results.append(_fail(f"Skill check failed: {e}"))

    # ── 7. LLM Provider Check ────────────────────────────────
    _section("7. LLM Provider Check")

    if quick:
        _info("  Skipped (--quick mode)")
        results.append("skip")
    else:
        try:
            from models.db import db

            models = db.get_llm_models()
            if not models:
                results.append(_warn("No LLM models configured"))
            else:
                tested = 0
                for m in models:
                    mid = m.get("id", "?")
                    mname = m.get("name", mid)
                    base_url = m.get("base_url")
                    provider = m.get("provider", "?")

                    if not base_url:
                        _info(f"  {_WARN} {mname} ({provider}) — no base_url, skipping")
                        continue

                    _info(f"  Testing: {mname} ({provider}) → {base_url}")
                    try:
                        models_url = f"{base_url.rstrip('/')}/models"
                        headers = {"Content-Type": "application/json"}
                        if m.get("api_key"):
                            headers["Authorization"] = f"Bearer {m['api_key']}"

                        resp = requests.get(models_url, headers=headers, timeout=10)
                        if resp.status_code == 200:
                            data = resp.json()
                            available = data.get("data") or data.get("models") or []
                            results.append(_ok(f"  {mname} — {len(available)} models"))
                        elif resp.status_code in (401, 403):
                            results.append(
                                _warn(
                                    f"  {mname} — auth error (HTTP {resp.status_code})"
                                )
                            )
                        else:
                            results.append(
                                _warn(
                                    f"  {mname} — HTTP {resp.status_code}: {resp.text[:100]}"
                                )
                            )
                        tested += 1
                    except requests.exceptions.Timeout:
                        results.append(_fail(f"  {mname} — timed out"))
                    except requests.exceptions.ConnectionError as e:
                        results.append(
                            _fail(f"  {mname} — connection error: {str(e)[:80]}")
                        )
                    except Exception as e:
                        results.append(_fail(f"  {mname} — {str(e)[:80]}"))

                if tested == 0:
                    results.append(_info("  No models with base_url to test"))
        except Exception as e:
            results.append(_fail(f"LLM check failed: {e}"))

    # ── 8. Supervisor Config Check ──────────────────────────────────────────────
    _section("8. Supervisor Config Check")

    sup_cfg_path = os.path.join(ROOT, "supervisor", "config.json")
    if os.path.isfile(sup_cfg_path):
        _info("  supervisor/config.json found")
        try:
            with open(sup_cfg_path) as f:
                sup_cfg = json.load(f)

            # Validate app_root
            app_root = sup_cfg.get("app_root", "")
            if app_root and os.path.isdir(app_root):
                results.append(_ok(f"  app_root: {app_root}"))
            elif app_root:
                results.append(
                    _fail(
                        f"  app_root '{app_root}' does not exist or is not a directory"
                    )
                )
            else:
                results.append(_fail("  app_root is not set in supervisor/config.json"))

            # Validate numeric fields
            for key, label, min_val in [
                ("poll_interval", "poll_interval", 1),
                ("health_port", "health_port", 1),
                ("health_temp_port", "health_temp_port", 1),
                ("health_timeout", "health_timeout", 1),
                ("monitor_duration", "monitor_duration", 1),
                ("keep_releases", "keep_releases", 1),
            ]:
                val = sup_cfg.get(key)
                if isinstance(val, int) and val >= min_val:
                    _info(f"  {label}: {val}")
                else:
                    results.append(
                        _warn(f"  {label} is invalid or missing (got {val!r})")
                    )

            # Validate telegram_bot_token
            token = sup_cfg.get("telegram_bot_token", "")
            if token:
                results.append(_ok("  telegram_bot_token is configured"))
            else:
                results.append(
                    _warn(
                        "  telegram_bot_token is empty — configure it for supervisor notifications. "
                        "Set via super agent channel or edit supervisor/config.json manually."
                    )
                )

            # Validate telegram_chat_id
            chat_id = sup_cfg.get("telegram_chat_id", "")
            if chat_id:
                results.append(_ok("  telegram_chat_id is configured"))
            else:
                results.append(
                    _warn(
                        "  telegram_chat_id is empty — configure it for supervisor notifications. "
                        "Set via super agent channel or edit supervisor/config.json manually."
                    )
                )

        except json.JSONDecodeError as e:
            results.append(_fail(f"  supervisor/config.json parse error: {e}"))
        except Exception as e:
            results.append(_fail(f"  supervisor/config.json validation error: {e}"))
    else:
        results.append(
            _warn(
                "  supervisor/config.json not found — self-update supervisor is not configured"
            )
        )

    # ── Summary ───────────────────────────────────────────────
    _section("Summary")

    # Filter out "skip" entries
    real = [r for r in results if r != "skip"]
    passed = sum(1 for r in real if r == "pass")
    failed = sum(1 for r in real if r == "fail")
    warnings = sum(1 for r in real if r == "warn")
    total = passed + failed + warnings

    print(f"\n  {_BOLD}Total checks:{_RESET} {total}")
    print(f"  {_G}✓ Passed:{_RESET}  {passed}")
    if warnings:
        print(f"  {_Y}⚠ Warnings:{_RESET} {warnings}")
    if failed:
        print(f"  {_R}✗ Failed:{_RESET}  {failed}")

    if failed > 0:
        print(f"\n{_R}{_BOLD}  System has issues that need attention.{_RESET}")
    elif warnings > 0:
        print(f"\n{_Y}{_BOLD}  System is operational with minor warnings.{_RESET}")
    else:
        print(f"\n{_G}{_BOLD}  All checks passed. System is healthy!{_RESET}")

    print()
    return 0 if failed == 0 else 1


# ─── Sandbox Management ───────────────────────────────────────────────────────


def clear_sandbox():
    """Destroy all running evonic sandbox containers (force sweep)."""
    result = subprocess.run(
        ['docker', 'ps', '--filter', 'label=evonic.managed=1', '--format', '{{.Names}}'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f'Error querying Docker: {result.stderr.strip()}')
        sys.exit(1)

    names = [n.strip() for n in result.stdout.splitlines() if n.strip()]
    if not names:
        print('No evonic sandbox containers running.')
        return

    print(f'Found {len(names)} sandbox container(s):')
    for name in names:
        print(f'  {name}')
    print()

    destroyed = 0
    failed = 0
    for name in names:
        rm = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True)
        if rm.returncode == 0:
            print(f'  ✓ Destroyed {name}')
            destroyed += 1
        else:
            print(f'  ✗ Failed to destroy {name}: {rm.stderr.strip()}')
            failed += 1

    print()
    print(f'Done: {destroyed} destroyed, {failed} failed.')
    if failed:
        sys.exit(1)


# ─── Channel Management ───────────────────────────────────────────────────────


def channel_approve(pair_code):
    """Approve a pending channel pairing request by pair code."""
    if not pair_code:
        print("Error: pair_code is required.")
        print("Usage: evonic channel approve <pair_code>")
        sys.exit(1)

    # Accept both XXX-XXX and XXXXXX formats
    pair_code = pair_code.replace("-", "").strip().upper()

    db = _get_db()
    pending = db.get_pending_approval_by_code(pair_code)

    if pending is None:
        print("❌ Pairing code invalid or expired.")
        sys.exit(1)

    success = db.approve_pending(pending["id"])
    if success:
        print(
            f"✅ User {pending['external_user_id']} has been added to the allowlist"
        )
    else:
        print("❌ Failed to approve pairing request.")
        sys.exit(1)


# ============================================================
# Backup & Restore System
# ============================================================

import hashlib
import json as _json
import sqlite3
import tarfile
import glob
import shutil
import getpass
from datetime import datetime as _datetime

try:
    from backend.version import get_version as _get_evonic_version
except ImportError:
    def _get_evonic_version():
        return _datetime.now().strftime("%Y%m%d")

# Encryption support (pure Python AES-256-GCM)
try:
    from cli.backup_crypto import encrypt_file_aes256gcm, decrypt_file_aes256gcm
    _ENCRYPTION_AVAILABLE = True
except ImportError:
    _ENCRYPTION_AVAILABLE = False

# ---------------------------------------------------------------------------
# Backup source definitions
# ---------------------------------------------------------------------------
# Each entry: (relative_path_from_root, description, is_db, is_glob)
# is_db: use sqlite3.backup() for atomic snapshot
# is_glob: expand glob pattern

def _build_backup_sources():
    """Return the list of backup sources as (rel_path, label, is_db, is_glob)."""
    sources = [
        # 1. Agent runtime data
        ("agents/", "Agent runtime data", False, True),
        # 2. Shared agent KB files
        ("agents/shared/kb/", "Shared agent KB", False, True),
        # 3. Agent artifacts (shared/agents/<id>/artifacts/)
        ("shared/agents/", "Agent artifacts", False, True),
        # 4. Main platform DB (with WAL files)
        ("shared/db/evonic.db", "Main platform DB (evonic)", True, False),
        ("shared/db/evonic.db-wal", "Main DB WAL", False, False),
        ("shared/db/evonic.db-shm", "Main DB SHM", False, False),
        # 5. Plugin databases
        ("shared/data/db/plugins/*.db", "Plugin databases", True, True),
        # 6. Avatars
        ("shared/avatars/", "Agent avatars", False, True),
        # 7. Environment configs
        (".env", "Root .env config", False, False),
        ("shared/.env", "Shared .env config", False, False),
        # 8. Update state
        ("shared/update/update_state.json", "Update state", False, False),
        # 9. Server log
        ("shared/run/server.log", "Server runtime log", False, False),
        # 10. Plugin data dirs
        ("plugins/", "Plugin data directories", False, True),
        # 11. Plugin configs (handled separately via pattern)
        ("plugins/*/config.json", "Plugin configurations", False, True),
        # 12. Skill config
        ("skills/config.json", "Skill configuration", False, False),
        # 13. SSH keys
        ("keys/", "SSH keys", False, True),
        # 14. Plan files
        ("plan/", "Agent plan files", False, True),
    ]
    return sources


# Excluded paths (relative to ROOT)
_EXCLUDED_PATTERNS = [
    "backend/", "cli/", "app.py", "config.py", "routes/", "releases/", "current/",
    "plugins/",  # source code excluded; data + config.json included via specific patterns
    "skills/",   # source code excluded; config.json included via specific pattern
    "skills/*/.git/", "shared/data/icd10_*",
    ".git/", ".venv/", "__pycache__/", "*.pyc",
    "logs/", ".claude/", ".claude/settings.local.json",
]


def _should_exclude(rel_path, extra_excludes=None):
    """Check if a relative path matches any exclusion pattern."""
    import fnmatch
    all_excludes = list(_EXCLUDED_PATTERNS)
    if extra_excludes:
        all_excludes.extend(extra_excludes)
    normalized = rel_path.replace("\\", "/").rstrip("/")
    for pat in all_excludes:
        pat = pat.replace("\\", "/").rstrip("/")
        if normalized.startswith(pat.rstrip("/") + "/") or normalized == pat.rstrip("/"):
            return True
        if fnmatch.fnmatch(normalized, pat):
            return True
        # Also match individual files in excluded dirs
        for part in normalized.split("/"):
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def _should_exclude_file(filepath, extra_excludes=None):
    """Check if a specific file should be excluded."""
    # Exclude plugin source files but keep data/ and config.json
    if "/plugins/" in filepath:
        plugin_parts = filepath.split("/plugins/", 1)
        if len(plugin_parts) == 2:
            inner = plugin_parts[1]
            # Keep data/ directories and config.json
            if inner.startswith("data/") or inner.endswith("/config.json") or inner == "config.json":
                return False
            return True
    
    # Exclude skill source files but keep config.json
    if "/skills/" in filepath:
        skill_parts = filepath.split("/skills/", 1)
        if len(skill_parts) == 2:
            inner = skill_parts[1]
            if inner == "config.json":
                return False
            if "/" in inner:
                sub = inner.split("/")[0]
                if inner == f"{sub}/config.json":
                    return False
            return True
    
    return _should_exclude(filepath, extra_excludes)


# ---------------------------------------------------------------------------
# SHA-256 utilities
# ---------------------------------------------------------------------------

def _sha256_file(filepath):
    """Compute SHA-256 hash of a file. Returns hex string."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data):
    """Compute SHA-256 hash of bytes."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Database utilities
# ---------------------------------------------------------------------------

def _wal_checkpoint(db_path):
    """Run WAL checkpoint on a SQLite database to flush pending writes."""
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception:
        pass


def _snapshot_db(db_path, staging_path):
    """Create atomic zero-downtime snapshot of SQLite DB using backup API."""
    if not os.path.exists(db_path):
        return False
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(staging_path)
        src.backup(dst)
        src.close()
        dst.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Source collection
# ---------------------------------------------------------------------------

def _collect_files_for_source(rel_pattern, is_glob, staging_dir, quiet=False):
    """
    Collect files matching a source pattern into the staging directory.
    Returns list of (rel_path, abs_src_path, abs_staging_path).
    """
    collected = []
    abs_pattern = os.path.join(ROOT, rel_pattern)

    if is_glob and ("*" in rel_pattern or "?" in rel_pattern):
        # Glob expansion
        matches = glob.glob(abs_pattern, recursive=False)
        for match in sorted(matches):
            rel = os.path.relpath(match, ROOT)
            if _should_exclude_file(rel):
                continue
            if os.path.isfile(match):
                dst = os.path.join(staging_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(match, dst)
                collected.append((rel, match, dst))
    elif is_glob and os.path.isdir(abs_pattern):
        # Directory: walk recursively
        for dirpath, dirnames, filenames in os.walk(abs_pattern):
            # Skip excluded dirs
            dirnames[:] = [d for d in dirnames if not _should_exclude(
                os.path.relpath(os.path.join(dirpath, d), ROOT)
            )]
            for fname in sorted(filenames):
                src = os.path.join(dirpath, fname)
                rel = os.path.relpath(src, ROOT)
                if _should_exclude_file(rel):
                    continue
                dst = os.path.join(staging_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                collected.append((rel, src, dst))
    elif os.path.isfile(abs_pattern):
        # Single file
        rel = rel_pattern
        if _should_exclude_file(rel):
            return collected
        dst = os.path.join(staging_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # DB files get special treatment via _snapshot_db
        shutil.copy2(abs_pattern, dst)
        collected.append((rel, abs_pattern, dst))

    return collected


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _create_manifest(staging_dir, file_list, version, archive_sha256=None):
    """
    Create backup-manifest.json with metadata, file list, and SHAs.
    file_list: list of (rel_path, staging_path, file_size)
    """
    manifest = {
        "version": "1.0",
        "evonic_version": version,
        "created_at": _datetime.now().isoformat(),
        "created_by": "evonic backup",
        "file_count": len(file_list),
        "total_size_bytes": sum(info[2] for info in file_list),
        "archive_sha256": archive_sha256,
        "files": []
    }

    for rel_path, staging_path, file_size in file_list:
        sha = _sha256_file(staging_path)
        manifest["files"].append({
            "path": rel_path,
            "size_bytes": file_size,
            "sha256": sha,
        })

    manifest_path = os.path.join(staging_dir, "backup-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        _json.dump(manifest, f, indent=2)

    return manifest_path, manifest


def _update_manifest_sha256(archive_path, sha256_value):
    """Re-open tar archive and update the manifest's archive_sha256 field."""
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            # Extract manifest
            members = tar.getmembers()
            for m in members:
                if m.name.endswith("backup-manifest.json"):
                    manifest_data = _json.loads(tar.extractfile(m).read().decode("utf-8"))
                    manifest_data["archive_sha256"] = sha256_value
                    manifest_bytes = _json.dumps(manifest_data, indent=2).encode("utf-8")
                    break
            else:
                return False

        # Replace manifest in archive
        # tarfile doesn't support in-place update, so we rebuild
        import io
        new_archive = archive_path + ".tmp"
        with tarfile.open(archive_path, "r:gz") as tar_in:
            with tarfile.open(new_archive, "w:gz", format=tarfile.PAX_FORMAT) as tar_out:
                for member in tar_in.getmembers():
                    if member.name.endswith("backup-manifest.json"):
                        # Create a new TarInfo for the updated manifest
                        info = tarfile.TarInfo(name=member.name)
                        info.size = len(manifest_bytes)
                        info.mtime = member.mtime
                        tar_out.addfile(info, io.BytesIO(manifest_bytes))
                    else:
                        fobj = tar_in.extractfile(member)
                        tar_out.addfile(member, fobj)
        os.replace(new_archive, archive_path)
        return True
    except Exception as e:
        print(f"Warning: Could not update manifest SHA-256 in archive: {e}")
        return False


# ---------------------------------------------------------------------------
# Archive utilities
# ---------------------------------------------------------------------------

def _create_archive(staging_dir, output_path, fmt="gz"):
    """Create compressed tar archive from staging directory."""
    mode_map = {"gz": "w:gz", "bz2": "w:bz2", "zip": None}
    if fmt == "zip":
        # Use shutil for zip
        base = output_path
        if base.endswith(".tar.gz"):
            base = base[:-7] + ".zip"
        elif base.endswith(".tar.bz2"):
            base = base[:-8] + ".zip"
        else:
            base = base + ".zip"
        shutil.make_archive(base.replace(".zip", ""), "zip", staging_dir)
        return base

    mode = mode_map.get(fmt, "w:gz")
    with tarfile.open(output_path, mode, format=tarfile.PAX_FORMAT) as tar:
        for root, dirs, files in os.walk(staging_dir):
            for fname in files:
                full_path = os.path.join(root, fname)
                arcname = os.path.relpath(full_path, staging_dir)
                tar.add(full_path, arcname=arcname)
    return output_path


def _archive_sha256(archive_path):
    """Compute SHA-256 of the archive file."""
    return _sha256_file(archive_path)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def _read_manifest_from_archive(archive_path):
    """Read backup-manifest.json from inside a tar.gz archive."""
    if not os.path.exists(archive_path):
        return None, "Backup file not found"
    
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            for member in tar.getmembers():
                if member.name.endswith("backup-manifest.json"):
                    return _json.loads(tar.extractfile(member).read().decode("utf-8")), None
            return None, "No backup-manifest.json found in archive"
    except tarfile.ReadError as e:
        return None, f"Invalid or corrupt archive: {e}"
    except Exception as e:
        return None, f"Error reading archive: {e}"


def _verify_archive(archive_path, manifest):
    """Verify all files in archive match manifest SHAs."""
    if not os.path.exists(archive_path):
        return False, "Archive not found"
    
    # Note: archive-level SHA-256 is stored in manifest as metadata but
    # cannot be self-referentially verified. File-level SHA-256 verification
    # provides equivalent integrity guarantees for all backed-up data.
    
    # Extract to temp and verify each file
    tmpdir = tempfile.mkdtemp(prefix="evonic-verify-")
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            tar.extractall(tmpdir, filter="data")
        
        for finfo in manifest.get("files", []):
            fpath = os.path.join(tmpdir, finfo["path"])
            if not os.path.exists(fpath):
                return False, f"Missing file in archive: {finfo['path']}"
            computed = _sha256_file(fpath)
            expected = finfo["sha256"]
            if computed != expected:
                return False, f"SHA-256 mismatch: {finfo['path']} (expected {expected}, got {computed})"
        
        return True, "All files verified successfully"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _list_archive_contents(archive_path, manifest=None):
    """List contents of a backup archive."""
    if manifest is None:
        manifest, err = _read_manifest_from_archive(archive_path)
        if err:
            print(f"Error: {err}")
            return
    
    print(f"Backup: {os.path.basename(archive_path)}")
    print(f"Created: {manifest.get('created_at', 'unknown')}")
    print(f"Evonic version: {manifest.get('evonic_version', 'unknown')}")
    print(f"Files: {manifest.get('file_count', 0)}")
    print(f"Total size: {manifest.get('total_size_bytes', 0):,} bytes")
    print(f"Archive SHA-256: {manifest.get('archive_sha256', 'not present')}")
    print()
    print(f"{'Path':<60} {'Size':>12} {'SHA-256'}")
    print("-" * 140)
    for finfo in manifest.get("files", []):
        sha_short = finfo["sha256"][:16] + "..."
        print(f"{finfo['path']:<60} {finfo['size_bytes']:>12,} {sha_short}")


# ---------------------------------------------------------------------------
# Path traversal safety
# ---------------------------------------------------------------------------

def _safe_extract(tar, dest_dir):
    """Extract tar archive safely, preventing path traversal."""
    dest_dir = os.path.abspath(dest_dir)
    for member in tar.getmembers():
        # Resolve the target path
        target = os.path.abspath(os.path.join(dest_dir, member.name))
        # Reject any path outside dest_dir
        if not target.startswith(dest_dir + os.sep) and target != dest_dir:
            print(f"WARNING: Rejecting path traversal: {member.name}")
            continue
        if member.isdir():
            os.makedirs(target, exist_ok=True)
        elif member.isfile():
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tar.extractfile(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        # Handle symlinks (reject for safety)
        elif member.issym() or member.islnk():
            print(f"WARNING: Skipping symlink/hardlink in backup: {member.name}")


# ---------------------------------------------------------------------------
# Rollback system
# ---------------------------------------------------------------------------

def _create_rollback_copies(file_list):
    """Create .bak copies of all files that will be overwritten during restore."""
    rollbacks = []
    for rel_path in file_list:
        target = os.path.join(ROOT, rel_path)
        if os.path.exists(target):
            bak = target + ".evonic-rollback"
            try:
                if os.path.isdir(target):
                    shutil.copytree(target, bak, symlinks=False)
                else:
                    shutil.copy2(target, bak)
                rollbacks.append((target, bak))
            except Exception as e:
                print(f"Warning: Could not create rollback for {rel_path}: {e}")
    return rollbacks


def _rollback_restore(rollbacks):
    """Restore all .bak copies (reverse the restore)."""
    restored = 0
    failed = 0
    for target, bak in rollbacks:
        try:
            if os.path.isdir(bak):
                if os.path.exists(target):
                    shutil.rmtree(target, ignore_errors=True)
                shutil.move(bak, target)
            else:
                shutil.move(bak, target)
            restored += 1
        except Exception as e:
            print(f"Rollback failed for {target}: {e}")
            failed += 1
    return restored, failed


def _cleanup_rollback_copies(rollbacks):
    """Remove leftover .bak files after successful restore."""
    for _, bak in rollbacks:
        if os.path.exists(bak):
            try:
                if os.path.isdir(bak):
                    shutil.rmtree(bak, ignore_errors=True)
                else:
                    os.remove(bak)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Post-restore validation
# ---------------------------------------------------------------------------

def _validate_restored_db():
    """Validate the restored database integrity."""
    db_path = os.path.join(ROOT, "shared", "db", "evonic.db")
    if not os.path.exists(db_path):
        return False, "Database file not found after restore"

    try:
        conn = sqlite3.connect(db_path)
        
        # Check agent count
        cur = conn.execute("SELECT count(*) FROM agents")
        count = cur.fetchone()[0]
        if count == 0:
            conn.close()
            return False, "Database has zero agents after restore"
        
        # Integrity check
        cur = conn.execute("PRAGMA integrity_check")
        result = cur.fetchone()[0]
        conn.close()
        
        if result.lower() != "ok":
            return False, f"Database integrity check failed: {result}"
        
        return True, f"Database valid ({count} agents, integrity OK)"
    except Exception as e:
        return False, f"Database validation error: {e}"


# ---------------------------------------------------------------------------
# Backup command
# ---------------------------------------------------------------------------

def backup_command(output=None, fmt="gz", quiet=False, exclude=None, encrypt=False):
    """Create a full Evonic backup archive."""
    
    # Parse options
    extra_excludes = list(exclude) if exclude else []
    
    # Get version
    version = _get_evonic_version()
    
    # Generate default output filename
    timestamp = _datetime.now().strftime("%Y%m%d-%H%M")
    ext_map = {"gz": ".tar.gz", "bz2": ".tar.bz2", "zip": ".zip"}
    ext = ext_map.get(fmt, ".tar.gz")
    default_name = f"evonic-backup-{timestamp}{ext}"

    if output is None:
        output = default_name
    elif os.path.isdir(output):
        # -o points to a directory — place backup inside with default filename
        output = os.path.join(output, default_name)

    output = os.path.abspath(output)
    
    if not quiet:
        print(f"Evonic Backup v{version}")
        print(f"Output: {output}")
        print(f"Format: {fmt}")
        if encrypt:
            print("Encryption: AES-256-GCM (passphrase will be prompted)")
        print()
    
    # Handle encryption passphrase
    passphrase = None
    if encrypt:
        if not _ENCRYPTION_AVAILABLE:
            print("Error: Encryption not available (backup_crypto module not found)")
            sys.exit(1)
        passphrase = getpass.getpass("Enter encryption passphrase: ")
        confirm = getpass.getpass("Confirm encryption passphrase: ")
        if passphrase != confirm:
            print("Error: Passphrases do not match")
            sys.exit(1)
        if len(passphrase) < 8:
            print("Error: Passphrase must be at least 8 characters")
            sys.exit(1)
    
    # Step 1: WAL checkpoint on main DB
    if not quiet:
        print("Running WAL checkpoint on main database...")
    evonic_db = os.path.join(ROOT, "shared", "db", "evonic.db")
    _wal_checkpoint(evonic_db)
    
    # Also checkpoint plugin databases
    plugin_db_pattern = os.path.join(ROOT, "shared", "data", "db", "plugins", "*.db")
    for pdb in glob.glob(plugin_db_pattern):
        _wal_checkpoint(pdb)
    
    # Step 2: Create staging directory
    staging_dir = tempfile.mkdtemp(prefix="evonic-backup-")
    if not quiet:
        print(f"Staging directory: {staging_dir}")
    
    try:
        # Step 3: Collect all sources
        sources = _build_backup_sources()
        all_files = []  # (rel_path, staging_path, size_bytes)
        db_files_collected = []  # DB files needing snapshot
        
        for rel_pattern, label, is_db, is_glob in sources:
            if is_db:
                # DB files get atomic snapshot
                abs_src = os.path.join(ROOT, rel_pattern)
                if os.path.exists(abs_src):
                    staging_path = os.path.join(staging_dir, rel_pattern)
                    os.makedirs(os.path.dirname(staging_path), exist_ok=True)
                    if not quiet:
                        print(f"  Snapshot DB: {rel_pattern}")
                    if _snapshot_db(abs_src, staging_path):
                        size = os.path.getsize(staging_path)
                        all_files.append((rel_pattern, staging_path, size))
                    else:
                        if not quiet:
                            print(f"  Warning: Failed to snapshot {rel_pattern}")
            else:
                # Regular files/directories
                if not quiet:
                    print(f"  Collecting: {label}")
                collected = _collect_files_for_source(
                    rel_pattern, is_glob, staging_dir, quiet
                )
                for rel, src, dst in collected:
                    if _should_exclude_file(rel, extra_excludes):
                        continue
                    size = os.path.getsize(src)
                    all_files.append((rel, dst, size))
        
        # Deduplicate (in case glob patterns overlap)
        seen = set()
        unique_files = []
        for rel, path, size in all_files:
            if rel not in seen:
                seen.add(rel)
                unique_files.append((rel, path, size))
        all_files = unique_files
        
        if not all_files:
            print("Error: No files collected. Check that Evonic is properly installed.")
            sys.exit(1)
        
        if not quiet:
            print(f"\nCollected {len(all_files)} files, "
                  f"{sum(f[2] for f in all_files):,} bytes total")
        
        # Step 4: Create manifest
        if not quiet:
            print("Creating manifest...")
        manifest_path, manifest = _create_manifest(staging_dir, all_files, version)
        # Add manifest itself to the file list for archive inclusion
        manifest_size = os.path.getsize(manifest_path)
        manifest_rel = "backup-manifest.json"
        all_files.append((manifest_rel, manifest_path, manifest_size))
        
        # Step 5: Create tar archive
        if not quiet:
            print(f"Creating archive ({fmt})...")
        
        if encrypt:
            # Create unencrypted archive first, then encrypt
            tmp_archive = output + ".plain"
            archive_path = _create_archive(staging_dir, tmp_archive, fmt)
            archive_sha = _archive_sha256(archive_path)
            
            # Update manifest with archive SHA
            _update_manifest_sha256(archive_path, archive_sha)
            
            # Encrypt
            if not quiet:
                print("Encrypting archive...")
            encrypt_file_aes256gcm(archive_path, output, passphrase)
            os.remove(archive_path)
            # Recompute SHA of encrypted file
            archive_sha = _archive_sha256(output)
        else:
            archive_path = _create_archive(staging_dir, output, fmt)
            archive_sha = _archive_sha256(archive_path)
            # Update manifest with archive SHA, then recompute
            _update_manifest_sha256(archive_path, archive_sha)
            archive_sha = _archive_sha256(output)
        
        archive_size = os.path.getsize(output)
        
        print(f"\n  Backup complete!")
        print(f"  Path:      {output}")
        print(f"  Size:      {archive_size:,} bytes")
        print(f"  SHA-256:   {archive_sha}")
        print(f"  Files:     {len(all_files)}")
        
    finally:
        # Step 6: Cleanup staging
        if not quiet:
            print("Cleaning up staging directory...")
        shutil.rmtree(staging_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Restore command
# ---------------------------------------------------------------------------

def restore_command(backup_file, dry_run=False, force=False, no_restart=False):
    """Restore Evonic from a backup archive."""
    
    backup_file = os.path.abspath(backup_file)
    
    if not os.path.exists(backup_file):
        print(f"Error: Backup file not found: {backup_file}")
        sys.exit(1)
    
    print(f"Evonic Restore")
    print(f"Backup file: {backup_file}")
    print()
    
    # Step 1: Verify archive
    print("Verifying backup...")
    manifest, err = _read_manifest_from_archive(backup_file)
    if err:
        print(f"Error: {err}")
        sys.exit(1)
    
    verified, verify_msg = _verify_archive(backup_file, manifest)
    if not verified:
        print(f"Error: Verification failed: {verify_msg}")
        print("The backup may be corrupted. Restore aborted.")
        sys.exit(1)
    print(f"Verification: OK ({manifest.get('file_count', 0)} files verified)")
    
    # Check if encrypted (look for encryption header)
    is_encrypted = False
    try:
        with open(backup_file, "rb") as f:
            header = f.read(4)
            # tar.gz starts with 0x1f 0x8b (gzip magic)
            if header[:2] != b"\x1f\x8b":
                is_encrypted = True
    except Exception:
        pass
    
    # If encrypted, prompt for passphrase
    if is_encrypted:
        if not _ENCRYPTION_AVAILABLE:
            print("Error: File appears encrypted but encryption module is not available")
            sys.exit(1)
        passphrase = getpass.getpass("Enter decryption passphrase: ")
        decrypted_path = backup_file + ".decrypted"
        try:
            print("Decrypting...")
            decrypt_file_aes256gcm(backup_file, decrypted_path, passphrase)
            backup_file = decrypted_path
            # Re-verify decrypted archive
            manifest, err = _read_manifest_from_archive(backup_file)
            if err:
                print(f"Error reading decrypted archive: {err}")
                sys.exit(1)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        decrypted_path = None
    
    # Step 2: Show contents
    print()
    _list_archive_contents(backup_file, manifest)
    
    # Step 3: Dry run handling
    if dry_run:
        print("\nDry run complete. No changes were made.")
        if decrypted_path:
            os.remove(decrypted_path)
        return
    
    # Step 4: Confirmation
    if not force:
        print(f"\nThis will stop the server and restore {manifest.get('file_count', 0)} files.")
        response = input("Continue? [y/N] ").strip().lower()
        if response not in ("y", "yes"):
            print("Restore cancelled.")
            if decrypted_path:
                os.remove(decrypted_path)
            return
    
    # Step 5: Extract to staging
    staging_dir = tempfile.mkdtemp(prefix="evonic-restore-")
    print(f"\nExtracting to staging: {staging_dir}")
    
    try:
        with tarfile.open(backup_file, "r:*") as tar:
            _safe_extract(tar, staging_dir)
        
        # Read manifest from staging
        manifest_path = None
        for root, dirs, files in os.walk(staging_dir):
            if "backup-manifest.json" in files:
                manifest_path = os.path.join(root, "backup-manifest.json")
                break
        
        if manifest_path is None:
            print("Error: No manifest found in extracted archive")
            sys.exit(1)
        
        with open(manifest_path, "r") as f:
            staged_manifest = _json.load(f)
        
        file_list = staged_manifest.get("files", [])
        
        # Step 6: Create rollback copies
        print("Creating rollback copies...")
        file_paths = [f["path"] for f in file_list if f["path"] != "backup-manifest.json"]
        rollbacks = _create_rollback_copies(file_paths)
        print(f"  {len(rollbacks)} rollback copies created")
        
        # Step 7: Stop server
        # Find the extracted manifest's source directory
        # The staging dir has the same structure as ROOT
        extract_root = staging_dir
        if manifest_path:
            extract_root = os.path.dirname(manifest_path)
        
        print("Stopping server...")
        stop_server()
        time.sleep(2)
        
        # Step 8: Restore files
        print(f"Restoring {len(file_paths)} files...")
        restored = 0
        skipped = 0
        for finfo in file_list:
            rel_path = finfo["path"]
            if rel_path == "backup-manifest.json":
                continue
            src = os.path.join(extract_root, rel_path)
            dst = os.path.join(ROOT, rel_path)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    restored += 1
                except Exception as e:
                    print(f"  Error restoring {rel_path}: {e}")
                    skipped += 1
        
        print(f"  {restored} files restored" + (f", {skipped} skipped" if skipped else ""))
        
        # Step 9: Post-restore validation
        print("Validating restored database...")
        valid, msg = _validate_restored_db()
        
        if not valid:
            print(f"ERROR: {msg}")
            print("Auto-rolling back restore...")
            _rollback_restore(rollbacks)
            print("Rollback complete. System is in pre-restore state.")
            sys.exit(1)
        
        print(f"  {msg}")
        
        # Cleanup rollback copies
        _cleanup_rollback_copies(rollbacks)
        
        # Step 10: Restart server
        if not no_restart:
            print("Restarting server...")
            start_server(daemon=True)
            time.sleep(3)
            
            # Health check
            pid = _get_pid()
            if _is_running(pid):
                print(f"Server restarted successfully (PID: {pid})")
            else:
                print("Warning: Server may not have started. Check 'evonic status'.")
        else:
            print("Server restart skipped (--no-restart). Run 'evonic start -d' to start.")
        
        print("\n  Restore complete!")
        
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if decrypted_path and os.path.exists(decrypted_path):
            os.remove(decrypted_path)


# ---------------------------------------------------------------------------
# Verify command
# ---------------------------------------------------------------------------

def verify_command(backup_file):
    """Verify a backup archive's integrity against its manifest."""
    backup_file = os.path.abspath(backup_file)
    
    if not os.path.exists(backup_file):
        print(f"Error: Backup file not found: {backup_file}")
        sys.exit(1)
    
    print(f"Verifying backup: {backup_file}")
    print()
    
    manifest, err = _read_manifest_from_archive(backup_file)
    if err:
        print(f"ERROR: {err}")
        sys.exit(1)
    
    print(f"Backup metadata:")
    print(f"  Created:      {manifest.get('created_at', 'unknown')}")
    print(f"  Version:      {manifest.get('evonic_version', 'unknown')}")
    print(f"  Files:        {manifest.get('file_count', 0)}")
    print(f"  Total size:   {manifest.get('total_size_bytes', 0):,} bytes")
    print(f"  Archive SHA:  {manifest.get('archive_sha256', 'not present')}")
    print()
    
    verified, msg = _verify_archive(backup_file, manifest)
    
    if verified:
        print("VERIFICATION PASSED")
        print(f"All {manifest.get('file_count', 0)} files verified.")
    else:
        print(f"VERIFICATION FAILED: {msg}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# List command
# ---------------------------------------------------------------------------

def list_command(backup_file):
    """List contents of a backup archive."""
    backup_file = os.path.abspath(backup_file)
    
    if not os.path.exists(backup_file):
        print(f"Error: Backup file not found: {backup_file}")
        sys.exit(1)
    
    manifest, err = _read_manifest_from_archive(backup_file)
    if err:
        print(f"Error: {err}")
        sys.exit(1)
    
    _list_archive_contents(backup_file, manifest)

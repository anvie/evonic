#!/usr/bin/env python3
"""Evonic CLI — entry point for start/stop/status/plugin commands."""

import argparse
import sys
import os
import signal
import time
from datetime import datetime
# Ensure the project root is on sys.path so we can import app and its modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configure centralized logging
try:
    from backend.logging_config import configure as configure_logging
    configure_logging()
except ImportError:
    pass

# Banner color by day of week (0=Monday, 1=Tuesday, etc.)
_DAY_COLORS = [
    "\033[91m",  # Monday    - Bright Red
    "\033[35m",  # Tuesday   - Magenta
    "\033[32m",  # Wednesday - Green
    "\033[93m",  # Thursday  - Bright Yellow
    "\033[34m",  # Friday    - Blue
    "\033[36m",  # Saturday  - Cyan
    "\033[93m",  # Sunday    - Bright Yellow (gold)
]
_RESET = "\033[0m"

EVONIC_BANNER = _DAY_COLORS[datetime.now().weekday()] + r"""

         ░░░░░░░░░░░░░░░░░░░░░░░░
       ░░▒▒███████████████████▒▒░░
     ░░▒▒██▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓██▒▒░░      ___________                  .__.
     ░░▒▒██▓▓▒▒░░░░░░░░░░▒▒▓▓██▒▒░░      \_   _____/__  ______   ____ |__| _____
     ░░▒▒██▓▓▒▒░░ ░░  ░░ ▒▒▓▓██▒▒░░       |    __)_\  \/ /    \ /    \|  |/ ____\
     ░░▒▒██▓▓▒▒░░░░░░░░░░▒▒▓▓██▒▒░░       |        \\   (  ()  )   |  \  \  \____
     ░░▒▒████████████████████▒▒░░░░      /_______  / \_/ \____/|___|  /__|\___  /
       ░░▒▒░░░░░░░░░░░░░░░░░░░░                  \/                 \/        \/
        ▓▓ ░░▓▓ ░░ ▓▓ ░░▓▓ ░░▓▓
      ▒▒ ░░ ▒▒ ▓▓  ▒▒  ▓▓   ▒▒▒
        ░░ ░▒░  ▓▓  ▒▒  ▓▓  ░▓
          ▒▒ ▒▒░▒▒  ▒▒░░▒  ▒▒
            ░░    ▓▓▓▓    ▓▓
              ▒▒        ▒▒

""" + _RESET

from cli.commands import (
    start_server, stop_server, status_server, restart_server,
    plugin_list, plugin_install, plugin_uninstall, plugin_enable, plugin_disable, plugin_new,
    plugin_reload, plugin_hotreload_enable, plugin_hotreload_disable, plugin_hotreload_status,
    skill_list, skill_add, skill_get, skill_rm,
    skillset_list, skillset_get, skillset_apply,
    agent_list, agent_get, agent_add, agent_enable, agent_disable, agent_remove,
    model_list, model_get, model_add, model_rm,
    channel_approve,
    update_server, setup_wizard, pass_setup,
    doctor_command,
    reconfigure_wizard,
)


def main():
    parser = argparse.ArgumentParser(
        prog="evonic",
        description="Evonic CLI — manage the Evonic platform server and plugins",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- start ---
    start_parser = subparsers.add_parser("start", help="Start the Flask server")
    start_parser.add_argument(
        "--port", type=int, default=None, help="Port to run on (default: from config or 8080)"
    )
    start_parser.add_argument(
        "--host", type=str, default=None, help="Host to bind (default: from config or 0.0.0.0)"
    )
    start_parser.add_argument(
        "--debug", action="store_true", default=None, help="Enable debug mode"
    )
    start_parser.add_argument(
        "-d", "--daemon", action="store_true", default=False, help="Run server in background (daemon mode)"
    )

    # --- setup ---
    subparsers.add_parser(
        "setup",
        help="Interactive first-time setup wizard",
        description="Configure your LLM provider, create the super agent, and set the communication style.",
    )

    # --- pass ---
    subparsers.add_parser(
        "pass",
        help="Set or change the admin dashboard password",
        description="Set a new admin password or change an existing one for web dashboard authentication.",
    )

    # --- reconfigure ---
    reconfigure_parser = subparsers.add_parser(
        "reconfigure",
        help="Reconfigure an existing Evonic setup",
        description="Change LLM provider, model, communication style, language, sandbox, and password on an already configured Evonic instance.",
    )
    reconfigure_parser.add_argument(
        "--supervisor", action="store_true", default=False,
        help="Reconfigure the supervisor daemon settings (poll interval, ports, Telegram, etc.) and save to supervisor/config.json",
    )

    # --- update ---
    update_parser = subparsers.add_parser("update", help="Check for and apply self-updates")
    update_parser.add_argument(
        "channel", nargs="?", default=None,
        choices=["nightly"],
        help="Update channel: 'nightly' pulls the latest from origin/main (no tag)"
    )
    update_parser.add_argument(
        "--check", action="store_true", default=False,
        help="Only check for available updates, do not apply"
    )
    update_parser.add_argument(
        "--force", action="store_true", default=False,
        help="Skip signature verification (development only)"
    )
    update_parser.add_argument(
        "--tag", type=str, default=None,
        help="Update to a specific tag instead of latest"
    )
    update_parser.add_argument(
        "--rollback", action="store_true", default=False,
        help="Roll back to the previous stable release"
    )

    # --- stop ---
    subparsers.add_parser("stop", help="Stop the running server")

    # --- status ---
    subparsers.add_parser("status", help="Check if the server is running")

    # --- restart ---
    subparsers.add_parser("restart", help="Restart the server (stop then start in daemon mode)")

    # --- doctor ---
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run system diagnostics and health checks",
        description="Check Evonic system health: environment, config, connections, services, files, agents, skills, and LLM providers.",
    )
    doctor_parser.add_argument(
        "--quick", action="store_true", default=False,
        help="Skip slow checks (LLM provider tests)",
    )

    # --- plugin ---
    plugin_parser = subparsers.add_parser(
        "plugin",
        help="Manage plugins (install, uninstall, list, enable, disable)",
        description="Manage Evonic plugins. Available subcommands: list, install, uninstall, enable, disable.",
    )
    plugin_subparsers = plugin_parser.add_subparsers(
        dest="plugin_command", help="Plugin management commands"
    )

    # plugin list
    plugin_subparsers.add_parser(
        "list",
        help="List all installed plugins",
        description="Display a table of all installed plugins with their ID, name, version, status, and event count.",
    )

    # plugin install
    install_parser = plugin_subparsers.add_parser(
        "install",
        help="Install a plugin from a zip file or directory",
        description="Install a plugin by providing a path to a .zip file or a directory containing plugin.json.",
    )
    install_parser.add_argument(
        "source",
        help="Path to a plugin .zip file or directory containing plugin.json",
    )

    # plugin uninstall
    uninstall_parser = plugin_subparsers.add_parser(
        "uninstall",
        help="Uninstall a plugin by its ID",
        description="Remove an installed plugin. System plugins cannot be uninstalled.",
    )
    uninstall_parser.add_argument(
        "name",
        help="Plugin ID to uninstall",
    )

    # plugin enable
    enable_parser = plugin_subparsers.add_parser(
        "enable",
        help="Enable a plugin by its ID",
        description="Enable a disabled plugin so it can process events and register its handlers.",
    )
    enable_parser.add_argument(
        "plugin_id",
        help="Plugin ID to enable",
    )

    # plugin disable
    disable_parser = plugin_subparsers.add_parser(
        "disable",
        help="Disable a plugin by its ID",
        description="Disable an enabled plugin so it stops processing events and responding to requests.",
    )
    disable_parser.add_argument(
        "plugin_id",
        help="Plugin ID to disable",
    )

    # plugin new
    plugin_subparsers.add_parser(
        "new",
        help="Scaffold a new plugin project",
        description="Interactive wizard to create a new plugin scaffold in plugins/ directory. Prompts for name, description, and author.",
    )

    # plugin reload
    reload_parser = plugin_subparsers.add_parser(
        "reload",
        help="Manually reload a plugin",
        description="Reload a plugin by unloading and loading it again. Useful during development.",
    )
    reload_parser.add_argument(
        "plugin_id",
        help="Plugin ID to reload",
    )

    # plugin hotreload-enable
    hotreload_enable_parser = plugin_subparsers.add_parser(
        "hotreload-enable",
        help="Enable hot reload for a plugin",
        description="Enable automatic reloading when plugin files change. Watches .py, .json, .yaml, and .md files.",
    )
    hotreload_enable_parser.add_argument(
        "plugin_id",
        nargs="?",
        default=None,
        help="Plugin ID to watch (omit to enable globally)",
    )

    # plugin hotreload-disable
    hotreload_disable_parser = plugin_subparsers.add_parser(
        "hotreload-disable",
        help="Disable hot reload for a plugin",
        description="Stop watching a plugin for file changes.",
    )
    hotreload_disable_parser.add_argument(
        "plugin_id",
        nargs="?",
        default=None,
        help="Plugin ID to stop watching (omit to disable globally)",
    )

    # plugin hotreload-status
    plugin_subparsers.add_parser(
        "hotreload-status",
        help="Show hot reload status",
        description="Display which plugins are being watched and pending reloads.",
    )

    # --- skill ---
    skill_parser = subparsers.add_parser(
        "skill",
        help="Manage skills (list, add, get, rm)",
        description="Manage Evonic skills. Available subcommands: list, add, get, rm.",
    )
    skill_subparsers = skill_parser.add_subparsers(
        dest="skill_command", help="Skill management commands"
    )

    # skill list
    skill_subparsers.add_parser(
        "list",
        help="List all installed skills",
        description="Display a table of all installed skills with their ID, name, version, and status.",
    )

    # skill add
    skill_add_parser = skill_subparsers.add_parser(
        "add",
        help="Install a skill from a local path, zip file, or GitHub URL",
        description="Install a skill by providing a local path, .zip file, or GitHub repository URL.",
    )
    skill_add_parser.add_argument(
        "source",
        help="Local path to a skill directory/zip, or a GitHub URL (e.g. https://github.com/user/repo)",
    )

    # skill get
    skill_get_parser = skill_subparsers.add_parser(
        "get",
        help="Show details of a specific skill",
        description="Display detailed information about an installed skill including tools and variables.",
    )
    skill_get_parser.add_argument(
        "skill_id",
        help="Skill ID to look up",
    )

    # skill rm
    skill_rm_parser = skill_subparsers.add_parser(
        "rm",
        help="Uninstall a skill by its ID",
        description="Remove an installed skill. Built-in/core skills cannot be removed.",
    )
    skill_rm_parser.add_argument(
        "skill_id",
        help="Skill ID to remove",
    )

    # --- skillset ---
    skillset_parser = subparsers.add_parser(
        "skillset",
        help="Manage skillset templates (list, get, apply)",
        description="Manage Evonic skillset templates. Available subcommands: list, get, apply.",
    )
    skillset_subparsers = skillset_parser.add_subparsers(
        dest="skillset_command", help="Skillset management commands"
    )

    # skillset list
    skillset_subparsers.add_parser(
        "list",
        help="List all available skillset templates",
        description="Display a table of all available skillset templates with their ID, name, description, tool count, and skill count.",
    )

    # skillset get
    skillset_get_parser = skillset_subparsers.add_parser(
        "get",
        help="Show details of a specific skillset template",
        description="Display detailed information about a skillset template including tools, skills, and system prompt.",
    )
    skillset_get_parser.add_argument(
        "skillset_id",
        help="Skillset ID to look up",
    )

    # skillset apply
    skillset_apply_parser = skillset_subparsers.add_parser(
        "apply",
        help="Create a new agent from a skillset template",
        description="Create a new agent pre-configured from a skillset template.",
    )
    skillset_apply_parser.add_argument(
        "skillset_id",
        help="Skillset template ID to apply",
    )
    skillset_apply_parser.add_argument(
        "--agent-id",
        required=True,
        help="Agent ID for the new agent (alphanumeric and underscores only)",
    )
    skillset_apply_parser.add_argument(
        "--name",
        default=None,
        help="Display name for the new agent (optional, uses skillset default)",
    )
    skillset_apply_parser.add_argument(
        "--description",
        default=None,
        help="Description for the new agent (optional, uses skillset default)",
    )
    skillset_apply_parser.add_argument(
        "--model",
        default=None,
        help="Model override for the new agent (optional, uses skillset default)",
    )

    # --- agent ---
    agent_parser = subparsers.add_parser(
        "agent",
        help="Manage agents (list, get, add, enable, disable, remove)",
        description="Manage Evonic agents. Available subcommands: list, get, add, enable, disable, remove.",
    )
    agent_subparsers = agent_parser.add_subparsers(
        dest="agent_command", help="Agent management commands"
    )

    # agent list
    agent_subparsers.add_parser(
        "list",
        help="List all agents",
        description="Display a table of all agents with their ID, name, status, tool count, and channel count.",
    )

    # agent get
    agent_get_parser = agent_subparsers.add_parser(
        "get",
        help="Show details of a specific agent",
        description="Display detailed information about an agent including tools and channels.",
    )
    agent_get_parser.add_argument(
        "agent_id",
        help="Agent ID to look up",
    )

    # agent add
    agent_add_parser = agent_subparsers.add_parser(
        "add",
        help="Create a new agent",
        description="Create a new agent. Optionally use --skillset to apply a template.",
    )
    agent_add_parser.add_argument(
        "agent_id",
        help="Agent ID (alphanumeric and underscores only)",
    )
    agent_add_parser.add_argument(
        "--name",
        required=True,
        help="Display name for the agent",
    )
    agent_add_parser.add_argument(
        "--description",
        default=None,
        help="Description for the agent",
    )
    agent_add_parser.add_argument(
        "--model",
        default=None,
        help="Model override for the agent",
    )
    agent_add_parser.add_argument(
        "--skillset",
        default=None,
        help="Skillset template ID to apply (pre-configures tools and prompt)",
    )

    # agent enable
    agent_enable_parser = agent_subparsers.add_parser(
        "enable",
        help="Enable an agent",
        description="Enable a disabled agent so it can process messages.",
    )
    agent_enable_parser.add_argument(
        "agent_id",
        help="Agent ID to enable",
    )

    # agent disable
    agent_disable_parser = agent_subparsers.add_parser(
        "disable",
        help="Disable an agent",
        description="Disable an agent so it stops processing messages.",
    )
    agent_disable_parser.add_argument(
        "agent_id",
        help="Agent ID to disable",
    )

    # agent remove
    agent_remove_parser = agent_subparsers.add_parser(
        "remove",
        help="Remove an agent (with confirmation)",
        description="Permanently remove an agent. Requires interactive confirmation.",
    )
    agent_remove_parser.add_argument(
        "agent_id",
        help="Agent ID to remove",
    )

    # --- model ---
    model_parser = subparsers.add_parser(
        "model",
        help="Manage LLM models (list, get, add, rm)",
        description="Manage Evonic LLM models. Available subcommands: list, get, add, rm.",
    )
    model_subparsers = model_parser.add_subparsers(
        dest="model_command", help="Model management commands"
    )

    # model list
    model_subparsers.add_parser(
        "list",
        help="List all configured LLM models",
        description="Display a table of all LLM models with their ID, name, and provider.",
    )

    # model get
    model_get_parser = model_subparsers.add_parser(
        "get",
        help="Show details of a specific model",
        description="Display detailed information about a configured LLM model.",
    )
    model_get_parser.add_argument(
        "model_id",
        help="Model ID to look up",
    )

    # model add
    model_add_parser = model_subparsers.add_parser(
        "add",
        help="Add a new LLM model",
        description="Add a new LLM model configuration.",
    )
    model_add_parser.add_argument(
        "model_id",
        help="Model ID (alphanumeric and underscores only)",
    )
    model_add_parser.add_argument(
        "--name",
        required=True,
        help="Display name for the model",
    )
    model_add_parser.add_argument(
        "--provider",
        required=True,
        help="Provider (e.g. openai, anthropic, groq, openrouter)",
    )
    model_add_parser.add_argument(
        "--api-key",
        default=None,
        help="API key for the model provider",
    )
    model_add_parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL for the API endpoint",
    )

    # model rm
    model_rm_parser = model_subparsers.add_parser(
        "rm",
        help="Remove a model (with confirmation)",
        description="Remove a configured LLM model. Requires interactive confirmation.",
    )
    model_rm_parser.add_argument(
        "model_id",
        help="Model ID to remove",
    )

    # --- channel ---
    channel_parser = subparsers.add_parser(
        "channel",
        help="Manage channel pairing approvals",
        description="Manage Evonic channels. Available subcommands: approve.",
    )
    channel_subparsers = channel_parser.add_subparsers(
        dest="channel_command", help="Channel management commands"
    )

    # channel approve
    channel_approve_subparser = channel_subparsers.add_parser(
        "approve",
        help="Approve a pending channel pairing by code",
        description="Approve a pending channel pairing request using the 8-character pair code.",
    )
    channel_approve_subparser.add_argument(
        "pair_code",
        help="8-character pairing code (e.g. ABCDEFGH)",
    )

    # --- Plugin CLI commands (discovered dynamically) ---
    # Plugins can register CLI subcommands. The core CLI discovers them from
    # enabled plugins via plugin_manager.get_cli_commands().
    plugin_cli_commands = {}
    plugin_cli_parsers = {}  # cmd_name -> parser (for help printing)
    try:
        from backend.plugin_manager import plugin_manager
        plugin_cli_commands = plugin_manager.get_cli_commands()
        for cmd_name, cmd_info in plugin_cli_commands.items():
            sub = subparsers.add_parser(
                cmd_name,
                help=cmd_info.get('help', ''),
                description=cmd_info.get('description', ''),
            )
            plugin_cli_parsers[cmd_name] = sub
            # Register sub-subparsers if the command defines subcommands
            if cmd_info.get('subcommands'):
                sub_subparsers = sub.add_subparsers(dest=f"{cmd_name}_command")
                for sub_name, sub_info in cmd_info['subcommands'].items():
                    sub_parser = sub_subparsers.add_parser(
                        sub_name,
                        help=sub_info.get('help', ''),
                        description=sub_info.get('description', ''),
                    )
                    for arg_def in sub_info.get('arguments', []):
                        name = arg_def['name']
                        kwargs = {}
                        for k, v in arg_def.items():
                            if k not in ('name', 'short'):
                                kwargs[k] = v
                        short = arg_def.get('short')
                        if short:
                            names = [name, short]
                            sub_parser.add_argument(*names, **kwargs)
                        else:
                            sub_parser.add_argument(name, **kwargs)
    except Exception:
        pass

    args = parser.parse_args()

    if args.command is None:
        print(EVONIC_BANNER)
        parser.print_help()
        sys.exit(0)

    if args.command == "setup":
        setup_wizard()
    elif args.command == "pass":
        pass_setup()
    elif args.command == "reconfigure":
        reconfigure_wizard(supervisor=args.supervisor)
    elif args.command == "update":
        update_server(
            check_only=args.check,
            force=args.force,
            tag=args.tag,
            rollback_flag=args.rollback,
            nightly=(args.channel == "nightly"),
        )
    elif args.command == "start":
        start_server(port=args.port, host=args.host, debug=args.debug, daemon=args.daemon)
    elif args.command == "stop":
        stop_server()
    elif args.command == "status":
        status_server()
    elif args.command == "restart":
        restart_server()
    elif args.command == "doctor":
        doctor_command(quick=args.quick)
    elif args.command == "plugin":
        if args.plugin_command is None:
            plugin_parser.print_help()
            sys.exit(0)
        elif args.plugin_command == "list":
            plugin_list()
        elif args.plugin_command == "install":
            plugin_install(args.source)
        elif args.plugin_command == "uninstall":
            plugin_uninstall(args.name)
        elif args.plugin_command == "enable":
            plugin_enable(args.plugin_id)
        elif args.plugin_command == "disable":
            plugin_disable(args.plugin_id)
        elif args.plugin_command == "new":
            plugin_new()
        elif args.plugin_command == "reload":
            plugin_reload(args.plugin_id)
        elif args.plugin_command == "hotreload-enable":
            plugin_hotreload_enable(args.plugin_id)
        elif args.plugin_command == "hotreload-disable":
            plugin_hotreload_disable(args.plugin_id)
        elif args.plugin_command == "hotreload-status":
            plugin_hotreload_status()
    elif args.command == "skill":
        if args.skill_command is None:
            skill_parser.print_help()
            sys.exit(0)
        elif args.skill_command == "list":
            skill_list()
        elif args.skill_command == "add":
            skill_add(args.source)
        elif args.skill_command == "get":
            skill_get(args.skill_id)
        elif args.skill_command == "rm":
            skill_rm(args.skill_id)
    elif args.command == "skillset":
        if args.skillset_command is None:
            skillset_parser.print_help()
            sys.exit(0)
        elif args.skillset_command == "list":
            skillset_list()
        elif args.skillset_command == "get":
            skillset_get(args.skillset_id)
        elif args.skillset_command == "apply":
            skillset_apply(
                args.skillset_id,
                agent_id=args.agent_id,
                name=args.name,
                description=args.description,
                model=args.model,
            )
    elif args.command == "agent":
        if args.agent_command is None:
            agent_parser.print_help()
            sys.exit(0)
        elif args.agent_command == "list":
            agent_list()
        elif args.agent_command == "get":
            agent_get(args.agent_id)
        elif args.agent_command == "add":
            agent_add(
                args.agent_id,
                name=args.name,
                description=args.description,
                model=args.model,
                skillset=args.skillset,
            )
        elif args.agent_command == "enable":
            agent_enable(args.agent_id)
        elif args.agent_command == "disable":
            agent_disable(args.agent_id)
        elif args.agent_command == "remove":
            agent_remove(args.agent_id)
    elif args.command == "model":
        if args.model_command is None:
            model_parser.print_help()
            sys.exit(0)
        elif args.model_command == "list":
            model_list()
        elif args.model_command == "get":
            model_get(args.model_id)
        elif args.model_command == "add":
            model_add(
                args.model_id,
                name=args.name,
                provider=args.provider,
                api_key=args.api_key,
                base_url=args.base_url,
            )
        elif args.model_command == "rm":
            model_rm(args.model_id)
    elif args.command == "channel":
        if args.channel_command is None:
            channel_parser.print_help()
            sys.exit(0)
        elif args.channel_command == "approve":
            channel_approve(args.pair_code)
    else:
        # Dispatch to plugin CLI commands
        if args.command in plugin_cli_commands:
            cmd_info = plugin_cli_commands[args.command]
            subcmd_dest = f"{args.command}_command"
            subcmd = getattr(args, subcmd_dest, None)
            if subcmd is None:
                # No subcommand specified — print help
                if args.command in plugin_cli_parsers:
                    plugin_cli_parsers[args.command].print_help()
                sys.exit(0)
            # Look up subcommand info and call handler
            sub_info = cmd_info.get('subcommands', {}).get(subcmd)
            if sub_info and sub_info.get('handler'):
                handler_name = sub_info['handler']
                plugin_id = sub_info.get('plugin_id')
                module = None
                try:
                    from backend.plugin_manager import plugin_manager
                    module = plugin_manager._modules.get(plugin_id)
                except Exception:
                    pass
                if module:
                    handler_fn = getattr(module, handler_name, None)
                    if handler_fn:
                        # Build kwargs from args matching argument names
                        arg_defs = sub_info.get('arguments', [])
                        kw = {}
                        for arg_def in arg_defs:
                            arg_name = arg_def['name']
                            # Strip -- prefix for kwargs
                            clean = arg_name.lstrip('-').replace('-', '_')
                            val = getattr(args, clean, None)
                            if val is not None:
                                kw[clean] = val
                        handler_fn(**kw)
                    else:
                        print(f"Error: CLI handler '{handler_name}' not found in plugin '{plugin_id}'.")
                        sys.exit(1)
                else:
                    print(f"Error: Plugin module '{plugin_id}' not loaded.")
                    sys.exit(1)
            else:
                print(f"Error: No handler for '{args.command} {subcmd}'.")
                sys.exit(1)
        else:
            print(f"Error: Unknown command '{args.command}'.")
            sys.exit(1)


if __name__ == "__main__":
    main()

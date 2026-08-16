"""
Super Agent Administrative Tools

These tools are exclusively available to the super agent and provide
full platform management capabilities: creating/managing agents, assigning
tools, managing skills, etc.
"""

import os
import json
import shutil
from typing import Any, Dict, List, Optional, Callable

from models.db import db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENTS_DIR = os.path.join(BASE_DIR, 'agents')
WORKSPACE_DIR = os.path.join(BASE_DIR, 'shared', 'agents')


def _ensure_kb_dir(agent_id: str) -> str:
    d = os.path.join(AGENTS_DIR, agent_id, 'kb')
    os.makedirs(d, exist_ok=True)
    return d


def _write_system_prompt(agent_id: str, content: str):
    path = os.path.join(AGENTS_DIR, agent_id, 'SYSTEM.md')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _read_system_prompt(agent_id: str) -> str:
    path = os.path.join(AGENTS_DIR, agent_id, 'SYSTEM.md')
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            pass
    return ''


_NOTES_MD_TEMPLATE = """# Notes.md -- User Preferences & Instructions

This file stores your user's personal preferences, tastes, language
preferences, and communication style instructions.

## What to store here

- User's preferred language (e.g. "User prefers Bahasa Indonesia")
- Communication style preferences (e.g. "User likes concise answers",
  "User dislikes emoji")
- Personal instructions (e.g. "Call the user 'Pak'")
- Tastes and preferences (e.g. "User prefers bullet points over paragraphs")

## What NOT to store here (use `remember` instead)

- Factual/memorization data: addresses, phone numbers, email, birthday
- Secret/sensitive data: passwords, tokens, PINs, secret codes, bank accounts

## Usage

- Read this file: read_file(file_path="/_self/kb/notes.md")
- Update via write_file with path /_self/kb/notes.md
- Update immediately when the user gives a new preference
- Prioritize notes.md over `remember` for non-factual preference information
"""


# ==================== Tool Definitions ====================

_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": "List all agents on the platform with their status (enabled/disabled, tool count, channel count).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_agent",
            "description": "Create a new agent on the platform.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Agent ID (lowercase snake_case: alphanumeric and underscores only, unique)"
                    },
                    "name": {
                        "type": "string",
                        "description": "Display name for the agent"
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description of the agent's purpose"
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "System prompt / persona for the agent"
                    },
                    "model_id": {
                        "type": "string",
                        "description": "Optional model ID to assign (leave empty to use platform default)"
                    }
                },
                "required": ["id", "name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_agent",
            "description": "Update settings for an existing agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent's ID"
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "model_id": {"type": "string"},
                    "system_prompt": {"type": "string"},
                    "enabled": {
                        "type": "boolean",
                        "description": "Whether the agent is enabled"
                    },
                    "run_as_user": {
                        "type": "string",
                        "description": "Linux username to run bash/python as when sandbox is disabled. Set to empty string to clear. Only super agents can change this."
                    },
                    "workplace_id": {
                        "type": "string",
                        "description": "Workplace ID to assign this agent to (e.g. 'a15117f03576'). Use list_workplaces to discover available workplaces. Set to empty string to unassign."
                    },
                    "sandbox_enabled": {
                        "type": "boolean",
                        "description": "Whether the sandbox (Docker) execution environment is enabled for this agent."
                    },
                    "bash_exec_enabled": {
                        "type": "boolean",
                        "description": "Whether the agent can execute arbitrary bash commands."
                    }
                },
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_agent",
            "description": "Permanently delete an agent and all its data. Cannot delete the super agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent's ID"
                    }
                },
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "enable_agent",
            "description": "Enable a previously disabled agent so it can process messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent's ID"
                    }
                },
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "disable_agent",
            "description": "Disable an agent so it stops processing messages. Cannot disable the super agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent's ID"
                    }
                },
                "required": ["agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "assign_tools",
            "description": "Assign a set of tools to an agent (replaces existing assignment).",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent's ID"
                    },
                    "tool_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of tool IDs to assign (use list_tools to see available IDs)"
                    }
                },
                "required": ["agent_id", "tool_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_tools",
            "description": "List all available tools on the platform (from registry and skills).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_skill",
            "description": "List, enable, or disable an installed skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "enable", "disable"],
                        "description": "Action to perform"
                    },
                    "skill_id": {
                        "type": "string",
                        "description": "Skill ID (required for enable/disable)"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_skillsets",
            "description": "List all available skillset templates for creating pre-configured agents.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apply_skillset",
            "description": "Create a new agent from a skillset template with pre-configured tools, skills, and prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The skillset template ID to apply (e.g., 'coder', 'devops')"
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID for the new agent (lowercase snake_case: alphanumeric and underscores only)"
                    },
                    "name": {
                        "type": "string",
                        "description": "Display name for the new agent (optional, uses skillset default)"
                    },
                    "description": {
                        "type": "string",
                        "description": "Description for the new agent (optional, uses skillset default)"
                    },
                    "model_id": {
                        "type": "string",
                        "description": "Optional model ID to assign (optional, uses skillset default)"
                    },
                    "workplace_id": {
                        "type": "string",
                        "description": "Optional workplace ID to assign the new agent to (e.g. 'a15117f03576'). Use list_workplaces to discover available workplaces."
                    }
                },
                "required": ["skill_id", "agent_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_owner_name",
            "description": "Save the platform owner's name after learning it during onboarding. Call this once the owner tells you their name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The owner's name"
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "restart",
            "description": "Restart the Evonic server. Only available to the super agent.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "assign_skills",
            "description": "Add skills to an agent (skips skills already assigned). Does not remove existing assignments. Validates agent exists before assigning.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent's ID"
                    },
                    "skill_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of skill IDs to assign (e.g., 'kanban', 'github')"
                    }
                },
                "required": ["agent_id", "skill_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "unassign_skill",
            "description": "Remove a single skill from an agent's assignment list. If the skill was not assigned, returns a clear message (not an error).",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent's ID"
                    },
                    "skill_id": {
                        "type": "string",
                        "description": "The skill ID to remove (e.g., 'kanban')"
                    }
                },
                "required": ["agent_id", "skill_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agent_info",
            "description": "Get comprehensive information about an agent: settings, tools, skills, channels, portals, KB files, artifacts stats, variables, and models.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The target agent's ID (e.g. 'siwa', 'adit', 'angga')"
                    },
                    "section": {
                        "type": "string",
                        "description": "Comma-separated section names to return. Options: info, tools, skills, channels, portals, kb, artifacts, variables, models. Omit or leave empty to return all sections. Example: 'info,channels,artifacts'"
                    }
                },
                "required": ["agent_id"]
            }
        }
    },
]


# ==================== Executors ====================

def _exec_list_agents(args: dict) -> dict:
    agents = db.get_agents()
    result = []
    for a in agents:
        tool_count = len(db.get_agent_tools(a['id']))
        channels = db.get_channels(a['id'])
        result.append({
            'id': a['id'],
            'name': a['name'],
            'description': a.get('description', ''),
            'enabled': bool(a.get('enabled', True)),
            'is_super': bool(a.get('is_super', False)),
            'model_id': a.get('model_id'),
            'tool_count': tool_count,
            'channel_count': len(channels),
            'created_at': a.get('created_at', ''),
        })
    return {'agents': result, 'count': len(result)}


def _exec_create_agent(args: dict) -> dict:
    import re as _re
    agent_id = (args.get('id') or '').strip().lower()
    name = (args.get('name') or '').strip()
    if not agent_id or not _re.match(r'^[a-z0-9_]+$', agent_id):
        return {'error': 'Invalid ID. Use only lowercase alphanumeric characters and underscores (snake_case).'}
    if _re.search(r'_sub_\d+$', agent_id):
        return {'error': 'Agent ID cannot end with a sub-agent pattern (e.g. _sub_1). This naming convention is reserved for internal use.'}
    if not name:
        return {'error': 'Name is required.'}
    if db.get_agent(agent_id):
        return {'error': f"Agent ID '{agent_id}' already exists."}
    try:
        workspace = os.path.join(WORKSPACE_DIR, agent_id)
        _ensure_kb_dir(agent_id)
        os.makedirs(workspace, exist_ok=True)
        db.create_agent({
            'id': agent_id,
            'name': name,
            'description': args.get('description', ''),
            'system_prompt': args.get('system_prompt', ''),
            'model_id': args.get('model_id') or None,
            'workspace': workspace,
        })
        _write_system_prompt(agent_id, args.get('system_prompt', ''))
        # Create notes.md template if it does not already exist
        _notes_md = os.path.join(AGENTS_DIR, agent_id, 'kb', 'notes.md')
        if not os.path.isfile(_notes_md):
            with open(_notes_md, 'w', encoding='utf-8') as _f:
                _f.write(_NOTES_MD_TEMPLATE)
        return {'success': True, 'agent_id': agent_id, 'message': f"Agent '{name}' ({agent_id}) created successfully."}
    except Exception as e:
        return {'error': str(e)}


def _exec_update_agent(args: dict, agent_context: dict = None) -> dict:
    agent_id = (args.get('agent_id') or '').strip()
    if not agent_id:
        return {'error': 'agent_id is required.'}
    agent = db.get_agent(agent_id)
    if not agent:
        return {'error': f"Agent '{agent_id}' not found."}
    if agent.get('is_super') and args.get('enabled') is False:
        return {'error': 'Super agent cannot be disabled.'}

    # Security: only super agents can set/clear run_as_user.
    # Prevent non-super agents from escalating by clearing their own run_as_user.
    if 'run_as_user' in args:
        caller_is_super = (agent_context or {}).get('is_super', False) if agent_context else True
        if not caller_is_super:
            return {'error': 'Only super agents can change run_as_user.'}

    update_data = {k: v for k, v in args.items() if k != 'agent_id'}
    if 'system_prompt' in update_data:
        _write_system_prompt(agent_id, update_data.pop('system_prompt'))
    if 'model_id' in update_data and update_data['model_id'] == '':
        update_data['model_id'] = None  # empty string resets to global default
    if 'messaging_acl' in update_data and isinstance(update_data['messaging_acl'], list):
        import json as _json
        update_data['messaging_acl'] = _json.dumps(update_data['messaging_acl'])
    if update_data:
        db.update_agent(agent_id, update_data)
    return {'success': True, 'message': f"Agent '{agent_id}' updated."}


def _exec_delete_agent(args: dict) -> dict:
    agent_id = (args.get('agent_id') or '').strip()
    if not agent_id:
        return {'error': 'agent_id is required.'}
    agent = db.get_agent(agent_id)
    if not agent:
        return {'error': f"Agent '{agent_id}' not found."}
    if agent.get('is_super'):
        return {'error': 'Super agent cannot be deleted.'}
    try:
        db.delete_agent(agent_id)
        agent_dir = os.path.join(AGENTS_DIR, agent_id)
        if os.path.isdir(agent_dir):
            shutil.rmtree(agent_dir)
        try:
            from models.chat import agent_chat_manager
            agent_chat_manager.drop(agent_id)
        except Exception:
            pass
        return {'success': True, 'message': f"Agent '{agent_id}' deleted."}
    except Exception as e:
        return {'error': str(e)}


def _exec_enable_agent(args: dict) -> dict:
    agent_id = (args.get('agent_id') or '').strip()
    if not agent_id:
        return {'error': 'agent_id is required.'}
    if not db.get_agent(agent_id):
        return {'error': f"Agent '{agent_id}' not found."}
    db.update_agent(agent_id, {'enabled': True})
    return {'success': True, 'message': f"Agent '{agent_id}' enabled."}


def _exec_disable_agent(args: dict) -> dict:
    agent_id = (args.get('agent_id') or '').strip()
    if not agent_id:
        return {'error': 'agent_id is required.'}
    agent = db.get_agent(agent_id)
    if not agent:
        return {'error': f"Agent '{agent_id}' not found."}
    if agent.get('is_super'):
        return {'error': 'Super agent cannot be disabled.'}
    db.update_agent(agent_id, {'enabled': False})
    return {'success': True, 'message': f"Agent '{agent_id}' disabled."}


def _exec_assign_tools(args: dict) -> dict:
    agent_id = (args.get('agent_id') or '').strip()
    tool_ids = args.get('tool_ids', [])
    if not agent_id:
        return {'error': 'agent_id is required.'}
    if not db.get_agent(agent_id):
        return {'error': f"Agent '{agent_id}' not found."}
    if not isinstance(tool_ids, list):
        return {'error': 'tool_ids must be a list.'}
    db.set_agent_tools(agent_id, tool_ids)
    return {'success': True, 'message': f"Assigned {len(tool_ids)} tool(s) to agent '{agent_id}'."}


def _exec_list_tools(args: dict) -> dict:
    from backend.tools import tool_registry
    all_defs = tool_registry.get_all_tool_defs()
    result = []
    for td in all_defs:
        fn = td.get('function', {})
        result.append({
            'id': td.get('id', fn.get('name', '')),
            'name': fn.get('name', ''),
            'description': fn.get('description', ''),
        })
    return {'tools': result, 'count': len(result)}


def _exec_manage_skill(args: dict) -> dict:
    from backend.skills_manager import skills_manager
    action = args.get('action', '')
    skill_id = args.get('skill_id', '')
    if action == 'list':
        skills = skills_manager.list_skills()
        return {'skills': [
            {'id': s['id'], 'name': s.get('name', s['id']),
             'enabled': s.get('enabled', True), 'version': s.get('version', '')}
            for s in skills
        ]}
    if not skill_id:
        return {'error': 'skill_id is required for enable/disable.'}
    if action == 'enable':
        skills_manager.set_skill_enabled(skill_id, True)
        return {'success': True, 'message': f"Skill '{skill_id}' enabled."}
    if action == 'disable':
        skills_manager.set_skill_enabled(skill_id, False)
        return {'success': True, 'message': f"Skill '{skill_id}' disabled."}
    return {'error': f"Unknown action '{action}'. Use: list, enable, disable."}


def _exec_list_skillsets(args: dict) -> dict:
    from backend.skillsets import list_skillsets as ls
    skillsets = ls()
    return {'skillsets': skillsets, 'count': len(skillsets)}


def _exec_apply_skillset(args: dict) -> dict:
    import re as _re
    from backend.skillsets import apply_skillset as apply_ss
    from backend.skills_manager import skills_manager

    skill_id = (args.get('skill_id') or '').strip()
    agent_id = (args.get('agent_id') or '').strip().lower()
    if not skill_id:
        return {'error': 'skill_id is required.'}
    if not agent_id or not _re.match(r'^[a-z0-9_]+$', agent_id):
        return {'error': 'Invalid agent_id. Use only lowercase alphanumeric characters and underscores (snake_case).'}

    if _re.search(r'_sub_\d+$', agent_id):
        return {'error': 'Agent ID cannot end with a sub-agent pattern (e.g. _sub_1). This naming convention is reserved for internal use.'}

    if db.get_agent(agent_id):
        return {'error': f"Agent ID '{agent_id}' already exists."}

    agent_data = {
        'id': agent_id,
        'name': args.get('name', ''),
        'description': args.get('description', ''),
        'model_id': args.get('model_id', ''),
    }

    result = apply_ss(skill_id, agent_data)
    if 'error' in result:
        return result

    try:
        agents_dir = AGENTS_DIR
        agent_dir = os.path.join(agents_dir, agent_id)
        kb_dir = os.path.join(agent_dir, 'kb')
        os.makedirs(kb_dir, exist_ok=True)

        system_prompt_path = os.path.join(agent_dir, 'SYSTEM.md')
        with open(system_prompt_path, 'w', encoding='utf-8') as f:
            f.write(result.get('system_prompt', ''))

        # Create workspace directory at shared/agents/[agent-id]
        workspace_dir = os.path.join(WORKSPACE_DIR, agent_id)
        os.makedirs(workspace_dir, exist_ok=True)

        create_data = {
            'id': agent_id,
            'name': result.get('name', ''),
            'description': result.get('description', ''),
            'system_prompt': result.get('system_prompt', ''),
            'model_id': result.get('model'),
            'workspace': workspace_dir,
        }
        workplace_id = args.get('workplace_id', '').strip()
        if workplace_id:
            create_data['workplace_id'] = workplace_id
        db.create_agent(create_data)

        tools = result.get('tools', [])
        if tools:
            db.set_agent_tools(agent_id, tools)

        for skill_name in result.get('skills', []):
            skills_manager.set_skill_enabled(skill_name, True)

        for fname, content in result.get('kb_files', {}).items():
            kb_file_path = os.path.join(kb_dir, fname)
            with open(kb_file_path, 'w', encoding='utf-8') as f:
                f.write(content)

        # Create notes.md template if it does not already exist
        _notes_md = os.path.join(kb_dir, 'notes.md')
        if not os.path.isfile(_notes_md):
            with open(_notes_md, 'w', encoding='utf-8') as _f:
                _f.write(_NOTES_MD_TEMPLATE)

        return {
            'success': True,
            'agent_id': agent_id,
            'message': f"Agent '{result.get('name', agent_id)}' created from skillset '{skill_id}'."
        }
    except Exception as e:
        return {'error': str(e)}


def _exec_set_owner_name(args: dict) -> dict:
    name = (args.get('name') or '').strip()
    if not name:
        return {'error': 'Name is required.'}
    db.set_setting('owner_name', name)

    # Copy defaults/super_agent_system_prompt.md → agents/<id>/SYSTEM.md
    agent_id = db.get_setting('super_agent_id')
    if agent_id:
        default_path = os.path.join(BASE_DIR, 'defaults', 'super_agent_system_prompt.md')
        if os.path.isfile(default_path):
            try:
                with open(default_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                _write_system_prompt(agent_id, content)
            except Exception:
                pass

    return {'success': True, 'message': f"Owner name saved: {name}"}


def _exec_restart(args: dict, agent_context: dict = None) -> dict:
    """Restart the Evonic server. Same mechanism as /restart slash command."""
    import json as _json
    import logging

    _logger = logging.getLogger(__name__)

    if agent_context:
        agent_id = agent_context.get('id')
        channel_id = agent_context.get('channel_id')
        external_user_id = agent_context.get('user_id')
        session_id = agent_context.get('session_id')
    else:
        agent_id = channel_id = external_user_id = session_id = None

    if agent_context and agent_context.get('restart_origin'):
        return {
            'error': 'Restart is disabled for restart-origin turns.',
            'blocked': True,
        }

    # ── Inter-agent restart guard ──────────────────────────────────────
    # When a regular agent messages the super agent asking for a restart,
    # the super agent should NOT auto-execute it — require user approval first.
    # The approval flow in llm_loop.py handles the rest: when user approves,
    # it sets agent_context['_skip_safety'] = True and re-calls us.
    _is_inter_agent = bool(
        external_user_id
        and external_user_id.startswith('__agent__')
    )
    _skip_safety = agent_context.get('_skip_safety', False) if agent_context else False

    if _is_inter_agent and not _skip_safety:
        # Resolve the requesting agent's name for the approval prompt
        _requester_name = external_user_id
        _requester_id = external_user_id[len('__agent__'):] if _is_inter_agent else ''
        try:
            _req_agent = db.get_agent(_requester_id)
            if _req_agent:
                _requester_name = _req_agent.get('name', _requester_id)
        except Exception:
            pass

        _logger.info(
            'restart: inter-agent request from %s (%s) — requiring user approval',
            _requester_name, _requester_id
        )
        return {
            'level': 'requires_approval',
            'score': 10,
            'reasons': [
                f'Server restart requested by another agent ({_requester_name}) '
                f'via inter-agent messaging. This requires human approval.'
            ],
            'blocked_patterns': ['inter_agent_restart'],
            'requires_approval': True,
            'approval_info': {
                'risk_level': 'high',
                'description': (
                    f'Agent "{_requester_name}" ({_requester_id}) has requested '
                    f'a server restart via agent-to-agent messaging. '
                    f'This will shut down the entire Evonic platform.'
                ),
                'initiated_by': _requester_name,
                'initiated_by_id': _requester_id,
            },
        }

    # Persist routing only. The existing session history remains available after
    # boot, so replaying it inside a new user message is both redundant and unsafe.
    db.set_setting('restart_greeting_needed', _json.dumps({
        'channel_id': channel_id,
        'external_user_id': external_user_id,
        'session_id': session_id,
    }))

    from backend.restart import restart_service
    restart_service()
    return {'result': 'Restarting...'}


def _sync_skill_tools(agent_id: str, final_skill_ids: list) -> dict:
    """Sync skills and auto-manage tools for the given agent.

    For each newly-added non-lazy skill, auto-assign its tools as
    ``skill:<skill_id>:<fn_name>``.  For each removed skill, strip all
    ``skill:<skill_id>:*`` tool IDs from the agent's tool list.
    Persists both the final skill list and updated tool list.

    Returns a dict with:
      - added_skills, removed_skills (list[str])
      - tools_added, tools_removed (int)
      - final_skills, final_tools (list[str])
    """
    from backend.skills_manager import skills_manager

    current_skills = db.get_agent_skills(agent_id)
    current_tools = db.get_agent_tools(agent_id)

    added = [s for s in final_skill_ids if s not in current_skills]
    removed = [s for s in current_skills if s not in final_skill_ids]

    tools_changed = False

    # ---- auto-assign tools for newly-added non-lazy skills ----
    total_added = 0
    for skill_id in added:
        skill = skills_manager.get_skill(skill_id)
        if not skill:
            continue
        if skill.get('lazy_tools', False):
            continue
        # Use _load_tool_defs with _dir to bypass is_skill_enabled guard.
        # Admin explicitly assigning a skill wants its tools regardless of
        # global enable/disable status.  Fall back to get_skill_tool_defs
        # when _dir is missing (e.g. mocked skills in tests).
        skill_dir = skill.get('_dir', '')
        if skill_dir:
            tool_defs = skills_manager._load_tool_defs(skill_dir, skill)
        else:
            tool_defs = skills_manager.get_skill_tool_defs(skill_id)
        for td in tool_defs:
            fn_name = td['function']['name'] if isinstance(td, dict) and 'function' in td else td.get('name', '')
            tool_id = f"skill:{skill_id}:{fn_name}" if fn_name else ''
            if tool_id and tool_id not in current_tools:
                current_tools.append(tool_id)
                total_added += 1
    if total_added > 0:
        tools_changed = True

    # ---- strip tools for removed skills ----
    total_removed = 0
    if removed:
        before = len(current_tools)
        current_tools = [t for t in current_tools
                         if not any(t.startswith(f"skill:{s}:") for s in removed)]
        total_removed = before - len(current_tools)
        if total_removed > 0:
            tools_changed = True

    # ---- persist ----
    db.set_agent_skills(agent_id, final_skill_ids)
    if tools_changed:
        db.set_agent_tools(agent_id, current_tools)

    return {
        'added_skills': added,
        'removed_skills': removed,
        'tools_added': total_added,
        'tools_removed': total_removed,
        'final_skills': final_skill_ids,
        'final_tools': current_tools,
    }


def _exec_assign_skills(args: dict) -> dict:
    agent_id = (args.get('agent_id') or '').strip()
    skill_ids = args.get('skill_ids', [])
    if not agent_id:
        return {'error': 'agent_id is required.'}
    if not db.get_agent(agent_id):
        return {'error': f"Agent '{agent_id}' not found."}
    if not isinstance(skill_ids, list):
        return {'error': 'skill_ids must be a list.'}
    current_skills = db.get_agent_skills(agent_id)
    new_skills = [s for s in skill_ids if s not in current_skills]
    if not new_skills:
        return {'message': f"All {len(skill_ids)} skill(s) are already assigned to agent '{agent_id}'. No changes made."}
    merged = current_skills + new_skills

    result = _sync_skill_tools(agent_id, merged)

    msg = f"Added {len(new_skills)} new skill(s) to agent '{agent_id}' ({len(skill_ids)} requested, {len(skill_ids) - len(new_skills)} already assigned). Total: {len(merged)} skill(s)."
    if result['tools_added'] > 0:
        msg += f" Auto-assigned {result['tools_added']} tool(s) from non-lazy skills."
    return {'success': True, 'message': msg}


def _exec_unassign_skill(args: dict) -> dict:
    agent_id = (args.get('agent_id') or '').strip()
    skill_id = (args.get('skill_id') or '').strip()
    if not agent_id:
        return {'error': 'agent_id is required.'}
    if not skill_id:
        return {'error': 'skill_id is required.'}
    if not db.get_agent(agent_id):
        return {'error': f"Agent '{agent_id}' not found."}
    current_skills = db.get_agent_skills(agent_id)
    if skill_id not in current_skills:
        return {'message': f"Skill '{skill_id}' was not assigned to agent '{agent_id}'. No changes made."}
    updated_skills = [s for s in current_skills if s != skill_id]

    result = _sync_skill_tools(agent_id, updated_skills)

    msg = f"Removed skill '{skill_id}' from agent '{agent_id}'. {len(updated_skills)} skill(s) remaining."
    if result['tools_removed'] > 0:
        msg += f" Cleaned up {result['tools_removed']} orphaned tool(s)."
    return {'success': True, 'message': msg}


def _exec_agent_info(args: dict) -> dict:
    from backend.tools.agent_info import execute as _agent_info_exec
    return _agent_info_exec({}, args)


# ==================== Registry-style access ====================

_EXECUTORS: Dict[str, Callable] = {
    'list_agents': _exec_list_agents,
    'create_agent': _exec_create_agent,
    'update_agent': _exec_update_agent,
    'delete_agent': _exec_delete_agent,
    'enable_agent': _exec_enable_agent,
    'disable_agent': _exec_disable_agent,
    'assign_tools': _exec_assign_tools,
    'list_tools': _exec_list_tools,
    'manage_skill': _exec_manage_skill,
    'list_skillsets': _exec_list_skillsets,
    'apply_skillset': _exec_apply_skillset,
    'set_owner_name': _exec_set_owner_name,
    'restart': _exec_restart,
    'assign_skills': _exec_assign_skills,
    'unassign_skill': _exec_unassign_skill,
    'agent_info': _exec_agent_info,
}


def get_super_agent_tool_defs() -> List[Dict[str, Any]]:
    """Return OpenAI-format tool definitions for super agent tools."""
    return list(_TOOL_DEFS)


def get_super_agent_executor(agent_context: dict) -> Callable:
    """Return an executor callable for super agent tools."""
    def executor(fn_name: str, args: dict):
        if fn_name in _EXECUTORS:
            try:
                return _EXECUTORS[fn_name](args, agent_context=agent_context)
            except TypeError:
                # Executor doesn't accept agent_context — call without it
                try:
                    return _EXECUTORS[fn_name](args)
                except Exception as e:
                    return {'error': f"Super agent tool error: {str(e)}"}
            except Exception as e:
                return {'error': f"Super agent tool error: {str(e)}"}
        return None  # not a super agent tool — fall through
    return executor

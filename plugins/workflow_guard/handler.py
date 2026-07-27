from __future__ import annotations

import os

from backend.plugin_manager import (
    register_tool_guard, register_tool_result_gate, register_turn_gate,
    unregister_tool_guard, unregister_tool_result_gate, unregister_turn_gate,
)
from .engine import WorkflowGuard

PLUGIN_DIR = os.path.dirname(__file__)
_guard = None
_repo = None
_sdk = None


def turn_gate(context):
    return _guard.turn_gate(context) if _guard else None


def tool_guard(agent_id, tool_name, args, context=None):
    return _guard.tool_guard(agent_id, tool_name, args, context or {}) if _guard else None


def tool_result_gate(context, tool_name, args, result):
    return _guard.tool_result_gate(context, tool_name, args, result) if _guard else None


def on_enable(sdk=None):
    global _sdk, _guard, _repo
    _sdk = sdk
    config = sdk.config if sdk else {}
    log = sdk.log if sdk else None
    _guard = WorkflowGuard(PLUGIN_DIR, config, log)
    _repo = _guard.repo
    register_turn_gate(turn_gate)
    register_tool_guard(tool_guard)
    register_tool_result_gate(tool_result_gate)
    _guard.start_worker()


def on_disable(sdk=None):
    unregister_turn_gate(turn_gate)
    unregister_tool_guard(tool_guard)
    unregister_tool_result_gate(tool_result_gate)
    if _guard:
        _guard.stop_worker()


def repository():
    return _repo

"""Plugin lifecycle: register the quota hooks and start the workers.

Enabling the plugin registers a turn gate (hard cap / suspended answers
without calling the LLM), a message interceptor (soft-cap warning) and a
tool-result gate (a turn that runs past its quota mid-loop ends at the next
tool result). Two daemon threads run the WhatsApp pairing poll and the
callback outbox. Disabling the plugin removes all of it.
"""
from __future__ import annotations

import logging

from backend.plugin_manager import (
    register_message_interceptor, register_tool_result_gate, register_turn_gate,
    unregister_message_interceptor, unregister_tool_result_gate, unregister_turn_gate,
)

from . import quota
from .callbacks import CallbackWorker
from .whatsapp import PairingWorker

_logger = logging.getLogger(__name__)

_pairing = PairingWorker()
_callbacks = CallbackWorker()


def turn_gate(context):
    return quota.turn_gate(context)


def message_interceptor(agent_id, content, messages):
    return quota.message_interceptor(agent_id, content, messages)


def tool_result_gate(context, tool_name, args, result):
    return quota.tool_result_gate(context, tool_name, args, result)


def on_enable(sdk=None):
    register_turn_gate(turn_gate)
    register_message_interceptor(message_interceptor)
    register_tool_result_gate(tool_result_gate)
    _pairing.start()
    _callbacks.start()
    _logger.info("TiyasanCloud plugin enabled")


def on_disable(sdk=None):
    unregister_turn_gate(turn_gate)
    unregister_message_interceptor(message_interceptor)
    unregister_tool_result_gate(tool_result_gate)
    _pairing.stop()
    _callbacks.stop()
    _logger.info("TiyasanCloud plugin disabled")

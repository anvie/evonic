"""HTTP API for the TiyasanCloud console.

Every route lives under ``/plugin/tiyasancloud/v1`` (and the ``/plugin/tiyasan/v1``
alias) and needs the shared secret. Evonic exempts ``/plugin/*`` from its
session and CSRF checks, so this blueprint authenticates every request itself.

The plugin registers agents; it does not list, read, change or delete them.

    POST   /register_agent                          new agent + WhatsApp link, one transaction
    GET    /token_usage?agent_id=&from=&to=          tokens used (default: the current period)
    POST   /agents/<agent_id>/whatsapp/link          fresh "chat on WhatsApp" link
    GET    /agents/<agent_id>/whatsapp               linked numbers + channel status
    DELETE /agents/<agent_id>/whatsapp/users/<phone>
    GET    /health                                   plugin + shared channel state
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Tuple

from flask import Blueprint, jsonify, request

from .common import ApiError, API_PREFIXES, PLUGIN_ID, authorized, config, invalid_body, iso, now, parse_time
from . import agents, quota, store, whatsapp

_logger = logging.getLogger(__name__)


def _body() -> Dict[str, Any]:
    if not request.data:
        return {}
    payload = request.get_json(silent=True)
    if payload is None:
        raise invalid_body("body must be valid JSON")
    if not isinstance(payload, dict):
        raise invalid_body("body must be a JSON object")
    return payload


def _fail(exc: ApiError):
    body, status = exc.response()
    return jsonify(body), status


def create_blueprint():
    bp = Blueprint(PLUGIN_ID, __name__)

    def route(rule: str, **options) -> Callable:
        """Register one handler under both API prefixes."""
        def decorator(fn: Callable) -> Callable:
            for index, prefix in enumerate(API_PREFIXES):
                bp.add_url_rule(prefix + rule, endpoint=f"{fn.__name__}_{index}",
                                view_func=fn, **options)
            return fn
        return decorator

    @bp.before_request
    def require_token():
        if not authorized(request):
            return jsonify({"error": "unauthorized", "message": "missing or wrong access token"}), 401
        return None

    @bp.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):
        return _fail(exc)

    # --------------------------------------------------------------- register

    @route("/register_agent", methods=["POST"])
    def register_agent():
        """Create the agent and its WhatsApp link in one call.

        201 with the new agent; 200 with ``error: "agent_exists"`` when it is
        already there (nothing is changed, no new link is made).
        """
        try:
            body = _body()
            agent_id = agents.require_managed(body.get("agent_id"))
            limits = None
            if body.get("limits") is not None:
                if not isinstance(body["limits"], dict):
                    raise invalid_body("'limits' must be an object")
                limits = quota.parse_limits(body["limits"], body.get("locale") or "en")
            result, created = agents.register(agent_id, body)
            if not created:
                return jsonify({**result, "error": "agent_exists", "whatsapp": None}), 200
            if limits is not None:
                quota.save(agent_id, limits)
            # The customer never types a number: the link opens WhatsApp with a
            # ready-made message to the shared number.
            return jsonify({**result, "whatsapp": _link_for(agent_id)}), 201
        except ApiError as exc:
            return _fail(exc)

    def _link_for(agent_id: str) -> Dict[str, Any]:
        """WhatsApp block of the registration; a WhatsApp outage never fails it."""
        try:
            return {"link": whatsapp.chat_link(agent_id, reuse=True)}
        except ApiError as exc:
            _logger.info("TiyasanCloud WhatsApp link skipped for %s: %s", agent_id, exc.code)
            return {"link": None, "error": exc.code}

    # ------------------------------------------------------------------ quota

    @route("/token_usage", methods=["GET"])
    def token_usage():
        try:
            agent_id = agents.require_managed(request.args.get("agent_id"))
            since = request.args.get("from") or None
            until = request.args.get("to") or None
            for value, field in ((since, "from"), (until, "to")):
                if value:
                    parse_time(value, field)
            usage = quota.usage_view(agent_id, since, until)
            return jsonify({"agent_id": agent_id, **{k: v for k, v in usage.items() if k != "summary"}})
        except ValueError as exc:
            return _fail(invalid_body(str(exc)))
        except ApiError as exc:
            return _fail(exc)

    # --------------------------------------------------------------- whatsapp

    @route("/agents/<agent_id>/whatsapp/link", methods=["POST"])
    def post_whatsapp_link(agent_id: str):
        try:
            return jsonify(whatsapp.chat_link(agents.require_managed(agent_id)))
        except ApiError as exc:
            return _fail(exc)

    @route("/agents/<agent_id>/whatsapp", methods=["GET"])
    def get_whatsapp(agent_id: str):
        try:
            return jsonify(whatsapp.status(agents.require_managed(agent_id)))
        except ApiError as exc:
            return _fail(exc)

    @route("/agents/<agent_id>/whatsapp/users/<phone>", methods=["DELETE"])
    def delete_linked_user(agent_id: str, phone: str):
        try:
            whatsapp.unlink(agents.require_managed(agent_id), phone)
            return "", 204
        except ApiError as exc:
            return _fail(exc)

    # ----------------------------------------------------------------- health

    @route("/health", methods=["GET"])
    def health():
        settings = config()
        return jsonify({
            "plugin": PLUGIN_ID,
            "time": iso(now()),
            "shared_channels": [
                {"id": row["id"], "name": row.get("name"), "number": whatsapp.channel_number(row["id"]),
                 "status": whatsapp.channel_status(row["id"])}
                for row in whatsapp.shared_channels()
            ],
            "quota_enforced": bool(settings.get("ENFORCE_QUOTA")),
            "callbacks_pending": len(store.due_callbacks(limit=100)),
        })

    return bp

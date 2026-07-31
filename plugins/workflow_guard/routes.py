from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from .handler import repository


def _authorized():
    return bool(session.get("authenticated"))


def create_blueprint(sdk=None):
    bp = Blueprint("workflow_guard", __name__)

    @bp.get("/workflow-guard")
    def dashboard():
        if not _authorized():
            return jsonify({"error": "unauthorized"}), 401
        repo = repository()
        return jsonify({"subjects": repo.list_subjects(), "outbox": repo.list_outbox()})

    @bp.post("/api/workflow-guard/subjects/<subject_id>/reopen")
    def reopen(subject_id):
        if not _authorized():
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        actor = str(session.get("user_id") or session.get("username") or "authenticated-staff")
        reason = str(data.get("reason") or "").strip()
        if not reason:
            return jsonify({"error": "reason is required"}), 400
        try:
            return jsonify(repository().reopen(subject_id, actor, reason))
        except KeyError:
            return jsonify({"error": "not found"}), 404

    return bp

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .classifier import classify
from .repository import Repository


class WorkflowGuard:
    def __init__(self, plugin_dir: str, config: dict, log=None):
        self.plugin_dir, self.config, self.log = plugin_dir, config, log or (lambda *_a, **_k: None)
        data_dir = os.environ.get("EVONIC_PLUGIN_DATA_DIR") or os.path.join(plugin_dir, "data")
        self.repo = Repository(os.path.join(data_dir, "workflow_guard.db"))
        with open(os.path.join(plugin_dir, "policies.json"), encoding="utf-8") as handle:
            policies = json.load(handle)
        self.policies = {p["agent_id"]: p for p in policies if p.get("enabled", True)}
        self.secret = self._secret()
        self._stop = threading.Event()
        self._worker = None

    def _secret(self) -> bytes:
        path = os.path.join(os.path.dirname(self.repo.path), ".identity-key")
        try:
            with open(path, "rb") as handle:
                value = handle.read()
            if len(value) >= 32:
                return value
        except OSError:
            pass
        value = os.urandom(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
        return value

    @staticmethod
    def _bool(value, default=False):
        if value is None:
            return default
        return value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}

    def enabled(self):
        return self._bool(self.config.get("ENFORCEMENT_ENABLED"), True)

    def shadow(self):
        return self._bool(self.config.get("SHADOW_MODE"), False)

    def policy(self, context):
        return self.policies.get(str(context.get("agent_id") or ""))

    def identity(self, context):
        external = str(context.get("external_user_id") or "").strip()
        channel = str(context.get("channel_id") or "web").strip()
        if not external:
            return None
        canonical = f"{channel}\x1f{external}".encode()
        digest = hmac.new(self.secret, canonical, hashlib.sha256).hexdigest()
        visible = external[-4:] if len(external) >= 4 else "hidden"
        return digest, f"***{visible}"

    @staticmethod
    def _image_attachment_ids(context):
        attachment_ids = list(context.get("attachment_ids") or [])
        mime_types = list(context.get("attachment_mime_types") or [])
        return [
            str(attachment_id)
            for index, attachment_id in enumerate(attachment_ids)
            if index < len(mime_types) and str(mime_types[index]).lower().startswith("image/")
        ]

    def turn_gate(self, context):
        policy, identity = self.policy(context), self.identity(context)
        if not policy or not identity or not self.enabled() or self.shadow():
            return None
        subject = self.repo.subject(policy, identity[0], identity[1], create=False)
        if subject and subject["status"] == "locked":
            self.log(f"blocked_turn policy={policy['policy_id']} subject={subject['id']}")
            return {"handled": True, "response": policy["fixed_response"],
                    "suppress_intermediate": True}
        decision = {"suppress_intermediate": bool(policy.get("suppress_intermediate", True))}
        required_tool = str(policy.get("image_attachment_required_tool") or "").strip()
        image_attachment_ids = self._image_attachment_ids(context)
        if required_tool and image_attachment_ids:
            decision["required_tool"] = required_tool
            self.log(
                f"required_tool policy={policy['policy_id']} tool={required_tool} "
                f"attachment_count={len(image_attachment_ids)}"
            )
        return decision

    def tool_guard(self, agent_id, tool_name, _args, context):
        policy, identity = self.policy({**(context or {}), "agent_id": agent_id}), self.identity(context or {})
        if not policy or tool_name not in policy.get("mutating_tools", []) or not identity or not self.enabled() or self.shadow():
            return None
        subject = self.repo.subject(policy, identity[0], identity[1], create=False)
        if subject and subject["status"] == "locked":
            self.log(f"blocked_tool policy={policy['policy_id']} tool={tool_name} subject={subject['id']}")
            return {"block": True, "error": "Workflow subject is locked."}
        return None

    def tool_result_gate(self, context, tool_name, args, result):
        policy, identity = self.policy(context), self.identity(context)
        if not policy or tool_name not in policy.get("monitored_tools", []) or not identity:
            return None
        outcome = classify(policy, tool_name, result)
        if outcome["action"] == "success":
            self.repo.mark_submitted(policy["policy_id"], identity[0])
            return None
        if outcome["action"] != "count":
            return None
        attempt = self.attempt_key(context, tool_name, args, result, outcome)
        decision = self.repo.record_failure(policy, identity[0], identity[1], attempt,
                                            outcome["stage"], outcome["reason_code"],
                                            shadow=self.shadow() or not self.enabled())
        self.log(f"failure policy={policy['policy_id']} count={decision['count']} duplicate={decision['duplicate']}")
        if decision["locked"] and self.enabled() and not self.shadow():
            self.log(f"threshold_lock policy={policy['policy_id']} subject={decision.get('subject_id')}")
            return {"terminate_turn": True, "response": policy["fixed_response"]}
        return None

    def attempt_key(self, context, tool_name, args, result, outcome):
        message_id = str(context.get("message_id") or "")
        attachment_ids = ",".join(sorted(str(v) for v in context.get("attachment_ids") or []))
        fingerprint = str(result.get("file_fingerprint") or "") if isinstance(result, dict) else ""
        if not message_id:
            message_id = f"runtime:{context.get('session_id') or ''}:{context.get('turn_index') or ''}"
        payload = "\x1f".join([
            str(context.get("channel_id") or "web"),
            str(context.get("external_user_id") or ""), message_id,
            attachment_ids, tool_name, outcome.get("stage", ""), fingerprint,
        ]).encode()
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def start_worker(self):
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._work, name="workflow-guard-outbox", daemon=True)
        self._worker.start()

    def stop_worker(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2)

    def _work(self):
        interval = max(5, int(self.config.get("OUTBOX_INTERVAL_SECONDS") or 30))
        while not self._stop.wait(interval):
            for row in self.repo.pending_outbox():
                self._deliver(row)

    def _deliver(self, row):
        url = str(self.config.get("ESCALATION_WEBHOOK_URL") or "").strip()
        if not url:
            return
        request = Request(url, data=row["payload_json"].encode(), method="POST", headers={
            "Content-Type": "application/json", "Idempotency-Key": row["idempotency_key"]})
        try:
            with urlopen(request, timeout=15) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("HTTP_ERROR")
            self.repo.delivery_result(row["id"], True)
            self.log(f"report_delivered outbox={row['id']}")
        except (HTTPError, URLError, TimeoutError, RuntimeError):
            retries = row["retry_count"] + 1
            retry_at = (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** min(retries, 10)))).isoformat()
            self.repo.delivery_result(row["id"], False, "DELIVERY_FAILED", retry_at)
            self.log(f"report_retry outbox={row['id']} retry={retries}")

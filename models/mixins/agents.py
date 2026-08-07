import sqlite3
from typing import Dict, Any, List, Optional

from models.boolean import normalize_bool


class AgentMixin:
    """Agent CRUD and agent-tool/skill mapping. Requires self._connect() from the host class."""

    def get_agents(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, description, system_prompt, vision_enabled, created_at, updated_at, summarize_threshold, summarize_tail, summarize_prompt, message_buffer_seconds, inject_agent_id, inject_datetime, send_intermediate_responses, outbound_buffer_seconds, enable_agent_state, workspace, is_super, enabled, default_model_id, sandbox_enabled, attachments_enabled, attachment_max_size_mb, send_file_allowed_path_regex, audio_enabled, video_enabled, artifacts_enabled, last_active_at, safety_checker_enabled, bash_exec_enabled, primary_channel_id, avatar_path, disable_parallel_tool_execution, disable_turn_prefetch, agent_messaging_enabled, session_count, fallback_model_id, model_id, tool_compression_enabled, message_wrapper_enabled, run_as_user, vision_model_id, inter_agent_clear_context, builtin_tools_enabled, workplace_id, messaging_acl, messaging_acl_mode, memory_engine, kb_organizer_mode, enable_atg, enable_cmp, always_execute, hidden_slash_commands, disabled_slash_commands FROM agents ORDER BY last_active_at DESC NULLS LAST, name")
            return [dict(row) for row in cursor.fetchall()]

    def get_enabled_agents_sorted(self, limit: int = None) -> List[Dict[str, Any]]:
        """Enabled agents sorted by last_active_at DESC, then by name.
        Filters and sorts in SQL instead of Python for performance."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            sql = """SELECT id, name, description, system_prompt, vision_enabled, created_at, updated_at, summarize_threshold, summarize_tail, summarize_prompt, message_buffer_seconds, inject_agent_id, inject_datetime, send_intermediate_responses, outbound_buffer_seconds, enable_agent_state, workspace, is_super, enabled, default_model_id, sandbox_enabled, attachments_enabled, attachment_max_size_mb, send_file_allowed_path_regex, audio_enabled, video_enabled, artifacts_enabled, last_active_at, safety_checker_enabled, bash_exec_enabled, primary_channel_id, avatar_path, disable_parallel_tool_execution, disable_turn_prefetch, agent_messaging_enabled, session_count, fallback_model_id, model_id, tool_compression_enabled, message_wrapper_enabled, run_as_user, vision_model_id, inter_agent_clear_context, builtin_tools_enabled, workplace_id, messaging_acl, messaging_acl_mode, memory_engine, kb_organizer_mode, enable_atg, enable_cmp, always_execute, hidden_slash_commands, disabled_slash_commands FROM agents
                     WHERE enabled = 1
                     ORDER BY last_active_at DESC NULLS LAST, name"""
            if limit is not None:
                sql += " LIMIT ?"
                return [dict(row) for row in conn.execute(sql, (limit,))]
            return [dict(row) for row in conn.execute(sql)]

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, description, system_prompt, vision_enabled, created_at, updated_at, summarize_threshold, summarize_tail, summarize_prompt, message_buffer_seconds, inject_agent_id, inject_datetime, send_intermediate_responses, outbound_buffer_seconds, enable_agent_state, workspace, is_super, enabled, default_model_id, sandbox_enabled, attachments_enabled, attachment_max_size_mb, send_file_allowed_path_regex, audio_enabled, video_enabled, artifacts_enabled, last_active_at, safety_checker_enabled, bash_exec_enabled, primary_channel_id, avatar_path, disable_parallel_tool_execution, disable_turn_prefetch, agent_messaging_enabled, session_count, fallback_model_id, model_id, tool_compression_enabled, message_wrapper_enabled, run_as_user, vision_model_id, inter_agent_clear_context, builtin_tools_enabled, workplace_id, messaging_acl, messaging_acl_mode, memory_engine, kb_organizer_mode, enable_atg, enable_cmp, always_execute, dm_only, hidden_slash_commands, disabled_slash_commands FROM agents WHERE id = ?", (agent_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_agent(self, agent: Dict[str, Any]) -> str:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO agents (id, name, description, system_prompt, is_super, enabled,
                    vision_enabled, inject_agent_id, inject_datetime, send_intermediate_responses, enable_agent_state,
                    workspace, agent_messaging_enabled, sandbox_enabled, summarize_tail, artifacts_enabled,
                    message_wrapper_enabled, fallback_model_id, model_id, audio_enabled, video_enabled,
                    run_as_user, bash_exec_enabled, vision_model_id, inter_agent_clear_context,
                    builtin_tools_enabled, messaging_acl, messaging_acl_mode, workplace_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent['id'], agent.get('name', agent['id']),
                agent.get('description', ''), agent.get('system_prompt', ''),
                1 if agent.get('is_super') else 0,
                0 if agent.get('enabled') is False else 1,
                1,  # vision_enabled
                1,  # inject_agent_id
                1,  # inject_datetime
                1,  # send_intermediate_responses
                1,  # enable_agent_state
                agent.get('workspace'),
                1 if agent.get('agent_messaging_enabled') is not False else 0,
                1 if agent.get('sandbox_enabled') else 0,
                agent.get('summarize_tail', 5),
                1 if agent.get('artifacts_enabled') is not False else 0,
                1 if normalize_bool(agent.get('message_wrapper_enabled'), True) else 0,
                agent.get('fallback_model_id'),
                agent.get('model_id'),
                1 if agent.get('audio_enabled') else 0,
                1 if agent.get('video_enabled') else 0,
                agent.get('run_as_user'),
                # Default ON for the super agent, OFF for regular agents.
                # Honors an explicit value when present (e.g. cloning).
                1 if agent.get('bash_exec_enabled', agent.get('is_super')) else 0,
                agent.get('vision_model_id'),
                1 if agent.get('inter_agent_clear_context') else 0,
                1 if agent.get('builtin_tools_enabled', True) else 0,
                agent.get('messaging_acl'),
                agent.get('messaging_acl_mode', 'whitelist'),
                agent.get('workplace_id'),
            ))
            conn.commit()
        return agent['id']

    def update_agent(self, agent_id: str, data: Dict[str, Any]) -> bool:
        allowed = {'name', 'description', 'vision_enabled',
                   'summarize_threshold', 'summarize_tail', 'summarize_prompt',
                   'message_buffer_seconds', 'inject_agent_id', 'inject_datetime',
                   'send_intermediate_responses', 'outbound_buffer_seconds', 'message_wrapper_enabled', 'enable_agent_state', 'workspace',
                   'enabled', 'is_super', 'sandbox_enabled', 'safety_checker_enabled', 'primary_channel_id',
                   'avatar_path', 'disable_parallel_tool_execution', 'disable_turn_prefetch',
                   'agent_messaging_enabled', 'workplace_id',
                   'attachments_enabled', 'attachment_max_size_mb', 'send_file_allowed_path_regex', 'artifacts_enabled',
                   'fallback_model_id', 'audio_enabled', 'video_enabled',
                   'model_id',
                   'run_as_user', 'bash_exec_enabled', 'vision_model_id',
                   'inter_agent_clear_context', 'builtin_tools_enabled', 'tool_compression_enabled',
                   'messaging_acl', 'messaging_acl_mode',
                   'memory_engine', 'kb_organizer_mode', 'enable_atg', 'enable_cmp', 'always_execute',
                   'dm_only',
                   'hidden_slash_commands', 'disabled_slash_commands'}
        updates = {k: v for k, v in data.items() if k in allowed}
        if 'message_wrapper_enabled' in updates:
            updates['message_wrapper_enabled'] = 1 if normalize_bool(updates['message_wrapper_enabled'], True) else 0
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [agent_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE agents SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_agent(self, agent_id: str) -> bool:
        # Super agent cannot be deleted
        agent = self.get_agent(agent_id)
        if agent and agent.get('is_super'):
            raise ValueError("Super agent cannot be deleted")
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            conn.commit()
            return cursor.rowcount > 0

    def clone_agent(self, source_id: str, new_id: str, new_name: str,
                    new_description: str = '') -> Optional[str]:
        """Clone an agent: copy all settings, tools, skills, and variables.

        Returns the new agent ID on success, or None if source not found.
        """
        source = self.get_agent(source_id)
        if not source:
            return None
        if self.get_agent(new_id):
            raise ValueError(f"Agent ID '{new_id}' already exists")

        # Build new agent dict: copy all fields, override id/name/desc, skip auto fields
        auto_fields = {'id', 'created_at', 'updated_at', 'last_active_at', 'model',
                       'session_count', 'primary_channel_id', 'avatar_path'}
        clone = {}
        for k, v in source.items():
            if k in auto_fields:
                continue
            clone[k] = v
        clone['id'] = new_id
        clone['name'] = new_name
        clone['description'] = new_description
        clone['is_super'] = False
        clone['enabled'] = True  # cloned agent starts enabled

        self.create_agent(clone)

        # Copy tools, skills, and variables
        with self._connect() as conn:
            cursor = conn.cursor()
            for tid in self.get_agent_tools(source_id):
                cursor.execute(
                    "INSERT INTO agent_tools (agent_id, tool_id) VALUES (?, ?)",
                    (new_id, tid))
            for sid in self.get_agent_skills(source_id):
                cursor.execute(
                    "INSERT INTO agent_skills (agent_id, skill_id) VALUES (?, ?)",
                    (new_id, sid))
            for var in self.get_agent_variables(source_id):
                cursor.execute(
                    "INSERT INTO agent_variables (agent_id, key, value, is_secret) VALUES (?, ?, ?, ?)",
                    (new_id, var['key'], var['value'],
                     1 if var.get('is_secret') else 0))
            conn.commit()

        return new_id

    def get_super_agent(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, description, system_prompt, vision_enabled, created_at, updated_at, summarize_threshold, summarize_tail, summarize_prompt, message_buffer_seconds, inject_agent_id, inject_datetime, send_intermediate_responses, outbound_buffer_seconds, enable_agent_state, workspace, is_super, enabled, default_model_id, sandbox_enabled, attachments_enabled, attachment_max_size_mb, send_file_allowed_path_regex, audio_enabled, video_enabled, artifacts_enabled, last_active_at, safety_checker_enabled, bash_exec_enabled, primary_channel_id, avatar_path, disable_parallel_tool_execution, disable_turn_prefetch, agent_messaging_enabled, session_count, fallback_model_id, model_id, tool_compression_enabled, message_wrapper_enabled, run_as_user, vision_model_id, inter_agent_clear_context, builtin_tools_enabled, workplace_id, always_execute FROM agents WHERE is_super = 1 LIMIT 1")
            row = cursor.fetchone()
            return dict(row) if row else None

    def has_super_agent(self) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM agents WHERE is_super = 1 LIMIT 1")
            return cursor.fetchone() is not None

    # ==================== Agent Tools ====================

    def get_agent_tools(self, agent_id: str) -> List[str]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT tool_id FROM agent_tools WHERE agent_id = ?", (agent_id,))
            return [row[0] for row in cursor.fetchall()]

    def set_agent_tools(self, agent_id: str, tool_ids: List[str]):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM agent_tools WHERE agent_id = ?", (agent_id,))
            for tid in tool_ids:
                cursor.execute(
                    "INSERT INTO agent_tools (agent_id, tool_id) VALUES (?, ?)",
                    (agent_id, tid)
                )
            conn.commit()

    def clear_all_agent_tools(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM agent_tools")
            conn.commit()

    def add_agent_tool(self, agent_id: str, tool_id: str):
        """Add a single tool to an agent (idempotent)."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO agent_tools (agent_id, tool_id) VALUES (?, ?)",
                (agent_id, tool_id)
            )
            conn.commit()

    def remove_agent_tool(self, agent_id: str, tool_id: str):
        """Remove a single tool from an agent."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM agent_tools WHERE agent_id = ? AND tool_id = ?",
                (agent_id, tool_id)
            )
            conn.commit()

    # ==================== Agent Skills ====================

    def get_agent_skills(self, agent_id: str) -> List[str]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT skill_id FROM agent_skills WHERE agent_id = ?", (agent_id,))
            return [row[0] for row in cursor.fetchall()]

    def set_agent_skills(self, agent_id: str, skill_ids: List[str]):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM agent_skills WHERE agent_id = ?", (agent_id,))
            for sid in skill_ids:
                cursor.execute(
                    "INSERT INTO agent_skills (agent_id, skill_id) VALUES (?, ?)",
                    (agent_id, sid)
                )
            conn.commit()

    # ==================== Agent Variables ====================

    def get_agent_variables(self, agent_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key, value, is_secret FROM agent_variables WHERE agent_id = ? ORDER BY key",
                (agent_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_agent_variables_dict(self, agent_id: str) -> Dict[str, str]:
        """Return agent variables as a flat {key: value} dict."""
        rows = self.get_agent_variables(agent_id)
        return {r['key']: r['value'] for r in rows}

    def set_agent_variable(self, agent_id: str, key: str, value: str, is_secret: bool = False):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO agent_variables (agent_id, key, value, is_secret) VALUES (?, ?, ?, ?)",
                (agent_id, key, value, 1 if is_secret else 0)
            )
            conn.commit()

    def delete_agent_variable(self, agent_id: str, key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM agent_variables WHERE agent_id = ? AND key = ?",
                (agent_id, key)
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_agent_variables_bulk(self, agent_id: str, variables: List[Dict[str, Any]]):
        """Replace all variables for an agent with the given list."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM agent_variables WHERE agent_id = ?", (agent_id,))
            for var in variables:
                cursor.execute(
                    "INSERT INTO agent_variables (agent_id, key, value, is_secret) VALUES (?, ?, ?, ?)",
                    (agent_id, var['key'], var.get('value', ''), 1 if var.get('is_secret') else 0)
                )
            conn.commit()

    # ==================== Primary Channel ====================

    def set_primary_channel(self, agent_id: str, channel_id: str) -> bool:
        """Set a channel as primary for an agent. Auto-demotes any existing primary."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agents SET primary_channel_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (channel_id, agent_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    def unset_primary_channel(self, agent_id: str) -> bool:
        """Clear the primary channel for an agent."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agents SET primary_channel_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (agent_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_primary_channel_id(self, agent_id: str) -> Optional[str]:
        """Return the primary channel ID for an agent, or None."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT primary_channel_id FROM agents WHERE id = ?",
                (agent_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

    # ---- Pending tool-call approvals (for SSE snapshot on reconnect) ----

    def store_pending_tool_approval(self, approval_id: str, session_id: str,
                                     agent_id: str, tool_name: str,
                                     tool_args: dict, approval_info: dict,
                                     reasons: list, score: int = None,
                                     source_agent_id: str = None,
                                     source_agent_name: str = None):
        """Persist a pending tool-call approval to the database so
        reconnecting SSE clients can retrieve it via _build_snapshot()."""
        import json
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pending_tool_approvals
                    (id, session_id, agent_id, tool_name, tool_args,
                     approval_info, reasons, score,
                     source_agent_id, source_agent_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                approval_id, session_id, agent_id, tool_name,
                json.dumps(tool_args), json.dumps(approval_info),
                json.dumps(reasons), score,
                source_agent_id, source_agent_name,
            ))
            conn.commit()

    def delete_pending_tool_approval(self, approval_id: str):
        """Remove a resolved tool-call approval from the database."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM pending_tool_approvals WHERE id = ?",
                (approval_id,)
            )
            conn.commit()

    def get_pending_tool_approvals(self) -> list:
        """Return all pending tool-call approvals (for SSE snapshot)."""
        import json
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, session_id, agent_id, tool_name, tool_args,
                       approval_info, reasons, score,
                       source_agent_id, source_agent_name,
                       created_at
                FROM pending_tool_approvals
                ORDER BY created_at
            """).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                for col in ('tool_args', 'approval_info', 'reasons'):
                    try:
                        d[col] = json.loads(d[col])
                    except (json.JSONDecodeError, TypeError):
                        pass
                results.append(d)
            return results

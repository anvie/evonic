import functools
import os
from typing import Dict, Any, List, Optional

import sqlite3

from models.chat import AGENTS_DIR


class ChatDelegationMixin:
    """Chat session delegation to per-agent AgentChatDB instances.
    Requires self._connect(), self.get_agents(), self.get_agent(), self.get_channel()
    from the host class."""

    def _chat_db(self, agent_id: str) -> 'AgentChatDB':
        from models.chat import agent_chat_manager
        return agent_chat_manager.get(agent_id)

    def _refresh_session_count(self, agent_id: str) -> None:
        """Recompute session_count from per-agent chat DB and store in main agents table."""
        try:
            sc, _ = self._chat_db(agent_id).get_counts()
            with self._connect() as conn:
                conn.execute("UPDATE agents SET session_count = ? WHERE id = ?", (sc, agent_id))
                conn.commit()
        except Exception:
            pass

    def get_or_create_session(self, agent_id: str, external_user_id: str,
                               channel_id: str = None,
                               db_agent_id: str = None) -> str:
        """Get or create a session.

        Args:
            agent_id: Stored in the session's agent_id column.
            db_agent_id: If provided, selects which per-agent chat DB to use
                (e.g. parent's DB for sub-agents). Defaults to agent_id.
        """
        _db_id = db_agent_id or agent_id
        channel_type = None
        if channel_id:
            ch = self.get_channel(channel_id)
            channel_type = ch.get('type') if ch else None
        session_id = self._chat_db(_db_id).get_or_create_session(
            agent_id, external_user_id, channel_id, channel_type=channel_type)
        self._refresh_session_count(_db_id)
        return session_id

    def get_session_messages(self, session_id: str, limit: int = 50,
                              agent_id: str = None) -> List[Dict[str, Any]]:
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return []
        return self._chat_db(agent_id).get_session_messages(session_id, limit)

    def add_chat_message(self, session_id: str, role: str, content: str = None,
                          tool_calls: Any = None, tool_call_id: str = None,
                          agent_id: str = None, metadata: dict = None,
                          db_agent_id: str = None) -> int:
        """Add a chat message.

        Args:
            agent_id: Used for _find_agent_for_session fallback and last_active_at.
            db_agent_id: If provided, selects which per-agent chat DB to use
                (e.g. parent's DB for sub-agents). Defaults to agent_id.
        """
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return -1
        _db_id = db_agent_id or agent_id
        result = self._chat_db(_db_id).add_chat_message(session_id, role, content, tool_calls, tool_call_id, metadata=metadata)
        # Update last_active_at only for user/assistant messages — NOT for tool
        # calls or tool results, which can fire dozens of times per turn and
        # cause constant write pressure on the main DB (WAL checkpoint contention
        # blocks reads like db.get_agent() on the agent detail page).
        if role in ('user', 'assistant'):
            try:
                with self._connect() as conn:
                    conn.execute("UPDATE agents SET last_active_at = CURRENT_TIMESTAMP WHERE id = ?", (agent_id,))
                    conn.commit()
            except Exception:
                pass
        return result

    def touch_agent_active(self, agent_id: str) -> None:
        """Update last_active_at on the agents table."""
        try:
            with self._connect() as conn:
                conn.execute("UPDATE agents SET last_active_at = CURRENT_TIMESTAMP WHERE id = ?",
                             (agent_id,))
                conn.commit()
        except Exception:
            pass

    def upsert_agent_state(self, content: str, agent_id: str):
        self._chat_db(agent_id).upsert_agent_state(content)

    def get_agent_state(self, agent_id: str) -> str | None:
        return self._chat_db(agent_id).get_agent_state()

    def upsert_session_state(self, session_id: str, content: str, agent_id: str):
        self._chat_db(agent_id).upsert_session_state(session_id, content)

    def get_session_state(self, session_id: str, agent_id: str) -> str | None:
        return self._chat_db(agent_id).get_session_state(session_id)

    def clear_session(self, session_id: str, agent_id: str = None):
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if agent_id:
            self._chat_db(agent_id).clear_session(session_id)
            from models.chatlog import chatlog_manager
            chatlog_manager.get(agent_id, session_id).clear()

    def get_summary(self, session_id: str, agent_id: str = None):
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return None
        return self._chat_db(agent_id).get_summary(session_id)

    def upsert_summary(self, session_id: str, summary: str,
                        last_message_id: int, message_count: int,
                        agent_id: str = None, last_message_ts: int = None):
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if agent_id:
            self._chat_db(agent_id).upsert_summary(
                session_id, summary, last_message_id, message_count,
                last_message_ts=last_message_ts)

    def get_agent_summaries(self, agent_id: str, query: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """List all session summaries for an agent with optional keyword filter."""
        if not agent_id:
            return []
        return self._chat_db(agent_id).get_agent_summaries(query=query, limit=limit)

    def get_messages_after(self, session_id: str, after_id: int,
                            agent_id: str = None):
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return []
        return self._chat_db(agent_id).get_messages_after(session_id, after_id)

    def get_messages_between(self, session_id: str, after_id: int,
                              up_to_id: int, agent_id: str = None):
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return []
        return self._chat_db(agent_id).get_messages_between(session_id, after_id, up_to_id)

    def get_message_count(self, session_id: str, agent_id: str = None):
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return 0
        return self._chat_db(agent_id).get_message_count(session_id)

    def delete_session(self, session_id: str, agent_id: str = None) -> bool:
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return False
        result = self._chat_db(agent_id).delete_session(session_id)
        if result:
            import os
            from models.chatlog import chatlog_manager
            cl = chatlog_manager.get(agent_id, session_id)
            cl.close()
            chatlog_manager.evict(agent_id, session_id)
            try:
                os.remove(cl._path)
            except FileNotFoundError:
                pass
            self._refresh_session_count(agent_id)
            # Wipe attachments tied to this session (rows + on-disk files) so
            # they don't linger unreachable after the conversation is gone.
            try:
                self.delete_session_attachments(session_id, agent_id)
            except (sqlite3.Error, OSError, ValueError) as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Failed to clear attachments for session %s: %s",
                    session_id, e,
                )
        return result

    def get_latest_agent_request_metadata(self, session_id: str, agent_id: str = None, sender_agent_id: str = None) -> dict | None:
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return None
        return self._chat_db(agent_id).get_latest_agent_request_metadata(session_id, sender_agent_id)

    def get_session_messages_full(self, session_id: str, agent_id: str = None) -> List[Dict[str, Any]]:
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return []
        return self._chat_db(agent_id).get_session_messages_full(session_id)

    def get_new_messages(self, session_id: str, after_id: int, agent_id: str = None) -> List[Dict[str, Any]]:
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return []
        return self._chat_db(agent_id).get_new_messages(session_id, after_id)

    def get_last_assistant_message(self, session_id: str, agent_id: str = None) -> Optional[Dict[str, Any]]:
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return None
        return self._chat_db(agent_id).get_last_assistant_message(session_id)

    def set_session_bot_enabled(self, session_id: str, enabled: bool, agent_id: str = None):
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if agent_id:
            self._chat_db(agent_id).set_session_bot_enabled(session_id, enabled)

    def is_session_bot_enabled(self, session_id: str, agent_id: str = None) -> bool:
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return True
        return self._chat_db(agent_id).is_session_bot_enabled(session_id)

    def get_latest_human_session(self, agent_id: str) -> Optional[Dict[str, Any]]:
        return self._chat_db(agent_id).get_latest_human_session(agent_id)

    def get_web_fallback_session(self, agent_id: str,
                                  exclude_session_id: str = None) -> Optional[Dict[str, Any]]:
        """Return the most recent web session (no channel) for a human user.

        Delegates to the per-agent chat DB so escalate_to_user can deliver
        messages to the web UI as a secondary target.
        """
        return self._chat_db(agent_id).get_web_fallback_session(
            agent_id, exclude_session_id=exclude_session_id)

    def get_session_with_details(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Find session across all agent DBs and enrich with agent/channel info."""
        agent_id = self._find_agent_for_session(session_id)
        if not agent_id:
            return None
        session = self._chat_db(agent_id).get_session(session_id)
        if not session:
            return None
        # Enrich with agent and channel info from main DB.
        # Sub-agents have no DB entry, so fall back to the session's own agent_id.
        agent = self.get_agent(session.get('agent_id') or agent_id)
        session['agent_name'] = (agent['name'] if agent
                                 else (session.get('agent_id') or 'Unknown'))
        if session.get('channel_id'):
            ch = self.get_channel(session['channel_id'])
            session['channel_type'] = ch.get('type') if ch else None
            session['channel_name'] = ch.get('name') if ch else None
        else:
            session['channel_type'] = None
            session['channel_name'] = None
        return session

    # SQLite's compiled-in ATTACH limit is 10; reserve 1 slot for the main DB.
    _ATTACH_BATCH = 9

    def get_all_sessions(self, search: str = None, limit: int = 50, offset: int = 0,
                          exclude_test: bool = True) -> tuple:
        """Aggregate sessions across all per-agent chat DBs using ATTACH + UNION ALL.

        Agents are processed in batches of 9 to stay within SQLite's compiled-in
        10-database ATTACH limit.  All matching rows are collected across batches,
        sorted globally in Python, then paginated."""
        agents = self.get_agents()
        if not agents:
            return [], 0

        # Build the list of agent DBs that actually exist on disk.
        valid = []
        for agent in agents:
            aid = agent['id']
            db_path = os.path.join(AGENTS_DIR, aid, 'chat.db')
            if not os.path.exists(db_path):
                continue
            safe_id = aid.replace('-', '_').replace('.', '_').replace('"', '_')
            valid.append((aid, db_path, f"a_{safe_id}"))

        if not valid:
            return [], 0

        # WHERE clause (shared across all batches)
        where_parts = []
        where_params = []
        if exclude_test:
            where_parts.append(
                "NOT (combined.external_user_id = 'web_test' AND combined.channel_id IS NULL)"
            )
        if search:
            q = f"%{search}%"
            where_parts.append(
                "(ag.name LIKE ? OR combined.external_user_id LIKE ? OR peer.name LIKE ?)"
            )
            where_params.extend([q, q, q])
        where_sql = " AND ".join(where_parts) if where_parts else "1=1"

        _qmsg = ('SELECT m2.content FROM "{a}".chat_messages m2 '
                 "WHERE m2.session_id = s.id AND m2.role IN ('user', 'assistant') "
                 'AND m2.content IS NOT NULL ORDER BY m2.created_at DESC LIMIT 1')
        _qrole = ('SELECT m3.role FROM "{a}".chat_messages m3 '
                  "WHERE m3.session_id = s.id AND m3.role IN ('user', 'assistant') "
                  'AND m3.content IS NOT NULL ORDER BY m3.created_at DESC LIMIT 1')
        _qcnt = 'SELECT COUNT(*) FROM "{a}".chat_messages m WHERE m.session_id = s.id'

        all_rows = []

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row

            for i in range(0, len(valid), self._ATTACH_BATCH):
                batch = valid[i:i + self._ATTACH_BATCH]
                attached_aliases = []

                for _aid, db_path, alias in batch:
                    conn.execute(f'ATTACH DATABASE ? AS "{alias}"', (db_path,))
                    attached_aliases.append(alias)

                arms = []
                for _aid, _db_path, alias in batch:
                    arms.append(f'''
                        SELECT s.id, s.agent_id, s.channel_id, s.external_user_id,
                               s.bot_enabled, s.created_at, s.updated_at,
                               ({_qcnt.format(a=alias)}) AS message_count,
                               ({_qmsg.format(a=alias)}) AS last_message,
                               ({_qrole.format(a=alias)}) AS last_message_role
                        FROM "{alias}".chat_sessions s
                        WHERE s.archived = 0
                    ''')

                union_body = " UNION ALL ".join(arms)
                data_sql = f"""
                    SELECT combined.*,
                           COALESCE(ag.name, combined.agent_id) AS agent_name,
                           ch.type AS channel_type, ch.name AS channel_name,
                           peer.name AS peer_agent_name
                    FROM ({union_body}) combined
                    LEFT JOIN agents ag ON ag.id = combined.agent_id
                    LEFT JOIN channels ch ON ch.id = combined.channel_id
                    LEFT JOIN agents peer ON (
                        combined.external_user_id LIKE '__agent__%'
                        AND peer.id = substr(combined.external_user_id, 10)
                    )
                    WHERE {where_sql}
                """
                rows = conn.execute(data_sql, where_params).fetchall()
                all_rows.extend(dict(r) for r in rows)

                for alias in attached_aliases:
                    conn.execute(f'DETACH DATABASE "{alias}"')

        # Sort globally then paginate in Python (needed because batches are independent).
        all_rows.sort(key=lambda r: r.get('updated_at') or '', reverse=True)
        total = len(all_rows)

        if limit > 0:
            return all_rows[offset:offset + limit], total
        return [], total

    @functools.lru_cache(maxsize=256)
    def _find_agent_for_session(self, session_id: str) -> Optional[str]:
        """Look up which agent owns a session by scanning agent chat DBs.
        Result is LRU-cached (max 256 entries) to avoid repeated full agent scans
        when multiple methods query the same session_id in a single request."""
        agents = self.get_agents()
        for agent in agents:
            chat_db = self._chat_db(agent['id'])
            if chat_db.has_session(session_id):
                return agent['id']
        return None

    def clear_all_sessions(self):
        """Drop all chat sessions, messages, and summaries across all agents.

        Also removes every stored attachment (rows + on-disk files) since they
        are no longer reachable once the sessions referencing them are gone.
        """
        agents = self.get_agents()
        for agent in agents:
            chat_db = self._chat_db(agent['id'])
            chat_db.clear_all()
        # Wipe attachments after sessions to keep storage in sync with the
        # newly-cleared chat history.
        try:
            self.delete_all_attachments()
        except (sqlite3.Error, OSError) as e:
            # Logged inside delete_all_attachments for per-file errors; this
            # catches DB-level issues without breaking the session clear.
            import logging
            logging.getLogger(__name__).warning(
                "Failed to clear attachments during clear_all_sessions: %s", e
            )

    # ---- Long-term Memory delegation ----

    def add_memory(self, agent_id: str, content: str, category: str = 'general',
                   source_session_id: str = None) -> int:
        return self._chat_db(agent_id).add_memory(content, category, source_session_id)

    def update_memory(self, agent_id: str, memory_id: int, content: str,
                      category: str = None):
        self._chat_db(agent_id).update_memory(memory_id, content, category)

    def search_memories(self, agent_id: str, query: str,
                        limit: int = 10) -> List[Dict[str, Any]]:
        return self._chat_db(agent_id).search_memories(query, limit)

    def get_all_memories(self, agent_id: str,
                         include_expired: bool = False) -> List[Dict[str, Any]]:
        return self._chat_db(agent_id).get_all_memories(include_expired)

    def get_recent_memories(self, agent_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self._chat_db(agent_id).get_recent_memories(limit)

    def expire_memory(self, agent_id: str, memory_id: int):
        self._chat_db(agent_id).expire_memory(memory_id)

import functools
import os
import re
from typing import Dict, Any, List, Optional, Tuple

import sqlite3




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

    def get_session_id(self, agent_id: str, external_user_id: str,
                        channel_id: str = None, db_agent_id: str = None) -> Optional[str]:
        """Read-only session lookup — no writes, no sync. Returns session_id or None."""
        _db_id = db_agent_id or agent_id
        return self._chat_db(_db_id).get_session_id(agent_id, external_user_id, channel_id)

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
        self._sync_session_index(agent_id, session_id, db_agent_id=_db_id)
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
            # Keep session_index in sync (message_count, last_message, last_message_role, updated_at).
            # Only user/assistant messages to avoid write amplification from tool calls.
            self._sync_session_index(agent_id, session_id, db_agent_id=_db_id)
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

    def get_agent_state(self, agent_id: str) -> Optional[str]:
        return self._chat_db(agent_id).get_agent_state()

    def upsert_session_state(self, session_id: str, content: str, agent_id: str):
        self._chat_db(agent_id).upsert_session_state(session_id, content)

    def get_session_state(self, session_id: str, agent_id: str) -> Optional[str]:
        return self._chat_db(agent_id).get_session_state(session_id)

    def clear_session(self, session_id: str, agent_id: str = None, no_archive: bool = False):
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if agent_id:
            # Archive session data before clearing (background, non-blocking)
            import config
            if config.SESSION_ARCHIVE and not no_archive:
                from models.session_archive import SessionArchiver
                SessionArchiver.archive_session(agent_id, session_id)

            self._chat_db(agent_id).clear_session(session_id)
            from models.chatlog import chatlog_manager
            chatlog_manager.get(agent_id, session_id).clear()
            # Drop the byte-exact LLM-trace file (records already read by archiver above)
            from models.llm_trace import llm_trace_manager
            llm_trace_manager.get(agent_id, session_id).clear()
            llm_trace_manager.evict(agent_id, session_id)
            self._remove_session_index(session_id)

    def get_last_message_timestamp(self, session_id: str, agent_id: str = None) -> Optional[float]:
        """Return the unix timestamp of the most recent message in a session.

        Returns None if the session has no messages.
        """
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return None
        return self._chat_db(agent_id).get_last_message_timestamp(session_id)

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
            self._remove_session_index(session_id)
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

    def get_latest_agent_request_metadata(self, session_id: str, agent_id: str = None, sender_agent_id: str = None) -> Optional[dict]:
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return None
        return self._chat_db(agent_id).get_latest_agent_request_metadata(session_id, sender_agent_id)

    def get_session_messages_full(self, session_id: str, agent_id: str = None, limit: int = 200, before_id: Optional[int] = None) -> Tuple[List[Dict[str, Any]], bool]:
        agent_id = agent_id or self._find_agent_for_session(session_id)
        if not agent_id:
            return [], False
        return self._chat_db(agent_id).get_session_messages_full(session_id, limit=limit, before_id=before_id)

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
        session['attachments_enabled'] = bool(agent.get('attachments_enabled', 1)) if agent else True
        # Resolve the peer agent name for inter-agent sessions
        # (external_user_id = '__agent__<peer_id>'), mirroring get_all_sessions so
        # explorer/sub-agent captions can show the real parent name instead of 'Unknown'.
        ext = session.get('external_user_id') or ''
        if ext.startswith('__agent__'):
            peer = self.get_agent(ext[len('__agent__'):])
            session['peer_agent_name'] = peer['name'] if peer else None
        else:
            session['peer_agent_name'] = None
        if session.get('channel_id'):
            ch = self.get_channel(session['channel_id'])
            session['channel_type'] = ch.get('type') if ch else None
            session['channel_name'] = ch.get('name') if ch else None
        else:
            session['channel_type'] = None
            session['channel_name'] = None
        return session

    # ---- Session Index helpers (materialized view in main DB) ----

    def _sync_session_index(self, agent_id: str, session_id: str,
                            db_agent_id: str = None) -> None:
        """Upsert session metadata while preserving its logical agent owner.

        ``agent_id`` is written to ``session_index``. ``db_agent_id`` selects
        the chat DB that physically stores the session, which is the parent for
        ephemeral sub-agents.
        """
        import logging
        logger = logging.getLogger(__name__)
        try:
            chat_db = self._chat_db(db_agent_id or agent_id)
            session = chat_db.get_session(session_id)
            if not session:
                # Session deleted concurrently — remove from index if it exists.
                self._remove_session_index(session_id)
                return
            mc = chat_db.get_message_count(session_id)
            # Get last message (content + role) directly from the per-agent DB.
            last_msg = None
            with chat_db._connect() as agent_conn:
                agent_conn.row_factory = sqlite3.Row
                last_msg = agent_conn.execute("""
                    SELECT content, role FROM chat_messages
                    WHERE session_id = ? AND role IN ('user', 'assistant')
                      AND content IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1
                """, (session_id,)).fetchone()
            with self._connect() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO session_index
                        (session_id, agent_id, external_user_id, channel_id,
                         bot_enabled, archived, created_at, updated_at,
                         message_count, last_message, last_message_role)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    session_id,
                    agent_id,
                    session.get('external_user_id', ''),
                    session.get('channel_id'),
                    session.get('bot_enabled', 1),
                    session.get('archived', 0),
                    session.get('created_at'),
                    session.get('updated_at'),
                    mc,
                    last_msg['content'] if last_msg else None,
                    last_msg['role'] if last_msg else None,
                ))
                conn.commit()
        except Exception:
            logger.debug("_sync_session_index failed for session %s agent %s",
                         session_id, agent_id, exc_info=True)

    def _remove_session_index(self, session_id: str) -> None:
        """Remove a session from the materialized session_index."""
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM session_index WHERE session_id = ?", (session_id,))
                conn.commit()
        except Exception:
            pass


    def get_all_sessions(self, search: str = None, limit: int = 50, offset: int = 0,
                          exclude_test: bool = True) -> tuple:
        """Query session_index (materialized view in main DB) joined with agents/channels.

        Single query against the main DB — no ATTACH/DETACH, no UNION ALL,
        no Python-side sort of all rows.  SQL handles ORDER BY, LIMIT, OFFSET."""
        where_parts = ["si.archived = 0"]
        where_params = []

        if exclude_test:
            where_parts.append(
                "NOT (si.external_user_id = 'web_test' AND si.channel_id IS NULL)"
            )
        if search:
            q = "%" + search + "%"
            where_parts.append(
                "(COALESCE(ag.name, si.agent_id) LIKE ?"
                " OR si.external_user_id LIKE ?"
                " OR peer.name LIKE ?)"
            )
            where_params.extend([q, q, q])

        where_sql = " AND ".join(where_parts)

        # Base query: session_index joined with agents and channels.
        base_from = """session_index si
            LEFT JOIN agents ag ON ag.id = si.agent_id
            LEFT JOIN channels ch ON ch.id = si.channel_id
            LEFT JOIN agents peer ON (
                si.external_user_id LIKE '__agent__%'
                AND peer.id = substr(si.external_user_id, 10)
            )"""

        # Count total matching rows (same WHERE, no LIMIT/OFFSET).
        # All table/column names in base_from, columns, where_sql are compile-time constants;
        # values use ? placeholders.
        count_sql = "SELECT COUNT(*) FROM " + base_from + " WHERE " + where_sql

        # Data query with ORDER BY, LIMIT, OFFSET.
        columns = """si.session_id AS id,
                     si.agent_id, si.channel_id, si.external_user_id,
                     si.bot_enabled, si.created_at, si.updated_at,
                     si.message_count, si.last_message, si.last_message_role,
                     COALESCE(ag.name, si.agent_id) AS agent_name,
                     ch.type AS channel_type, ch.name AS channel_name,
                     peer.name AS peer_agent_name,
                     ag.attachments_enabled"""
        data_sql = ("SELECT " + columns
                    + " FROM " + base_from
                    + " WHERE " + where_sql
                    + " ORDER BY si.updated_at DESC"
                    + " LIMIT ? OFFSET ?")

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            total_row = conn.execute(count_sql, where_params).fetchone()
            total = total_row[0] if total_row else 0
        if limit > 0:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(data_sql, where_params + [limit, offset]).fetchall()
                return [dict(r) for r in rows], total
        return [], total

    @functools.lru_cache(maxsize=256)
    def _find_agent_for_session(self, session_id: str) -> Optional[str]:
        """Look up which agent owns a session from the session_index in main DB.
        Result is LRU-cached (max 256 entries) to avoid repeated main DB queries."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT agent_id FROM session_index WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            agent_id = row['agent_id'] if row else None
        if not agent_id:
            return None
        # Sub-agents and explorers persist their messages into the PARENT's
        # per-agent chat DB (db_agent_id = parent_id), but session_index records
        # the sub-agent id. Resolve to the parent so message/session lookups hit
        # the DB that actually holds the rows — otherwise the chat panel loads
        # from the sub-agent's empty DB and shows nothing.
        m = re.match(r'^(.+)_(?:sub|explorer|organizer)_\d+$', agent_id)
        if m and self._chat_db(m.group(1)).get_session(session_id):
            return m.group(1)
        return agent_id

    def clear_all_sessions(self):
        """Drop all chat sessions, messages, and summaries across all agents.

        Also removes every stored attachment (rows + on-disk files) since they
        are no longer reachable once the sessions referencing them are gone.
        """
        agents = self.get_agents()
        for agent in agents:
            chat_db = self._chat_db(agent['id'])
            chat_db.clear_all()
        # Clear the session_index too — all sessions are gone.
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM session_index")
                conn.commit()
        except Exception:
            pass
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
                   source_session_id: str = None, dimension: str = None) -> int:
        return self._chat_db(agent_id).add_memory(content, category, source_session_id, dimension)

    def update_memory(self, agent_id: str, memory_id: int, content: str,
                      category: str = None, dimension: str = None):
        self._chat_db(agent_id).update_memory(memory_id, content, category, dimension)

    def search_memories(self, agent_id: str, query: str,
                        limit: int = 10) -> List[Dict[str, Any]]:
        return self._chat_db(agent_id).search_memories(query, limit)

    def get_all_memories(self, agent_id: str,
                         include_expired: bool = False) -> List[Dict[str, Any]]:
        return self._chat_db(agent_id).get_all_memories(include_expired)

    def get_recent_memories(self, agent_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self._chat_db(agent_id).get_recent_memories(limit)

    def get_memories_by_dimension(self, agent_id: str, dimension: str) -> List[Dict[str, Any]]:
        return self._chat_db(agent_id).get_memories_by_dimension(dimension)

    def get_null_dimension_memories(self, agent_id: str) -> List[Dict[str, Any]]:
        return self._chat_db(agent_id).get_null_dimension_memories()

    def get_active_dimensions(self, agent_id: str, limit: int = 50) -> List[str]:
        return self._chat_db(agent_id).get_active_dimensions(limit)

    def supersede_memory(self, agent_id: str, old_memory_id: int, new_memory_id: int):
        self._chat_db(agent_id).supersede_memory(old_memory_id, new_memory_id)

    def expire_memory(self, agent_id: str, memory_id: int):
        self._chat_db(agent_id).expire_memory(memory_id)

    def clear_all_memories(self, agent_id: str) -> int:
        return self._chat_db(agent_id).clear_all_memories()

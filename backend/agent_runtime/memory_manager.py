"""
memory_manager.py — LLM-powered long-term memory for agents.

Extract→Deduplicate→Store→Retrieve pattern (inspired by Mem0):
1. Extract salient facts from conversation summary via LLM
2. Deduplicate/merge against existing memories
3. Store to per-agent SQLite memories table (FTS5-indexed, zero dependencies)
4. Retrieve via FTS5 BM25 keyword search at context-build time

No new pip dependencies — uses existing LLM client and SQLite FTS5 (Python stdlib).
"""

import json
import threading
from typing import List, Optional

from models.db import db
from backend.llm_client import llm_client, strip_thinking_tags

_EXTRACT_PROMPT = """You are a long-term memory extractor for an AI assistant. Given a conversation summary, extract facts worth remembering in FUTURE conversations.

Rules:
- Only extract facts that are durable and useful across sessions
- Skip ephemeral details (current task status, temporary states, in-progress items)
- Each fact must be a single clear, self-contained sentence in English
- Categories:
  - user_info: user identity, contact info, role (name, phone, email, job title)
  - preference: stated likes/dislikes, communication style, language preference
  - decision: commitments or choices made by the user or agreed upon
  - context: background facts about the user's domain, project, or situation
  - instruction: persistent instructions given to the agent about how to behave
- Return a JSON array only: [{{"content": "...", "category": "..."}}]
- If nothing worth remembering long-term, return: []

Conversation summary:
{summary}

Return only a JSON array, no explanation:"""

_DEDUP_PROMPT = """You are a memory deduplicator. Given new facts and existing memories, decide how to handle each new fact.

Existing memories:
{existing}

New facts to process:
{new_facts}

Rules:
- If a new fact is semantically identical to an existing memory: return null for that entry
- If a new fact UPDATES/CONTRADICTS an existing memory (e.g. user changed phone number): return {{"action": "update", "id": <existing_id>, "content": "<merged content>", "category": "..."}}
- If a new fact is genuinely new (no overlap): return {{"action": "add", "content": "...", "category": "..."}}

Return a JSON array with exactly one entry per new fact (same order as new facts):"""


def extract_and_store_memories(agent: dict, session_id: str, summary: str,
                                llm_lock: threading.Lock) -> None:
    """Extract memorable facts from a conversation summary and persist them.

    Runs in a background thread after summarization. Non-fatal on any error.
    """
    agent_id = agent['id']
    try:
        # Step 1: Extract facts from summary
        prompt = _EXTRACT_PROMPT.format(summary=summary)
        with llm_lock:
            result = llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                temperature=0.0,
                enable_thinking=False,
                max_tokens=1024,
            )
        if not result.get('success'):
            return

        choice = result['response'].get('choices', [{}])[0]
        if choice.get('finish_reason') == 'length':
            return

        raw = choice.get('message', {}).get('content', '')
        raw, _ = strip_thinking_tags(raw)
        raw = _strip_code_fences(raw)

        try:
            facts = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(facts, list) or not facts:
            return

        # Filter out malformed entries
        facts = [f for f in facts
                 if isinstance(f, dict) and f.get('content', '').strip()]
        if not facts:
            return

        # Step 2: Get existing memories for deduplication
        existing = db.get_all_memories(agent_id)

        if existing:
            existing_text = "\n".join(
                f"[id={m['id']}] ({m['category']}) {m['content']}"
                for m in existing[:60]  # cap to avoid huge prompts
            )
            new_text = "\n".join(
                f"- ({f.get('category', 'general')}) {f['content']}"
                for f in facts
            )
            dedup_prompt = _DEDUP_PROMPT.format(
                existing=existing_text,
                new_facts=new_text,
            )
            with llm_lock:
                dedup_result = llm_client.chat_completion(
                    messages=[{"role": "user", "content": dedup_prompt}],
                    tools=None,
                    temperature=0.0,
                    enable_thinking=False,
                    max_tokens=1024,
                )
            if dedup_result.get('success'):
                dedup_choice = dedup_result['response'].get('choices', [{}])[0]
                dedup_raw = dedup_choice.get('message', {}).get('content', '')
                dedup_raw, _ = strip_thinking_tags(dedup_raw)
                dedup_raw = _strip_code_fences(dedup_raw)
                try:
                    operations = json.loads(dedup_raw)
                    if isinstance(operations, list):
                        for op in operations:
                            if op is None:
                                continue
                            action = op.get('action')
                            if action == 'add' and op.get('content', '').strip():
                                db.add_memory(agent_id, op['content'].strip(),
                                              op.get('category', 'general'), session_id)
                            elif action == 'update' and op.get('id') and op.get('content', '').strip():
                                db.update_memory(agent_id, int(op['id']),
                                                 op['content'].strip(), op.get('category'))
                        return  # dedup handled all facts
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass  # fall through to simple add

        # No existing memories or dedup failed: add all new facts directly
        for fact in facts:
            db.add_memory(agent_id, fact['content'].strip(),
                          fact.get('category', 'general'), session_id)

    except Exception as e:
        print(f"[MemoryManager] Extraction failed for agent {agent_id} (non-fatal): {e}")


def get_memories_for_context(agent_id: str, messages: list,
                              limit: int = 8) -> Optional[str]:
    """Retrieve relevant memories for injection into the LLM context.

    Searches using the last user message as a FTS5 query. Falls back to
    most-recent memories if no query or no FTS5 matches.

    Returns a formatted markdown string or None if no memories exist.
    """
    try:
        query = _extract_last_user_query(messages)
        memories: List[dict] = []

        if query:
            fts_query = _sanitize_fts_query(query)
            if fts_query:
                try:
                    memories = db.search_memories(agent_id, fts_query, limit)
                except Exception:
                    pass  # FTS5 can fail on unusual query syntax — fall through

        if not memories:
            memories = db.get_recent_memories(agent_id, limit)

        if not memories:
            return None

        lines = ["## Long-term Memory",
                 "Facts remembered from past conversations:"]
        for m in memories:
            lines.append(f"- [{m['category']}] {m['content']}")
        return "\n".join(lines)

    except Exception as e:
        print(f"[MemoryManager] Context retrieval failed for agent {agent_id} (non-fatal): {e}")
        return None


def store_memory(agent_id: str, session_id: str, content: str,
                 category: str = 'general') -> dict:
    """Directly store a memory. Used by the `remember` built-in tool."""
    content = content.strip()
    if not content:
        return {"error": "Memory content cannot be empty."}
    try:
        memory_id = db.add_memory(agent_id, content, category, session_id)
        return {"result": "Memory stored.", "id": memory_id,
                "content": content, "category": category}
    except Exception as e:
        return {"error": f"Failed to store memory: {e}"}


def search_memories(agent_id: str, query: str, limit: int = 10) -> dict:
    """Search memories by keyword. Used by the `recall` built-in tool."""
    try:
        fts_query = _sanitize_fts_query(query)
        if fts_query:
            memories = db.search_memories(agent_id, fts_query, limit)
        else:
            memories = db.get_recent_memories(agent_id, limit)

        if not memories:
            return {"result": "No memories found.", "memories": [], "count": 0}
        return {
            "memories": [
                {"id": m['id'], "content": m['content'],
                 "category": m['category'], "created_at": m['created_at']}
                for m in memories
            ],
            "count": len(memories),
        }
    except Exception as e:
        return {"error": f"Memory search failed: {e}"}


def forget_memory(agent_id: str, memory_id: int, target_agent_id: str = None,
                  is_super: bool = False) -> dict:
    """Soft-delete a specific memory. Used by the `forget_memory` built-in tool.

    Regular agents can only delete their own memories. Super agents can
    specify a target_agent_id to delete another agent's memory.
    """
    try:
        # Determine whose memory we're operating on
        effective_agent_id = target_agent_id if target_agent_id else agent_id

        # Authorization: only super agents can delete another agent's memories
        if target_agent_id and target_agent_id != agent_id and not is_super:
            return {
                "error": (
                    f"Cannot delete memory belonging to agent '{target_agent_id}'. "
                    "Only super agents can delete another agent's memories."
                )
            }

        # Verify the memory exists and belongs to the effective agent
        memories = db.get_all_memories(effective_agent_id, include_expired=True)
        target_memory = None
        for m in memories:
            if m['id'] == memory_id:
                target_memory = m
                break

        if not target_memory:
            return {
                "error": (
                    f"Memory {memory_id} not found for agent '{effective_agent_id}'."
                )
            }

        if target_memory.get('expired'):
            return {
                "error": f"Memory {memory_id} is already deleted.",
                "id": memory_id,
            }

        db.expire_memory(effective_agent_id, memory_id)
        return {
            "result": "Memory forgotten.",
            "id": memory_id,
            "content": target_memory['content'],
            "category": target_memory['category'],
        }
    except Exception as e:
        return {"error": f"Failed to forget memory: {e}"}


# ---- Helpers ----

def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1]
        text = text.rsplit('```', 1)[0]
    return text.strip()


def _extract_last_user_query(messages: list) -> Optional[str]:
    for msg in reversed(messages):
        if msg.get('role') == 'user':
            content = msg.get('content')
            if isinstance(content, str) and content.strip():
                return content[:300]
    return None


def _sanitize_fts_query(query: str) -> str:
    """Build a safe FTS5 query from free text (avoid syntax errors)."""
    # Keep words longer than 2 chars, strip FTS5 special chars
    import re
    words = re.findall(r'[a-zA-Z0-9\u00C0-\u024F]{3,}', query)
    return ' '.join(words[:15])  # cap to avoid overly long queries

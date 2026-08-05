import sqlite3
import os
import shutil
import config


def _migrate_db_to_subdir(target_path: str):
    """Move evonic.db (or legacy evaluation.db) from project root to db/ subdir on first run after upgrade."""
    root_db = os.path.join(config.BASE_DIR, "evaluation.db")
    if os.path.isfile(root_db) and not os.path.isfile(target_path):
        shutil.move(root_db, target_path)


class SchemaMixin:
    """Database schema initialization and migrations. Requires self._connect() from the host class."""

    def _init_tables(self):
        """Initialize database tables"""
        with self._connect() as conn:
            cursor = conn.cursor()

            # Evaluation runs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at DATETIME NOT NULL,
                    completed_at DATETIME,
                    model_name TEXT,
                    summary TEXT,
                    overall_score REAL,
                    total_tokens INTEGER DEFAULT 0,
                    total_duration_ms INTEGER DEFAULT 0
                )
            """)

            # Test results table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    model_name TEXT,
                    domain TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    prompt TEXT,
                    response TEXT,
                    expected TEXT,
                    score REAL,
                    status TEXT NOT NULL,
                    details TEXT,
                    duration_ms INTEGER,
                    FOREIGN KEY (run_id) REFERENCES evaluation_runs (run_id)
                )
            """)

            # Improvement cycles table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS improvement_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    base_run_id INTEGER NOT NULL,
                    improved_run_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME,
                    status TEXT DEFAULT 'pending',
                    analysis TEXT,
                    training_data_path TEXT,
                    examples_count INTEGER,
                    comparison TEXT,
                    recommendation TEXT,
                    FOREIGN KEY (base_run_id) REFERENCES evaluation_runs (run_id),
                    FOREIGN KEY (improved_run_id) REFERENCES evaluation_runs (run_id)
                )
            """)

            # Generated training data table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS generated_training_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    source_test_id INTEGER,
                    domain TEXT,
                    level INTEGER,
                    prompt TEXT,
                    response TEXT,
                    tool_calls TEXT,
                    rationale TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (cycle_id) REFERENCES improvement_cycles (cycle_id),
                    FOREIGN KEY (source_test_id) REFERENCES test_results (id)
                )
            """)

            # ==================== Configurable Test System Tables ====================

            # Domains table (cache of domain.json files)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS domains (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    icon TEXT,
                    color TEXT,
                    evaluator_id TEXT,
                    system_prompt TEXT,
                    system_prompt_mode TEXT DEFAULT 'overwrite',
                    enabled BOOLEAN DEFAULT 1,
                    path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Levels table (cache of level.json files)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS levels (
                    domain_id TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    system_prompt TEXT,
                    system_prompt_mode TEXT DEFAULT 'overwrite',
                    path TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (domain_id, level),
                    FOREIGN KEY (domain_id) REFERENCES domains(id)
                )
            """)

            # Tests table (cache of test JSON files)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tests (
                    id TEXT PRIMARY KEY,
                    domain_id TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    system_prompt TEXT,
                    system_prompt_mode TEXT DEFAULT 'overwrite',
                    prompt TEXT NOT NULL,
                    expected TEXT,
                    evaluator_id TEXT,
                    timeout_ms INTEGER DEFAULT 30000,
                    weight REAL DEFAULT 1.0,
                    enabled BOOLEAN DEFAULT 1,
                    path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (domain_id) REFERENCES domains(id)
                )
            """)

            # Evaluators table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS evaluators (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT,
                    eval_prompt TEXT,
                    extraction_regex TEXT,
                    uses_pass2 BOOLEAN DEFAULT 0,
                    config TEXT,
                    path TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Level scores (aggregated from multiple tests)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS level_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    domain TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    average_score REAL NOT NULL,
                    total_tests INTEGER NOT NULL,
                    passed_tests INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (run_id) REFERENCES evaluation_runs(run_id)
                )
            """)

            # Individual test results (new table for multi-test per level)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS individual_test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    test_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    prompt TEXT,
                    response TEXT,
                    expected TEXT,
                    score REAL,
                    status TEXT NOT NULL,
                    details TEXT,
                    duration_ms INTEGER,
                    model_name TEXT,
                    system_prompt TEXT,
                    system_prompt_mode TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (run_id) REFERENCES evaluation_runs(run_id),
                    FOREIGN KEY (test_id) REFERENCES tests(id)
                )
            """)

            # Tools registry table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tools (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    function_def TEXT NOT NULL,
                    mock_response TEXT,
                    mock_response_type TEXT DEFAULT 'json',
                    path TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migrate run_id from TEXT (UUID) to INTEGER AUTOINCREMENT
            # Drop all tables that reference evaluation_runs and recreate them
            cursor.execute("PRAGMA table_info(evaluation_runs)")
            er_info = cursor.fetchall()
            er_col_types = {row[1]: row[2] for row in er_info}
            if er_col_types.get('run_id', '').upper() != 'INTEGER':
                cursor.execute("DROP TABLE IF EXISTS individual_test_results")
                cursor.execute("DROP TABLE IF EXISTS level_scores")
                cursor.execute("DROP TABLE IF EXISTS test_results")
                cursor.execute("DROP TABLE IF EXISTS improvement_cycles")
                cursor.execute("DROP TABLE IF EXISTS evaluation_runs")
                cursor.execute("""
                    CREATE TABLE evaluation_runs (
                        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        started_at DATETIME NOT NULL,
                        completed_at DATETIME,
                        model_name TEXT,
                        summary TEXT,
                        overall_score REAL,
                        total_tokens INTEGER DEFAULT 0,
                        total_duration_ms INTEGER DEFAULT 0
                    )
                """)
                cursor.execute("""
                    CREATE TABLE test_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        model_name TEXT,
                        domain TEXT NOT NULL,
                        level INTEGER NOT NULL,
                        prompt TEXT,
                        response TEXT,
                        expected TEXT,
                        score REAL,
                        status TEXT NOT NULL,
                        details TEXT,
                        duration_ms INTEGER,
                        FOREIGN KEY (run_id) REFERENCES evaluation_runs (run_id)
                    )
                """)
                cursor.execute("""
                    CREATE TABLE improvement_cycles (
                        cycle_id TEXT PRIMARY KEY,
                        base_run_id INTEGER NOT NULL,
                        improved_run_id INTEGER,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        completed_at DATETIME,
                        status TEXT DEFAULT 'pending',
                        analysis TEXT,
                        training_data_path TEXT,
                        examples_count INTEGER,
                        comparison TEXT,
                        recommendation TEXT,
                        FOREIGN KEY (base_run_id) REFERENCES evaluation_runs (run_id),
                        FOREIGN KEY (improved_run_id) REFERENCES evaluation_runs (run_id)
                    )
                """)
                cursor.execute("""
                    CREATE TABLE level_scores (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        domain TEXT NOT NULL,
                        level INTEGER NOT NULL,
                        average_score REAL NOT NULL,
                        total_tests INTEGER NOT NULL,
                        passed_tests INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (run_id) REFERENCES evaluation_runs(run_id)
                    )
                """)
                cursor.execute("""
                    CREATE TABLE individual_test_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        test_id TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        level INTEGER NOT NULL,
                        prompt TEXT,
                        response TEXT,
                        expected TEXT,
                        score REAL,
                        status TEXT NOT NULL,
                        details TEXT,
                        duration_ms INTEGER,
                        model_name TEXT,
                        system_prompt TEXT,
                        system_prompt_mode TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (run_id) REFERENCES evaluation_runs(run_id),
                        FOREIGN KEY (test_id) REFERENCES tests(id)
                    )
                """)

            # Add tool_ids column to domains, levels, tests if they don't exist
            for table in ('domains', 'levels', 'tests'):
                cursor.execute(f"PRAGMA table_info({table})")
                cols = [row[1] for row in cursor.fetchall()]
                if 'tool_ids' not in cols:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN tool_ids TEXT")

            # Add notes column to evaluation_runs if it doesn't exist
            try:
                cursor.execute("ALTER TABLE evaluation_runs ADD COLUMN notes TEXT")
            except sqlite3.OperationalError:
                pass

            # Add status column to evaluation_runs if it doesn't exist
            try:
                cursor.execute("ALTER TABLE evaluation_runs ADD COLUMN status TEXT DEFAULT 'running'")
            except sqlite3.OperationalError:
                pass
            # Backfill: mark runs with completed_at as 'completed', others as 'interrupted'
            cursor.execute(
                "UPDATE evaluation_runs SET status = 'completed' WHERE completed_at IS NOT NULL AND status IS NULL"
            )
            cursor.execute(
                "UPDATE evaluation_runs SET status = 'interrupted' WHERE completed_at IS NULL AND status IS NULL"
            )

            # Add selected_domains column to evaluation_runs if it doesn't exist
            try:
                cursor.execute("ALTER TABLE evaluation_runs ADD COLUMN selected_domains TEXT")
            except sqlite3.OperationalError:
                pass

            # ==================== Agentic Platform Tables ====================

            # Agents table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    system_prompt TEXT,
                    vision_enabled BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migration: add vision_enabled if missing
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN vision_enabled BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            # Migration: add summarization and buffering settings
            for col, defn in [
                ("summarize_threshold", "INTEGER DEFAULT 3"),
                ("summarize_tail", "INTEGER DEFAULT 5"),
                ("summarize_prompt", "TEXT"),
                ("message_buffer_seconds", "REAL DEFAULT 2"),
                ("inject_agent_id", "BOOLEAN DEFAULT 1"),
                ("inject_datetime", "BOOLEAN DEFAULT 1"),
                ("send_intermediate_responses", "BOOLEAN DEFAULT 0"),
                ("outbound_buffer_seconds", "REAL DEFAULT 1.5"),
                ("enable_agent_state", "BOOLEAN DEFAULT 0"),
                ("workspace", "TEXT"),
                ("is_super", "BOOLEAN DEFAULT 0"),
                ("enabled", "BOOLEAN DEFAULT 1"),
                ("default_model_id", "TEXT"),
                ("sandbox_enabled", "BOOLEAN DEFAULT 0"),
                ("attachments_enabled", "BOOLEAN DEFAULT 0"),
                ("attachment_max_size_mb", "INTEGER DEFAULT 20"),
                ("send_file_allowed_path_regex", "TEXT DEFAULT ''"),
                ("audio_enabled", "BOOLEAN DEFAULT 0"),
                ("video_enabled", "BOOLEAN DEFAULT 0"),
                ("enable_atg", "BOOLEAN DEFAULT 0"),
                ("enable_cmp", "BOOLEAN DEFAULT 0"),
                # dm_only: agent hanya melayani pesan pribadi (DM); pesan grup ditolak.
                ("dm_only", "INTEGER DEFAULT 0"),
            ]:
                try:
                    cursor.execute(f"ALTER TABLE agents ADD COLUMN {col} {defn}")
                except sqlite3.OperationalError:
                    pass

            # Migration: add always_execute to decouple CMP from plan/execute mode.
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN always_execute BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            # Migration: add artifacts_enabled (default ON for all agents)
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN artifacts_enabled BOOLEAN DEFAULT 1")
            except sqlite3.OperationalError:
                pass

            # Migration: add last_active_at to track most recently chatted agent
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN last_active_at TIMESTAMP")
            except sqlite3.OperationalError:
                pass

            # Migration: add safety_checker_enabled (default ON)
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN safety_checker_enabled BOOLEAN DEFAULT 1")
            except sqlite3.OperationalError:
                pass

            # Migration: add bash_exec_enabled — web-only "!" direct bash execution.
            # Default OFF for regular agents; backfill the super agent to ON.
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN bash_exec_enabled BOOLEAN DEFAULT 0")
                cursor.execute("UPDATE agents SET bash_exec_enabled = 1 WHERE is_super = 1")
            except sqlite3.OperationalError:
                pass

            # Migration: add primary_channel_id for agent communication routing
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN primary_channel_id TEXT")
            except sqlite3.OperationalError:
                pass

            # Migration: add avatar_path for agent avatar image
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN avatar_path TEXT")
            except sqlite3.OperationalError:
                pass

            # Migration: normalize avatar_path from absolute to relative
            # (makes backup/restore portable across different install paths)
            cursor.execute("SELECT id, avatar_path FROM agents WHERE avatar_path IS NOT NULL AND avatar_path != ''")
            rows = cursor.fetchall()
            for agent_id, avatar_path in rows:
                if os.path.isabs(avatar_path):
                    if 'shared/avatars/' in avatar_path:
                        idx = avatar_path.find('shared/avatars/')
                        rel_path = avatar_path[idx:]
                    else:
                        rel_path = os.path.join('shared', 'avatars', agent_id, os.path.basename(avatar_path))
                    cursor.execute("UPDATE agents SET avatar_path = ? WHERE id = ?", (rel_path, agent_id))

            # Migration: add disable_parallel_tool_execution toggle (default OFF = feature runs)
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN disable_parallel_tool_execution BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            # Migration: add disable_turn_prefetch toggle (default OFF = feature runs)
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN disable_turn_prefetch BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            # Migration: add agent_messaging_enabled toggle (default ON = messaging enabled)
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN agent_messaging_enabled BOOLEAN DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            # Backfill existing agents: NULL → 1 (messaging enabled by default)
            cursor.execute("UPDATE agents SET agent_messaging_enabled = 1 WHERE agent_messaging_enabled IS NULL")

            # Migration: add session_count cache column to eliminate N+1 dashboard queries
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN session_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            # Migration: add fallback_model_id for per-agent model fallback
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN fallback_model_id TEXT")
            except sqlite3.OperationalError:
                pass

            # Migration: add model_id (renamed from default_model_id for clarity)
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN model_id TEXT")
            except sqlite3.OperationalError:
                pass
            # Migrate data: copy default_model_id → model_id if column exists
            try:
                cursor.execute("UPDATE agents SET model_id = default_model_id WHERE model_id IS NULL AND default_model_id IS NOT NULL")
            except sqlite3.OperationalError:
                pass

            # Migration: add tool_compression_enabled for per-agent RTK toggle
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN tool_compression_enabled BOOLEAN DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            # Backfill existing agents: NULL -> 1 (compression enabled by default)
            cursor.execute("UPDATE agents SET tool_compression_enabled = 1 WHERE tool_compression_enabled IS NULL")

            # Migration: add message_wrapper_enabled for per-agent wrapper toggle (default ON)
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN message_wrapper_enabled BOOLEAN DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            # Backfill existing agents: NULL -> 1 (wrapper enabled by default)
            cursor.execute("UPDATE agents SET message_wrapper_enabled = 1 WHERE message_wrapper_enabled IS NULL")

            # Migration: enable inject_agent_id and inject_datetime for all existing agents
            cursor.execute("UPDATE agents SET inject_agent_id = 1 WHERE inject_agent_id = 0 OR inject_agent_id IS NULL")
            cursor.execute("UPDATE agents SET inject_datetime = 1 WHERE inject_datetime = 0 OR inject_datetime IS NULL")

            # Migration: add run_as_user for per-agent local execution user isolation
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN run_as_user TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass

            # Migration: add vision_model_id for per-agent vision model selection
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN vision_model_id TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass

            # Migration: add inter_agent_clear_context for clearing session context on inter-agent message
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN inter_agent_clear_context BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            # Migration: add builtin_tools_enabled to make built-in tools optional per-agent
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN builtin_tools_enabled BOOLEAN DEFAULT 1")
            except sqlite3.OperationalError:
                pass

            # Migration: add messaging_acl and messaging_acl_mode for per-agent outbound ACL
            for col, defn in [
                ("messaging_acl", "TEXT DEFAULT NULL"),
                ("messaging_acl_mode", "TEXT DEFAULT 'whitelist'"),
            ]:
                try:
                    cursor.execute(f"ALTER TABLE agents ADD COLUMN {col} {defn}")
                except sqlite3.OperationalError:
                    pass

            # Migration: per-agent memory settings. NULL = inherit global env
            # (EVONIC_MEMORY_ENGINE / EVOMEM_KB_ORGANIZER), preserving prior behavior.
            #   memory_engine     : '' | 'evomem' | 'fts5'
            #   kb_organizer_mode : '' | 'agentic' | 'non-agentic' | 'sefton' | 'off'
            for col, defn in [
                ("memory_engine", "TEXT DEFAULT NULL"),
                ("kb_organizer_mode", "TEXT DEFAULT NULL"),
            ]:
                try:
                    cursor.execute(f"ALTER TABLE agents ADD COLUMN {col} {defn}")
                except sqlite3.OperationalError:
                    pass

            # Agent Variables (per-agent key-value config used by tools/skills)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_variables (
                    agent_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    is_secret BOOLEAN DEFAULT 0,
                    PRIMARY KEY (agent_id, key),
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
                )
            """)

            # Agent-Tool mapping
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_tools (
                    agent_id TEXT NOT NULL,
                    tool_id TEXT NOT NULL,
                    PRIMARY KEY (agent_id, tool_id),
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
                )
            """)

            # Agent-Skill allowlist (controls which skills an agent can lazy-load via use_skill)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_skills (
                    agent_id TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    PRIMARY KEY (agent_id, skill_id),
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
                )
            """)

            # Channels table (per-agent channel configs; agent_id is NULL for
            # shared channels that serve multiple agents, e.g. whatsapp_shared)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    type TEXT NOT NULL,
                    name TEXT,
                    config TEXT DEFAULT '{}',
                    enabled BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
                    UNIQUE(agent_id, name)
                )
            """)

            # Migration: drop NOT NULL from channels.agent_id so shared channels
            # can exist without a host agent. Guarded by sqlite_master so it only
            # runs while the existing table still has the constraint. Uses
            # create-new/copy/drop/rename (not rename-old-first) so REFERENCES
            # channels clauses in other tables are not rewritten by SQLite.
            row = cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='channels'"
            ).fetchone()
            if row and 'agent_id TEXT NOT NULL' in row[0]:
                try:
                    cursor.execute("DROP TABLE IF EXISTS channels_new")
                    cursor.execute("""
                        CREATE TABLE channels_new (
                            id TEXT PRIMARY KEY,
                            agent_id TEXT,
                            type TEXT NOT NULL,
                            name TEXT,
                            config TEXT DEFAULT '{}',
                            enabled BOOLEAN DEFAULT 1,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
                            UNIQUE(agent_id, name)
                        )
                    """)
                    cursor.execute("""
                        INSERT INTO channels_new (id, agent_id, type, name, config, enabled, created_at, updated_at)
                        SELECT id, agent_id, type, name, config, enabled, created_at, updated_at
                        FROM channels
                    """)
                    cursor.execute("DROP TABLE channels")
                    cursor.execute("ALTER TABLE channels_new RENAME TO channels")
                except sqlite3.OperationalError:
                    pass

            # Migration: detach shared channels from their v1 host agent —
            # they are managed centrally (System Settings → Shared Channel).
            try:
                cursor.execute(
                    "UPDATE channels SET agent_id = NULL WHERE type = 'whatsapp_shared' AND agent_id IS NOT NULL")
            except sqlite3.OperationalError:
                pass

            # Channel pending approvals (pairing code allowlist)
            cursor.execute("""\
                CREATE TABLE IF NOT EXISTS channel_pending_approvals (
                    id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    external_user_id TEXT NOT NULL,
                    user_name TEXT,
                    pair_code TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
                )
            """)

            # Shared-channel inbox: unmapped senders captured for admin
            # assignment (capture-and-assign flow — LIDs are unknowable until
            # a message arrives, so admins annotate instead of typing IDs)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS shared_channel_inbox (
                    id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    external_user_id TEXT NOT NULL,
                    alt_user_id TEXT,
                    push_name TEXT,
                    is_group BOOLEAN DEFAULT 0,
                    group_name TEXT,
                    last_message TEXT,
                    message_count INTEGER DEFAULT 1,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE,
                    UNIQUE(channel_id, external_user_id)
                )
            """)

            # Pending tool-call approvals (for snapshot on SSE reconnect)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_tool_approvals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    tool_args TEXT NOT NULL DEFAULT '{}',
                    approval_info TEXT NOT NULL DEFAULT '{}',
                    reasons TEXT NOT NULL DEFAULT '[]',
                    score INTEGER,
                    source_agent_id TEXT,
                    source_agent_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_tool_approvals_session ON pending_tool_approvals(session_id)")

            # App-level settings (key-value store)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            # Schedules table (global scheduler)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    trigger_config TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_config TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    run_count INTEGER DEFAULT 0,
                    max_runs INTEGER,
                    metadata TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_schedules_owner ON schedules(owner_type, owner_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled)")

            # Schedule execution logs
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schedule_logs (
                    id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    executed_at TEXT NOT NULL,
                    duration_ms INTEGER,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    action_type TEXT NOT NULL,
                    action_summary TEXT,
                    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_schedule_logs_schedule ON schedule_logs(schedule_id, executed_at)")

            # Migration: add action_output column to schedule_logs
            try:
                cursor.execute("ALTER TABLE schedule_logs ADD COLUMN action_output TEXT")
            except sqlite3.OperationalError:
                pass

            # ==================== Providers Table ====================

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS providers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'remote',
                    base_url TEXT,
                    api_key TEXT,
                    api_format TEXT DEFAULT 'openai',
                    enabled BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migrations: add OAuth columns to providers for Codex support
            for col in [
                "auth_type TEXT DEFAULT 'api_key'",
                "refresh_token TEXT",
                "token_expires_at INTEGER",
            ]:
                try:
                    cursor.execute(f"ALTER TABLE providers ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass

            # ==================== LLM Models Table ====================

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS llm_models (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    base_url TEXT,
                    api_key TEXT,
                    model_name TEXT NOT NULL,
                    max_tokens INTEGER DEFAULT 32768,
                    timeout INTEGER DEFAULT 60,
                    thinking BOOLEAN DEFAULT 0,
                    thinking_budget INTEGER DEFAULT 0,
                    temperature REAL DEFAULT NULL,
                    enabled BOOLEAN DEFAULT 1,
                    is_default BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migration: add temperature column to llm_models if missing
            try:
                cursor.execute("ALTER TABLE llm_models ADD COLUMN temperature REAL DEFAULT NULL")
            except sqlite3.OperationalError:
                pass

            # Migration: add model_max_concurrent column to llm_models if missing
            try:
                cursor.execute("ALTER TABLE llm_models ADD COLUMN model_max_concurrent INTEGER DEFAULT 3")
            except sqlite3.OperationalError:
                pass

            # Migration: add api_format column to llm_models if missing
            try:
                cursor.execute("ALTER TABLE llm_models ADD COLUMN api_format TEXT DEFAULT 'openai'")
            except sqlite3.OperationalError:
                pass

            # Migration: add vision_supported column to llm_models if missing
            try:
                cursor.execute("ALTER TABLE llm_models ADD COLUMN vision_supported BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            # Migration: add legacy_id column to llm_models if missing
            try:
                cursor.execute("ALTER TABLE llm_models ADD COLUMN legacy_id TEXT")
            except sqlite3.OperationalError:
                pass

            # Migration: add shortcode column to llm_models if missing
            try:
                cursor.execute("ALTER TABLE llm_models ADD COLUMN shortcode INTEGER")
            except sqlite3.OperationalError:
                pass
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_models_shortcode ON llm_models(shortcode)")

            # Migration: add context_window column to llm_models if missing
            # (0 = unknown; max_tokens is the OUTPUT budget, this is the full window)
            try:
                cursor.execute("ALTER TABLE llm_models ADD COLUMN context_window INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass


            # One-shot migration: rewrite model ids to 'provider/model_name'
            # format (v2). Old ids are kept in legacy_id so stale references
            # still resolve via get_model_by_id. Guarded by a marker so later
            # provider/model_name edits never re-rename ids.
            self._migrate_model_ids_v2(cursor)

            # One-shot migration: seed providers table + assign shortcodes
            self._migrate_seed_providers(cursor)

            # ==================== Attachments Table ====================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    external_user_id TEXT,
                    channel_id TEXT,
                    channel_type TEXT,
                    filename TEXT NOT NULL,
                    original_filename TEXT,
                    mime_type TEXT,
                    file_type TEXT,
                    size_bytes INTEGER,
                    file_path TEXT NOT NULL,
                    telegram_file_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id, agent_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_attachments_created ON attachments(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_attachments_agent ON attachments(agent_id)")

            # Create indexes for faster queries
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tests_domain ON tests(domain_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tests_level ON tests(domain_id, level)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_level_scores_run ON level_scores(run_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_individual_results_run ON individual_test_results(run_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_individual_results_test ON individual_test_results(test_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_channels_agent ON channels(agent_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_channel_pending_pair_code ON channel_pending_approvals(pair_code)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_channel_pending_channel ON channel_pending_approvals(channel_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agents_primary_channel ON agents(primary_channel_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agents_last_active ON agents(last_active_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_eval_runs_model_score ON evaluation_runs(model_name, overall_score, completed_at)")

            # ==================== Workplaces Tables ====================

            # Migration: rename homes → workplaces, home_id → workplace_id
            try:
                cursor.execute("ALTER TABLE homes RENAME TO workplaces")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE tunnel_connectors RENAME COLUMN home_id TO workplace_id")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE agents RENAME COLUMN home_id TO workplace_id")
            except sqlite3.OperationalError:
                pass

            # Workplaces table (execution environments for agents)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS workplaces (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('local', 'remote', 'tunnel', 'bwrap')),
                    config TEXT NOT NULL DEFAULT '{}',
                    status TEXT DEFAULT 'disconnected',
                    error_msg TEXT,
                    last_connected_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)


            # @TODO remove this code if no longer needed in future
            # Migration: rename type cloud to tunnel for existing workplaces.
            # Guarded by sqlite_master check so it only runs when the old
            # CHECK constraint containing 'cloud' still exists.
            row = cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='workplaces'"
            ).fetchone()
            if row and 'cloud' in row[0]:
                try:
                    cursor.execute("DROP TABLE IF EXISTS workplaces_old")
                    cursor.execute("ALTER TABLE workplaces RENAME TO workplaces_old")
                    cursor.execute("""
                        CREATE TABLE workplaces (
                            id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            type TEXT NOT NULL CHECK(type IN ('local', 'remote', 'tunnel')),
                            config TEXT NOT NULL DEFAULT '{}',
                            status TEXT DEFAULT 'disconnected',
                            error_msg TEXT,
                            last_connected_at TEXT,
                            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    cursor.execute("""
                        INSERT INTO workplaces (id, name, type, config, status, error_msg, last_connected_at, created_at, updated_at)
                        SELECT id, name, CASE WHEN type = 'cloud' THEN 'tunnel' ELSE type END, config, status, error_msg, last_connected_at, created_at, updated_at
                        FROM workplaces_old
                    """)
                    cursor.execute("DROP TABLE workplaces_old")
                except sqlite3.OperationalError:
                    pass

            # Migration: add 'bwrap' to the workplaces.type CHECK constraint.
            # Guarded by sqlite_master so it only runs while the existing table's
            # CHECK does not yet include 'bwrap'. Uses create-new/copy/drop/rename
            # (instead of renaming the old table first) so REFERENCES workplaces
            # clauses in tunnel_connectors/agents are not rewritten by SQLite.
            row = cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='workplaces'"
            ).fetchone()
            if row and "'bwrap'" not in row[0]:
                try:
                    cursor.execute("DROP TABLE IF EXISTS workplaces_new")
                    cursor.execute("""
                        CREATE TABLE workplaces_new (
                            id TEXT PRIMARY KEY,
                            name TEXT NOT NULL,
                            type TEXT NOT NULL CHECK(type IN ('local', 'remote', 'tunnel', 'bwrap')),
                            config TEXT NOT NULL DEFAULT '{}',
                            status TEXT DEFAULT 'disconnected',
                            error_msg TEXT,
                            last_connected_at TEXT,
                            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    cursor.execute("""
                        INSERT INTO workplaces_new (id, name, type, config, status, error_msg, last_connected_at, created_at, updated_at)
                        SELECT id, name, type, config, status, error_msg, last_connected_at, created_at, updated_at
                        FROM workplaces
                    """)
                    cursor.execute("DROP TABLE workplaces")
                    cursor.execute("ALTER TABLE workplaces_new RENAME TO workplaces")
                except sqlite3.OperationalError:
                    pass

            # Migration: rename cloud_connectors to tunnel_connectors for existing databases
            try:
                cursor.execute("ALTER TABLE cloud_connectors RENAME TO tunnel_connectors")
            except sqlite3.OperationalError:
                pass
            # Tunnel connectors table (Evonet program pairing records)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tunnel_connectors (
                    id TEXT PRIMARY KEY,
                    workplace_id TEXT NOT NULL REFERENCES workplaces(id) ON DELETE CASCADE,
                    connector_token TEXT UNIQUE,
                    pairing_code TEXT,
                    pairing_expires_at TEXT,
                    device_name TEXT,
                    platform TEXT,
                    version TEXT,
                    last_seen_at TEXT
                )
            """)

            # Migration: add workplace_id to agents
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN workplace_id TEXT REFERENCES workplaces(id)")
            except sqlite3.OperationalError:
                pass

            # Migration: slash command visibility control
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN hidden_slash_commands TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE agents ADD COLUMN disabled_slash_commands TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass

            # Migration: relax connector_token NOT NULL so NULL is allowed before pairing completes
            try:
                col_info = cursor.execute("PRAGMA table_info(tunnel_connectors)").fetchall()
                token_col = next((c for c in col_info if c[1] == 'connector_token'), None)
                if token_col and token_col[3] == 1:  # notnull == 1
                    cursor.execute("ALTER TABLE tunnel_connectors RENAME TO tunnel_connectors_old")
                    cursor.execute("""
                        CREATE TABLE tunnel_connectors (
                            id TEXT PRIMARY KEY,
                            workplace_id TEXT NOT NULL REFERENCES workplaces(id) ON DELETE CASCADE,
                            connector_token TEXT UNIQUE,
                            pairing_code TEXT,
                            pairing_expires_at TEXT,
                            device_name TEXT,
                            platform TEXT,
                            version TEXT,
                            last_seen_at TEXT
                        )
                    """)
                    cursor.execute("""
                        INSERT INTO tunnel_connectors
                        SELECT id, workplace_id,
                               CASE WHEN connector_token = '' THEN NULL ELSE connector_token END,
                               pairing_code, pairing_expires_at, device_name, platform, version, last_seen_at
                        FROM tunnel_connectors_old
                    """)
                    cursor.execute("DROP TABLE tunnel_connectors_old")
            except sqlite3.OperationalError:
                pass

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_workplaces_type ON workplaces(type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tunnel_connectors_workplace ON tunnel_connectors(workplace_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tunnel_connectors_token ON tunnel_connectors(connector_token)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agents_workplace ON agents(workplace_id)")

            # ==================== Portals Table ====================

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS portals (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    virtual_path TEXT NOT NULL,
                    backend_type TEXT NOT NULL CHECK(backend_type IN ('local', 'ssh', 'evonet')),
                    backend_config TEXT NOT NULL DEFAULT '{}',
                    real_path TEXT NOT NULL,
                    status TEXT DEFAULT 'disconnected',
                    error_msg TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
                )
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_portals_agent ON portals(agent_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_portals_backend_type ON portals(backend_type)")

            # ==================== Transfer Jobs Table ====================

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transfer_jobs (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    dest_path TEXT NOT NULL,
                    source_backend_type TEXT NOT NULL,
                    dest_backend_type TEXT NOT NULL,
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
                    total_bytes INTEGER DEFAULT 0,
                    bytes_transferred INTEGER DEFAULT 0,
                    error_msg TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
                )
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfer_jobs_agent ON transfer_jobs(agent_id)")

            # ==================== HMADS Safety Rules Table ====================

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS safety_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    pattern TEXT NOT NULL,
                    pattern_type TEXT DEFAULT 'regex',
                    weight INTEGER NOT NULL DEFAULT 5,
                    category TEXT NOT NULL,
                    tool_scope TEXT DEFAULT 'all',
                    scope TEXT DEFAULT 'global',
                    enabled BOOLEAN DEFAULT 1,
                    is_system BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Migration: add scope column for existing safety_rules tables (replaces agent_id)
            try:
                cursor.execute("ALTER TABLE safety_rules ADD COLUMN scope TEXT DEFAULT 'global'")
            except sqlite3.OperationalError:
                pass

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_safety_rules_enabled ON safety_rules(enabled)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_safety_rules_scope ON safety_rules(scope)")

            # Agent ↔ Safety Rule assignment (many-to-many, for scope='specific' rules)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_safety_rules (
                    agent_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    PRIMARY KEY (agent_id, rule_id),
                    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
                    FOREIGN KEY (rule_id) REFERENCES safety_rules(id) ON DELETE CASCADE
                )
            """)

            # Session Index — materialized view of per-agent chat sessions in main DB.
            # Eliminates cross-DB ATTACH/UNION ALL for session aggregation queries.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS session_index (
                    session_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    external_user_id TEXT NOT NULL,
                    channel_id TEXT,
                    bot_enabled INTEGER DEFAULT 1,
                    archived INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    message_count INTEGER DEFAULT 0,
                    last_message TEXT,
                    last_message_role TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_index_agent ON session_index(agent_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_index_updated ON session_index(updated_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_index_external ON session_index(external_user_id)")

            # Durable correlation for delegated-agent questions sent to a human.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_escalations (
                    id TEXT PRIMARY KEY,
                    requesting_agent_id TEXT NOT NULL,
                    requesting_session_id TEXT NOT NULL,
                    originating_agent_id TEXT NOT NULL,
                    originating_session_id TEXT NOT NULL,
                    delivery_session_id TEXT NOT NULL,
                    external_user_id TEXT NOT NULL,
                    channel_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    metadata TEXT DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    answered_at REAL
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_escalations_pending "
                "ON user_escalations(originating_session_id, status, created_at)"
            )

            # ==================== System Alerts Table ====================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL UNIQUE,
                    agent_id TEXT,
                    message TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'error',
                    created_at REAL NOT NULL,
                    dismissed_at REAL DEFAULT NULL
                )
            """)

            conn.commit()

        # Migrate chat data from main DB to per-agent DBs
        self._migrate_chat_to_agent_dbs()

        # Backfill session_count for existing agents (idempotent)
        self._backfill_session_counts()

        # Populate session_index from per-agent chat DBs (idempotent one-time migration)
        self._populate_session_index()

    def _migrate_model_ids_v2(self, cursor):
        """One-shot rename of llm_models ids to 'provider/model_name' format.

        Cascades to agents model columns and app_settings model-id keys.
        Old ids are preserved in llm_models.legacy_id. Guarded by the
        app_settings marker 'model_id_format' = 'v2'.
        """
        cursor.execute("SELECT value FROM app_settings WHERE key = 'model_id_format'")
        row = cursor.fetchone()
        if row and row[0] == 'v2':
            return

        cursor.execute("SELECT id, provider, model_name FROM llm_models")
        rows = cursor.fetchall()
        all_old_ids = {r[0] for r in rows}
        used = set()
        renames = []
        # Pass 1: rows already in final form keep their id (reserve it)
        for old_id, provider, model_name in rows:
            if old_id == f"{provider or 'custom'}/{model_name or ''}".rstrip('/'):
                used.add(old_id)
        # Pass 2: assign new ids, avoiding reserved/assigned ids AND other
        # rows' current ids (SQLite checks the PK per UPDATE statement)
        for old_id, provider, model_name in rows:
            base = f"{provider or 'custom'}/{model_name or ''}".rstrip('/')
            if old_id == base:
                continue
            candidate, n = base, 2
            while candidate in used or candidate in (all_old_ids - {old_id}):
                candidate = f"{base}-{n}"
                n += 1
            used.add(candidate)
            renames.append((old_id, candidate))

        for old_id, new_id in renames:
            cursor.execute(
                "UPDATE llm_models SET id = ?, legacy_id = ? WHERE id = ?",
                (new_id, old_id, old_id),
            )
            for col in ("model_id", "fallback_model_id", "vision_model_id", "default_model_id"):
                try:
                    cursor.execute(
                        f"UPDATE agents SET {col} = ? WHERE {col} = ?",
                        (new_id, old_id),
                    )
                except sqlite3.OperationalError:
                    pass  # legacy column may have been dropped
            for key in ("vision_model_id", "kb_organizer_model_id",
                        "task_classifier_model_id", "audio_model_id"):
                cursor.execute(
                    "UPDATE app_settings SET value = ? WHERE key = ? AND value = ?",
                    (new_id, key, old_id),
                )

        cursor.execute(
            "INSERT INTO app_settings (key, value) VALUES ('model_id_format', 'v2') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        # Raw SQL above bypasses set_setting's cache invalidation
        self.invalidate_settings_cache()

    def _migrate_seed_providers(self, cursor):
        """One-shot: seed providers table from PROVIDER_DEFAULTS + existing models,
        and assign shortcodes to models that don't have one."""
        cursor.execute("SELECT value FROM app_settings WHERE key = 'providers_seeded'")
        row = cursor.fetchone()
        if row and row[0] == '1':
            return

        _BUILTIN_PROVIDERS = {
            "openrouter": ("OpenRouter", "remote", "https://openrouter.ai/api/v1", "openai"),
            "togetherai": ("Together AI", "remote", "https://api.together.xyz/v1", "openai"),
            "ollama": ("Ollama", "local", "http://localhost:11434/v1", "openai"),
            "ollama_cloud": ("Ollama Cloud", "remote", "https://ollama.com/api", "ollama"),
            "opencode_zen": ("OpenCode Zen", "remote", "https://opencode.ai/zen/v1", "openai"),
            "opencode_go": ("OpenCode Go", "remote", "https://opencode.ai/zen/go/v1", "openai"),
            "deepseek": ("DeepSeek", "remote", "https://api.deepseek.com", "openai"),
            "llama.cpp": ("llama.cpp", "local", "http://localhost:8080/v1", "openai"),
            "custom": ("Custom", "remote", "", "openai"),
        }

        for pid, (name, ptype, base_url, api_fmt) in _BUILTIN_PROVIDERS.items():
            cursor.execute(
                "INSERT OR IGNORE INTO providers (id, name, type, base_url, api_format) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, name, ptype, base_url, api_fmt),
            )

        # Create provider rows for any provider values in existing models
        # that aren't in PROVIDER_DEFAULTS (e.g. user-created "custom" entries)
        cursor.execute("SELECT DISTINCT provider FROM llm_models WHERE provider NOT IN (SELECT id FROM providers)")
        for (prov,) in cursor.fetchall():
            cursor.execute(
                "INSERT OR IGNORE INTO providers (id, name, type) VALUES (?, ?, 'remote')",
                (prov, prov),
            )

        # Assign shortcodes to existing models ordered by provider, then name
        cursor.execute(
            "SELECT id FROM llm_models WHERE shortcode IS NULL ORDER BY provider, name"
        )
        model_ids = [r[0] for r in cursor.fetchall()]
        if model_ids:
            cursor.execute("SELECT COALESCE(MAX(shortcode), 0) FROM llm_models")
            next_code = cursor.fetchone()[0] + 1
            for mid in model_ids:
                cursor.execute(
                    "UPDATE llm_models SET shortcode = ? WHERE id = ?",
                    (next_code, mid),
                )
                next_code += 1

        cursor.execute(
            "INSERT INTO app_settings (key, value) VALUES ('providers_seeded', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        self.invalidate_settings_cache()

    def _populate_session_index(self):
        """One-time migration: populate session_index from all per-agent chat DBs.

        Idempotent — uses INSERT OR REPLACE so it is safe to run multiple times.
        Called lazily from _init_tables, not at import time.
        """
        import logging
        import os
        from models.chat import AGENTS_DIR, agent_chat_manager
        logger = logging.getLogger(__name__)
        agents = self.get_agents()
        with self._connect() as conn:
            for agent in agents:
                aid = agent['id']
                db_path = os.path.join(AGENTS_DIR, aid, 'chat.db')
                if not os.path.exists(db_path):
                    continue
                try:
                    chat_db = agent_chat_manager.get(aid)
                    sessions = chat_db.get_sessions_with_preview()
                    for s in sessions:
                        conn.execute("""
                            INSERT OR REPLACE INTO session_index
                                (session_id, agent_id, external_user_id, channel_id,
                                 bot_enabled, archived, created_at, updated_at,
                                 message_count, last_message, last_message_role)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            s['id'], s.get('agent_id', aid), s.get('external_user_id', ''),
                            s.get('channel_id'), s.get('bot_enabled', 1),
                            s.get('archived', 0), s.get('created_at'), s.get('updated_at'),
                            s.get('message_count', 0), s.get('last_message'),
                            s.get('last_message_role'),
                        ))
                    conn.commit()
                except Exception as e:
                    logger.warning("Failed to populate session_index for agent %s: %s", aid, e)

    def _backfill_session_counts(self):
        """One-time backfill: compute session_count for all agents from per-agent chat DBs."""
        import os
        from models.chat import AGENTS_DIR, agent_chat_manager
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM agents")
            agent_ids = [row[0] for row in cursor.fetchall()]
        for aid in agent_ids:
            try:
                chat_db = agent_chat_manager.get(aid)
                sc, _ = chat_db.get_counts()
                with self._connect() as conn:
                    conn.execute("UPDATE agents SET session_count = ? WHERE id = ?", (sc, aid))
                    conn.commit()
            except Exception:
                pass

    def _migrate_chat_to_agent_dbs(self):
        """One-time migration: move chat_sessions/chat_messages from main DB to per-agent DBs."""
        from models.chat import agent_chat_manager
        with self._connect() as conn:
            cursor = conn.cursor()
            # Check if old tables exist
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_sessions'")
            if not cursor.fetchone():
                return
            cursor.execute("SELECT COUNT(*) FROM chat_sessions")
            if cursor.fetchone()[0] == 0:
                cursor.execute("DROP TABLE IF EXISTS chat_messages")
                cursor.execute("DROP TABLE IF EXISTS chat_sessions")
                conn.commit()
                return

            print("[DB] Migrating chat data to per-agent databases...")
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT agent_id FROM chat_sessions")
            agent_ids = [r['agent_id'] for r in cursor.fetchall()]

            for aid in agent_ids:
                chat_db = agent_chat_manager.get(aid)
                # No LIMIT: one-time migration needs all sessions for this agent
                cursor.execute("SELECT id, agent_id, channel_id, external_user_id, bot_enabled, archived, created_at, updated_at FROM chat_sessions WHERE agent_id = ?", (aid,))
                sessions = [dict(r) for r in cursor.fetchall()]
                for s in sessions:
                    with chat_db._connect() as aconn:
                        ac = aconn.cursor()
                        ac.execute("""
                            INSERT OR IGNORE INTO chat_sessions
                            (id, agent_id, channel_id, external_user_id, bot_enabled, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (s['id'], s['agent_id'], s.get('channel_id'), s['external_user_id'],
                              s.get('bot_enabled', 1), s['created_at'], s['updated_at']))
                        aconn.commit()

                    # No LIMIT: one-time migration needs all messages for this session
                    cursor.execute("SELECT id, session_id, role, content, tool_calls, tool_call_id, metadata, created_at FROM chat_messages WHERE session_id = ?", (s['id'],))
                    messages = [dict(r) for r in cursor.fetchall()]
                    if messages:
                        with chat_db._connect() as aconn:
                            ac = aconn.cursor()
                            for m in messages:
                                ac.execute("""
                                    INSERT OR IGNORE INTO chat_messages
                                    (id, session_id, role, content, tool_calls, tool_call_id, created_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                """, (m['id'], m['session_id'], m['role'], m.get('content'),
                                      m.get('tool_calls'), m.get('tool_call_id'), m['created_at']))
                            aconn.commit()

            cursor.execute("DROP TABLE IF EXISTS chat_messages")
            cursor.execute("DROP TABLE IF EXISTS chat_sessions")
            conn.commit()
            print(f"[DB] Migration complete: {len(agent_ids)} agent(s) migrated.")

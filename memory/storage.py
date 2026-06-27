import sqlite3
from pathlib import Path

from memory.identity import legacy_identity
from memory.models import FACT_INVALID, STATUS_PENDING


class MemoryStorage:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path))

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS viewer_profiles (
                    identity_key TEXT PRIMARY KEY,
                    display_name TEXT,
                    viewer_name TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    last_session_id TEXT,
                    source_type TEXT,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS viewer_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    viewer_name TEXT NOT NULL,
                    identity_key TEXT,
                    source_type TEXT,
                    message_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    fact_status TEXT NOT NULL DEFAULT 'pending',
                    summary_status TEXT NOT NULL DEFAULT 'pending',
                    fact_batch_id TEXT,
                    summary_batch_id TEXT,
                    fact_extracted_at TEXT,
                    summary_extracted_at TEXT
                );

                CREATE TABLE IF NOT EXISTS viewer_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    viewer_name TEXT NOT NULL,
                    identity_key TEXT,
                    category TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    confidence REAL,
                    source_message_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    source TEXT NOT NULL DEFAULT 'llm'
                );

                CREATE TABLE IF NOT EXISTS viewer_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    viewer_name TEXT NOT NULL,
                    identity_key TEXT,
                    session_id TEXT,
                    start_message_id INTEGER,
                    end_message_id INTEGER,
                    summary_text TEXT NOT NULL,
                    keywords_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    agent_name TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    message_kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    fact_status TEXT NOT NULL DEFAULT 'pending',
                    summary_status TEXT NOT NULL DEFAULT 'pending',
                    fact_batch_id TEXT,
                    summary_batch_id TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    confidence REAL,
                    source_message_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    source TEXT NOT NULL DEFAULT 'llm'
                );

                CREATE TABLE IF NOT EXISTS agent_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    agent_name TEXT NOT NULL,
                    start_message_id INTEGER,
                    end_message_id INTEGER,
                    summary_text TEXT NOT NULL,
                    keywords_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_viewer_messages_identity ON viewer_messages(identity_key);
                CREATE INDEX IF NOT EXISTS idx_viewer_facts_identity ON viewer_facts(identity_key);
                CREATE INDEX IF NOT EXISTS idx_viewer_summaries_identity ON viewer_summaries(identity_key);
                """
            )

    def _has_column(self, conn, table, column):
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row["name"] == column for row in rows)

    def migrate_to_identity_key(self):
        """把旧的「按昵称存」的库平滑迁移到「按 identity_key 存」。幂等。

        注意：必须先给旧表补 identity_key 列，再调 init_db()——因为 init_db
        会建引用 identity_key 列的索引，列不存在时会报错。
        """
        legacy_prefix = legacy_identity("")  # 即 "legacy:"
        with self._connect() as conn:
            # 1. 给三张消息/事实/摘要表补 identity_key 列并回填 legacy 键（仅当表已存在）
            for table in ("viewer_messages", "viewer_facts", "viewer_summaries"):
                if not self._table_exists(conn, table):
                    continue
                if not self._has_column(conn, table, "identity_key"):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN identity_key TEXT")
                conn.execute(
                    f"UPDATE {table} SET identity_key = ? || viewer_name "
                    f"WHERE identity_key IS NULL OR identity_key = ''",
                    (legacy_prefix,),
                )

            # 2. viewer_profiles 需要把主键从 viewer_name 换成 identity_key —— 重建表
            if self._table_exists(conn, "viewer_profiles") and not self._has_column(
                conn, "viewer_profiles", "identity_key"
            ):
                conn.execute("ALTER TABLE viewer_profiles RENAME TO viewer_profiles_old")
                conn.execute(
                    """
                    CREATE TABLE viewer_profiles (
                        identity_key TEXT PRIMARY KEY,
                        display_name TEXT,
                        viewer_name TEXT,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        message_count INTEGER NOT NULL DEFAULT 0,
                        last_session_id TEXT,
                        source_type TEXT,
                        notes TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO viewer_profiles (
                        identity_key, display_name, viewer_name, first_seen_at,
                        last_seen_at, message_count, last_session_id, source_type, notes
                    )
                    SELECT ? || viewer_name, viewer_name, viewer_name,
                           first_seen_at, last_seen_at, message_count, last_session_id,
                           source_type, notes
                    FROM viewer_profiles_old
                    """,
                    (legacy_prefix,),
                )
                conn.execute("DROP TABLE viewer_profiles_old")

        # 3. 列就位后再确保完整 schema（agent 表 + 索引）
        self.init_db()

    def _table_exists(self, conn, table_name):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def table_exists(self, table_name):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                (table_name,),
            ).fetchone()
        return row is not None

    def upsert_viewer_profile(self, identity_key, display_name, source_type, session_id, created_at):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT identity_key, message_count FROM viewer_profiles WHERE identity_key = ?",
                (identity_key,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO viewer_profiles (
                        identity_key, display_name, viewer_name, first_seen_at, last_seen_at,
                        message_count, last_session_id, source_type, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (identity_key, display_name, display_name, created_at, created_at, 1,
                     session_id, source_type, None),
                )
            else:
                conn.execute(
                    """
                    UPDATE viewer_profiles
                    SET last_seen_at = ?, message_count = ?, last_session_id = ?,
                        source_type = ?, display_name = ?, viewer_name = ?
                    WHERE identity_key = ?
                    """,
                    (created_at, existing["message_count"] + 1, session_id, source_type,
                     display_name, display_name, identity_key),
                )

    def get_viewer_profile(self, identity_key):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM viewer_profiles WHERE identity_key = ?",
                (identity_key,),
            ).fetchone()
        return dict(row) if row else None

    def insert_viewer_message(self, session_id, identity_key, display_name, source_type, message_text, created_at):
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO viewer_messages (
                    session_id, identity_key, viewer_name, source_type, message_text, created_at,
                    fact_status, summary_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    identity_key,
                    display_name,
                    source_type,
                    message_text,
                    created_at,
                    STATUS_PENDING,
                    STATUS_PENDING,
                ),
            )
        return cur.lastrowid

    def get_viewer_message_identity(self, message_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT identity_key FROM viewer_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        return row["identity_key"] if row else None

    def find_legacy_identities_by_name(self, display_name):
        """找出昵称匹配的 legacy 身份（identity_key 形如 legacy:{昵称}）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT identity_key FROM viewer_profiles
                WHERE identity_key LIKE 'legacy:%'
                  AND (display_name = ? OR viewer_name = ?)
                """,
                (display_name, display_name),
            ).fetchall()
        return [row["identity_key"] for row in rows]

    def claim_legacy_identity(self, legacy_key, target_identity_key, display_name):
        """把 legacy 身份下的 messages/facts/summaries 改挂到 target_identity_key，并合并 profile。
        返回合并报告 dict。仅供「唯一匹配」时调用（调用方保证）。"""
        report = {
            "claimed": False, "legacy_key": legacy_key,
            "target": target_identity_key, "messages": 0, "facts": 0, "summaries": 0,
        }
        col_by_table = {
            "viewer_messages": "messages",
            "viewer_facts": "facts",
            "viewer_summaries": "summaries",
        }
        with self._connect() as conn:
            for table, report_key in col_by_table.items():
                cur = conn.execute(
                    f"UPDATE {table} SET identity_key = ? WHERE identity_key = ?",
                    (target_identity_key, legacy_key),
                )
                report[report_key] = cur.rowcount
            # 合并 profile：把 legacy 的计数并入 target，删除 legacy profile
            legacy = conn.execute(
                "SELECT * FROM viewer_profiles WHERE identity_key = ?", (legacy_key,)
            ).fetchone()
            target = conn.execute(
                "SELECT * FROM viewer_profiles WHERE identity_key = ?", (target_identity_key,)
            ).fetchone()
            if legacy is not None:
                if target is None:
                    conn.execute(
                        """
                        INSERT INTO viewer_profiles (identity_key, display_name, viewer_name,
                            first_seen_at, last_seen_at, message_count, last_session_id, source_type, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (target_identity_key, display_name, display_name,
                         legacy["first_seen_at"], legacy["last_seen_at"], legacy["message_count"],
                         legacy["last_session_id"], legacy["source_type"], legacy["notes"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE viewer_profiles
                        SET message_count = message_count + ?,
                            first_seen_at = MIN(first_seen_at, ?),
                            display_name = ?
                        WHERE identity_key = ?
                        """,
                        (legacy["message_count"], legacy["first_seen_at"],
                         display_name, target_identity_key),
                    )
                conn.execute("DELETE FROM viewer_profiles WHERE identity_key = ?", (legacy_key,))
            report["claimed"] = True
        return report

    def list_viewer_messages(self, identity_key):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM viewer_messages WHERE identity_key = ? ORDER BY id ASC",
                (identity_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_agent_message(self, session_id, agent_name, message_text, message_kind, created_at):
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO agent_messages (
                    session_id, agent_name, message_text, message_kind, created_at,
                    fact_status, summary_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_name,
                    message_text,
                    message_kind,
                    created_at,
                    STATUS_PENDING,
                    STATUS_PENDING,
                ),
            )
        return cur.lastrowid

    def list_agent_messages(self, agent_name):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_messages WHERE agent_name = ? ORDER BY id ASC",
                (agent_name,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_viewer_fact(
        self,
        identity_key,
        display_name,
        category,
        fact_key,
        fact_value,
        confidence,
        source_message_id,
        status,
        created_at,
        source="llm",
    ):
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO viewer_facts (
                    identity_key, viewer_name, category, fact_key, fact_value, confidence,
                    source_message_id, status, created_at, updated_at, invalidated_at,
                    source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity_key,
                    display_name,
                    category,
                    fact_key,
                    fact_value,
                    confidence,
                    source_message_id,
                    status,
                    created_at,
                    created_at,
                    None,
                    source,
                ),
            )
        return cur.lastrowid

    def has_active_viewer_fact(self, identity_key, category, fact_key):
        """检查同三元组下是否存在任意来源的 active fact（用于确定性兜底写入前去重）。"""
        with self._connect() as conn:
            r = conn.execute(
                """
                SELECT 1 FROM viewer_facts
                WHERE identity_key = ? AND category = ? AND fact_key = ?
                  AND status = 'active'
                LIMIT 1
                """,
                (identity_key, category, fact_key),
            ).fetchone()
        return r is not None

    def list_guard_messages(self, session_id=None):
        """列出上舰消息（source_type='guard_buy'）。session_id=None 时扫全库。"""
        with self._connect() as conn:
            if session_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM viewer_messages
                    WHERE source_type = 'guard_buy'
                    ORDER BY id ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM viewer_messages
                    WHERE source_type = 'guard_buy' AND session_id = ?
                    ORDER BY id ASC
                    """,
                    (session_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def has_manual_viewer_fact(self, identity_key, category, fact_key):
        """检查同三元组下是否存在 manual 来源的 active fact。
        如果有，LLM 抽出的新 fact 应当跳过覆盖以保护人工修正。"""
        with self._connect() as conn:
            r = conn.execute(
                """
                SELECT 1 FROM viewer_facts
                WHERE identity_key = ? AND category = ? AND fact_key = ?
                  AND status = 'active' AND source = 'manual'
                LIMIT 1
                """,
                (identity_key, category, fact_key),
            ).fetchone()
        return r is not None

    def add_viewer_summary(
        self,
        identity_key,
        display_name,
        session_id,
        start_message_id,
        end_message_id,
        summary_text,
        keywords_json,
        created_at,
    ):
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO viewer_summaries (
                    identity_key, viewer_name, session_id, start_message_id, end_message_id,
                    summary_text, keywords_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity_key,
                    display_name,
                    session_id,
                    start_message_id,
                    end_message_id,
                    summary_text,
                    keywords_json,
                    created_at,
                ),
            )
        return cur.lastrowid

    def add_agent_fact(
        self,
        agent_name,
        category,
        fact_key,
        fact_value,
        confidence,
        source_message_id,
        status,
        created_at,
        source="llm",
    ):
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO agent_facts (
                    agent_name, category, fact_key, fact_value, confidence,
                    source_message_id, status, created_at, updated_at, invalidated_at,
                    source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_name,
                    category,
                    fact_key,
                    fact_value,
                    confidence,
                    source_message_id,
                    status,
                    created_at,
                    created_at,
                    None,
                    source,
                ),
            )
        return cur.lastrowid

    def has_manual_agent_fact(self, agent_name, category, fact_key):
        """检查同三元组下是否存在 manual 来源的 active agent fact。"""
        with self._connect() as conn:
            r = conn.execute(
                """
                SELECT 1 FROM agent_facts
                WHERE agent_name = ? AND category = ? AND fact_key = ?
                  AND status = 'active' AND source = 'manual'
                LIMIT 1
                """,
                (agent_name, category, fact_key),
            ).fetchone()
        return r is not None

    def add_agent_summary(
        self,
        session_id,
        agent_name,
        start_message_id,
        end_message_id,
        summary_text,
        keywords_json,
        created_at,
    ):
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO agent_summaries (
                    session_id, agent_name, start_message_id, end_message_id,
                    summary_text, keywords_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_name,
                    start_message_id,
                    end_message_id,
                    summary_text,
                    keywords_json,
                    created_at,
                ),
            )
        return cur.lastrowid

    def list_active_viewer_facts(self, identity_key, limit=6):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM viewer_facts
                WHERE identity_key = ? AND status = 'active'
                ORDER BY updated_at DESC, confidence DESC, id DESC
                LIMIT ?
                """,
                (identity_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_active_agent_facts(self, agent_name, limit_per_category=3):
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY category
                               ORDER BY updated_at DESC, confidence DESC, id DESC
                           ) AS rn
                    FROM agent_facts
                    WHERE agent_name = ? AND status = 'active'
                )
                SELECT * FROM ranked
                WHERE rn <= ?
                ORDER BY updated_at DESC, confidence DESC, id DESC
                """,
                (agent_name, limit_per_category),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_viewer_summaries(self, identity_key):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM viewer_summaries
                WHERE identity_key = ?
                ORDER BY created_at DESC, id DESC
                """,
                (identity_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_agent_summaries(self, agent_name, session_limit=5):
        with self._connect() as conn:
            recent_sessions = conn.execute(
                """
                SELECT DISTINCT session_id
                FROM agent_summaries
                WHERE agent_name = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (agent_name, session_limit),
            ).fetchall()
            session_ids = [row["session_id"] for row in recent_sessions]
            if not session_ids:
                return []
            placeholders = ",".join("?" for _ in session_ids)
            rows = conn.execute(
                f"""
                SELECT * FROM agent_summaries
                WHERE agent_name = ? AND session_id IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                """,
                (agent_name, *session_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_viewer_messages(self, identity_key, limit=3):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM viewer_messages
                WHERE identity_key = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (identity_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_pending_viewer_messages_for_fact(self, session_id):
        return self._list_pending_viewer_messages(session_id, "fact_status")

    def list_pending_viewer_messages_for_summary(self, session_id):
        return self._list_pending_viewer_messages(session_id, "summary_status")

    def list_pending_agent_messages_for_fact(self, session_id):
        return self._list_pending_agent_messages(session_id, "fact_status")

    def list_pending_agent_messages_for_summary(self, session_id):
        return self._list_pending_agent_messages(session_id, "summary_status")

    def _list_pending_viewer_messages(self, session_id, status_column):
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM viewer_messages
                WHERE session_id = ? AND {status_column} = 'pending'
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _list_pending_agent_messages(self, session_id, status_column):
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM agent_messages
                WHERE session_id = ? AND {status_column} = 'pending'
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_viewer_messages_fact_status(self, message_ids, status, batch_id=None, extracted_at=None):
        self._mark_viewer_messages_status(
            message_ids=message_ids,
            status_column="fact_status",
            status=status,
            batch_column="fact_batch_id",
            batch_id=batch_id,
            extracted_column="fact_extracted_at",
            extracted_at=extracted_at,
        )

    def mark_viewer_messages_summary_status(self, message_ids, status, batch_id=None, extracted_at=None):
        self._mark_viewer_messages_status(
            message_ids=message_ids,
            status_column="summary_status",
            status=status,
            batch_column="summary_batch_id",
            batch_id=batch_id,
            extracted_column="summary_extracted_at",
            extracted_at=extracted_at,
        )

    def mark_agent_messages_fact_status(self, message_ids, status, batch_id=None):
        self._mark_agent_messages_status(
            message_ids=message_ids,
            status_column="fact_status",
            status=status,
            batch_column="fact_batch_id",
            batch_id=batch_id,
        )

    def mark_agent_messages_summary_status(self, message_ids, status, batch_id=None):
        self._mark_agent_messages_status(
            message_ids=message_ids,
            status_column="summary_status",
            status=status,
            batch_column="summary_batch_id",
            batch_id=batch_id,
        )

    def invalidate_viewer_fact(self, identity_key, category, fact_key, invalidated_at):
        # 不 invalidate source='manual'：保护人工修正不被 LLM 自动覆盖
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE viewer_facts
                SET status = ?, invalidated_at = ?, updated_at = ?
                WHERE identity_key = ? AND category = ? AND fact_key = ?
                  AND status = 'active' AND source != 'manual'
                """,
                (FACT_INVALID, invalidated_at, invalidated_at, identity_key, category, fact_key),
            )

    def invalidate_agent_fact(self, agent_name, category, fact_key, invalidated_at):
        # 不 invalidate source='manual'：保护人工修正不被 LLM 自动覆盖
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_facts
                SET status = ?, invalidated_at = ?, updated_at = ?
                WHERE agent_name = ? AND category = ? AND fact_key = ?
                  AND status = 'active' AND source != 'manual'
                """,
                (FACT_INVALID, invalidated_at, invalidated_at, agent_name, category, fact_key),
            )

    def prune_agent_active_facts(self, agent_name, category, keep_limit, invalidated_at):
        # agent fact 配额裁剪时，manual 来源不计入 prune，永远保留 active
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM agent_facts
                WHERE agent_name = ? AND category = ? AND status = 'active'
                  AND source != 'manual'
                ORDER BY updated_at DESC, confidence DESC, id DESC
                """,
                (agent_name, category),
            ).fetchall()
            overflow_ids = [row["id"] for row in rows[keep_limit:]]
            if not overflow_ids:
                return
            placeholders = ",".join("?" for _ in overflow_ids)
            conn.execute(
                f"""
                UPDATE agent_facts
                SET status = ?, invalidated_at = ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (FACT_INVALID, invalidated_at, invalidated_at, *overflow_ids),
            )

    def _mark_viewer_messages_status(
        self,
        message_ids,
        status_column,
        status,
        batch_column,
        batch_id,
        extracted_column,
        extracted_at,
    ):
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE viewer_messages
                SET {status_column} = ?, {batch_column} = ?, {extracted_column} = ?
                WHERE id IN ({placeholders})
                """,
                (status, batch_id, extracted_at, *message_ids),
            )

    def _mark_agent_messages_status(self, message_ids, status_column, status, batch_column, batch_id):
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE agent_messages
                SET {status_column} = ?, {batch_column} = ?
                WHERE id IN ({placeholders})
                """,
                (status, batch_id, *message_ids),
            )

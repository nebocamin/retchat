"""Database layer for Retchat using SQLite."""

import os
import sqlite3
import time
from typing import Any, Dict, List, Optional


class Database:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            data_dir = os.path.expanduser("~/.local/share/retchat")
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(data_dir, "retchat.db")
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # Conversations table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    destination_hash TEXT PRIMARY KEY,
                    identity_hash TEXT,
                    display_name TEXT,
                    custom_name TEXT,
                    last_message_text TEXT DEFAULT '',
                    last_message_time REAL DEFAULT 0,
                    unread_count INTEGER DEFAULT 0,
                    is_pinned INTEGER DEFAULT 0,
                    hops INTEGER DEFAULT 0,
                    created_at REAL
                )
            """)

            # Messages table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_hash TEXT UNIQUE,
                    conversation_hash TEXT NOT NULL,
                    sender_hash TEXT NOT NULL,
                    recipient_hash TEXT NOT NULL,
                    is_outgoing INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    state INTEGER DEFAULT 0,
                    hops INTEGER DEFAULT 0,
                    fields_json TEXT,
                    FOREIGN KEY (conversation_hash) REFERENCES conversations(destination_hash)
                )
            """)

            # Discovered Announces table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS announces (
                    destination_hash TEXT PRIMARY KEY,
                    identity_hash TEXT,
                    display_name TEXT,
                    hops INTEGER DEFAULT 0,
                    aspect TEXT,
                    receiving_interface TEXT,
                    last_seen REAL
                )
            """)

            # Settings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # Create indexes for fast lookups
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_hash)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_announces_time ON announces(last_seen)")
            conn.commit()

    # --- Settings ---
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value)
            )
            conn.commit()

    # --- Conversations ---
    def get_conversations(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            if query:
                q = f"%{query.strip().lower()}%"
                rows = conn.execute("""
                    SELECT * FROM conversations
                    WHERE lower(destination_hash) LIKE ?
                       OR lower(COALESCE(custom_name, '')) LIKE ?
                       OR lower(COALESCE(display_name, '')) LIKE ?
                    ORDER BY is_pinned DESC, last_message_time DESC, created_at DESC
                """, (q, q, q)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM conversations
                    ORDER BY is_pinned DESC, last_message_time DESC, created_at DESC
                """).fetchall()
            return [dict(r) for r in rows]

    def get_conversation(self, dest_hash: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE destination_hash = ?",
                (dest_hash.lower(),)
            ).fetchone()
            return dict(row) if row else None

    def add_or_update_conversation(
        self,
        dest_hash: str,
        identity_hash: Optional[str] = None,
        display_name: Optional[str] = None,
        custom_name: Optional[str] = None,
        hops: Optional[int] = None
    ) -> Dict[str, Any]:
        dest_hash = dest_hash.lower()
        now = time.time()
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM conversations WHERE destination_hash = ?", (dest_hash,)
            ).fetchone()

            if existing:
                new_id_hash = identity_hash or existing["identity_hash"]
                new_display = display_name or existing["display_name"]
                new_custom = custom_name if custom_name is not None else existing["custom_name"]
                new_hops = hops if hops is not None else existing["hops"]

                conn.execute("""
                    UPDATE conversations
                    SET identity_hash = ?, display_name = ?, custom_name = ?, hops = ?
                    WHERE destination_hash = ?
                """, (new_id_hash, new_display, new_custom, new_hops, dest_hash))
            else:
                conn.execute("""
                    INSERT INTO conversations (
                        destination_hash, identity_hash, display_name, custom_name,
                        last_message_text, last_message_time, unread_count, is_pinned, hops, created_at
                    ) VALUES (?, ?, ?, ?, '', 0, 0, 0, ?, ?)
                """, (dest_hash, identity_hash, display_name, custom_name, hops or 0, now))
            conn.commit()

        return self.get_conversation(dest_hash)  # type: ignore

    def update_conversation_last_message(
        self,
        dest_hash: str,
        last_text: str,
        last_time: float,
        increment_unread: bool = False
    ):
        dest_hash = dest_hash.lower()
        with self._get_conn() as conn:
            if increment_unread:
                conn.execute("""
                    UPDATE conversations
                    SET last_message_text = ?, last_message_time = ?, unread_count = unread_count + 1
                    WHERE destination_hash = ?
                """, (last_text, last_time, dest_hash))
            else:
                conn.execute("""
                    UPDATE conversations
                    SET last_message_text = ?, last_message_time = ?
                    WHERE destination_hash = ?
                """, (last_text, last_time, dest_hash))
            conn.commit()

    def clear_unread_count(self, dest_hash: str):
        dest_hash = dest_hash.lower()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE conversations SET unread_count = 0 WHERE destination_hash = ?",
                (dest_hash,)
            )
            conn.commit()

    def set_custom_name(self, dest_hash: str, custom_name: Optional[str]):
        dest_hash = dest_hash.lower()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE conversations SET custom_name = ? WHERE destination_hash = ?",
                (custom_name, dest_hash)
            )
            conn.commit()

    def delete_conversation(self, dest_hash: str):
        dest_hash = dest_hash.lower()
        with self._get_conn() as conn:
            conn.execute("DELETE FROM messages WHERE conversation_hash = ?", (dest_hash,))
            conn.execute("DELETE FROM conversations WHERE destination_hash = ?", (dest_hash,))
            conn.commit()

    # --- Messages ---
    def get_messages(self, conversation_hash: str, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        conversation_hash = conversation_hash.lower()
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM messages
                WHERE conversation_hash = ?
                ORDER BY timestamp ASC, id ASC
                LIMIT ? OFFSET ?
            """, (conversation_hash, limit, offset)).fetchall()
            return [dict(r) for r in rows]

    def add_message(
        self,
        message_hash: str,
        conversation_hash: str,
        sender_hash: str,
        recipient_hash: str,
        is_outgoing: bool,
        content: str,
        timestamp: float,
        state: int = 0,
        hops: int = 0,
        fields_json: Optional[str] = None
    ) -> int:
        message_hash = message_hash.lower()
        conversation_hash = conversation_hash.lower()
        sender_hash = sender_hash.lower()
        recipient_hash = recipient_hash.lower()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO messages (
                    message_hash, conversation_hash, sender_hash, recipient_hash,
                    is_outgoing, content, timestamp, state, hops, fields_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_hash, conversation_hash, sender_hash, recipient_hash,
                1 if is_outgoing else 0, content, timestamp, state, hops, fields_json
            ))
            msg_id = cursor.lastrowid
            conn.commit()
            return msg_id

    def update_message_state(self, message_hash: str, state: int):
        message_hash = message_hash.lower()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE messages SET state = ? WHERE message_hash = ?",
                (state, message_hash)
            )
            conn.commit()

    def get_message_by_hash(self, message_hash: str) -> Optional[Dict[str, Any]]:
        message_hash = message_hash.lower()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE message_hash = ?",
                (message_hash,)
            ).fetchone()
            return dict(row) if row else None

    # --- Announces / Mesh Peer Discovery ---
    def save_announce(
        self,
        dest_hash: str,
        identity_hash: Optional[str],
        display_name: Optional[str],
        hops: int,
        aspect: str,
        receiving_interface: Optional[str],
        last_seen: float
    ):
        dest_hash = dest_hash.lower()
        if identity_hash:
            identity_hash = identity_hash.lower()

        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO announces (
                    destination_hash, identity_hash, display_name, hops, aspect, receiving_interface, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(destination_hash) DO UPDATE SET
                    identity_hash = COALESCE(excluded.identity_hash, announces.identity_hash),
                    display_name = COALESCE(excluded.display_name, announces.display_name),
                    hops = excluded.hops,
                    aspect = excluded.aspect,
                    receiving_interface = excluded.receiving_interface,
                    last_seen = excluded.last_seen
            """, (dest_hash, identity_hash, display_name, hops, aspect, receiving_interface, last_seen))
            conn.commit()

    def get_announces(self, query: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            if query:
                q = f"%{query.strip().lower()}%"
                rows = conn.execute("""
                    SELECT * FROM announces
                    WHERE lower(destination_hash) LIKE ?
                       OR lower(COALESCE(display_name, '')) LIKE ?
                       OR lower(COALESCE(identity_hash, '')) LIKE ?
                       OR lower(COALESCE(receiving_interface, '')) LIKE ?
                    ORDER BY last_seen DESC
                    LIMIT ?
                """, (q, q, q, q, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM announces
                    ORDER BY last_seen DESC
                    LIMIT ?
                """, (limit,)).fetchall()
            return [dict(r) for r in rows]

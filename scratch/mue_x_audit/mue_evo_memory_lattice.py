"""6-layer persistent memory lattice with SQLite FTS5 backend.

Each layer serves a distinct purpose in the agent's cognitive architecture.
The lattice is self-organizing: memories flow from raw (L5) to crystallized (L3)
as they are reinforced through successful reuse.
"""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class MemoryEntry:
    """A single memory unit in the lattice."""
    layer: int
    key: str
    content: str
    embedding: Optional[list[float]] = None
    tags: list[str] = field(default_factory=list)
    access_count: int = 0
    success_count: int = 0  # Times this memory led to successful action
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    ttl: Optional[float] = None  # Seconds until expiry (None = permanent)
    parent_key: Optional[str] = None  # Which memory spawned this one


class MemoryLattice:
    """The agent's complete memory system."""

    LAYERS = {
        0: "meta_rules",
        1: "insight_index",
        2: "global_facts",
        3: "task_skills",
        4: "session_archive",
        5: "episodic_raw",
    }

    def __init__(self, db_path: str = "evo_memory.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

        # Enable FTS5
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer INTEGER NOT NULL,
                key TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                tags TEXT DEFAULT '[]',
                access_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                last_accessed REAL NOT NULL,
                ttl REAL,
                parent_key TEXT,
                metadata TEXT DEFAULT '{}'
            )
        """)

        # FTS5 virtual table for full-text search
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                key, content, tags,
                content='memories',
                content_rowid='id'
            )
        """)

        # Unique index on layer+key
        self.conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_layer_key ON memories(layer, key)
        """)

        # Triggers to keep FTS in sync
        self.conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, key, content, tags)
                VALUES (new.id, new.key, new.content, new.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, key, content, tags)
                VALUES ('delete', old.id, old.key, old.content, old.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, key, content, tags)
                VALUES ('delete', old.id, old.key, old.content, old.tags);
                INSERT INTO memories_fts(rowid, key, content, tags)
                VALUES (new.id, new.key, new.content, new.tags);
            END;
        """)

        self.conn.commit()

    # ── WRITE ──────────────────────────────────────────────

    def store(self, entry: MemoryEntry) -> int:
        """Store a memory. Returns its ID."""
        embedding_blob = None
        if entry.embedding:
            embedding_blob = json.dumps(entry.embedding).encode()

        cursor = self.conn.execute("""
            INSERT OR REPLACE INTO memories
                (layer, key, content, embedding, tags, access_count, success_count,
                 created_at, last_accessed, ttl, parent_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.layer,
            entry.key,
            entry.content,
            embedding_blob,
            json.dumps(entry.tags),
            entry.access_count,
            entry.success_count,
            entry.created_at,
            entry.last_accessed,
            entry.ttl,
            entry.parent_key,
        ))
        self.conn.commit()
        return cursor.lastrowid

    def reinforce(self, layer: int, key: str, success: bool = True) -> None:
        """Reinforce a memory — increases its weight on success."""
        if success:
            self.conn.execute("""
                UPDATE memories SET access_count = access_count + 1,
                    success_count = success_count + 1, last_accessed = ?
                WHERE layer = ? AND key = ?
            """, (time.time(), layer, key))
        else:
            self.conn.execute("""
                UPDATE memories SET access_count = access_count + 1,
                    last_accessed = ?
                WHERE layer = ? AND key = ?
            """, (time.time(), layer, key))
        self.conn.commit()

    # ── READ ───────────────────────────────────────────────

    def get(self, layer: int, key: str) -> Optional[MemoryEntry]:
        """Retrieve exact memory by layer + key."""
        row = self.conn.execute(
            "SELECT * FROM memories WHERE layer = ? AND key = ?",
            (layer, key),
        ).fetchone()
        if not row:
            return None
        return self._row_to_entry(row)

    def search_fts(self, query: str, layers: Optional[list[int]] = None,
                   limit: int = 10) -> list[MemoryEntry]:
        """Full-text search across memories."""
        layer_filter = ""
        params: list = []
        if layers:
            placeholders = ",".join("?" * len(layers))
            layer_filter = f"AND m.layer IN ({placeholders})"
            params = list(layers)

        rows = self.conn.execute(f"""
            SELECT m.* FROM memories m
            INNER JOIN memories_fts fts ON m.id = fts.rowid
            WHERE memories_fts MATCH ? {layer_filter}
            ORDER BY rank
            LIMIT ?
        """, [query] + params + [limit]).fetchall()

        return [self._row_to_entry(r) for r in rows]

    def search_tagged(self, tag: str, layer: Optional[int] = None,
                      limit: int = 20) -> list[MemoryEntry]:
        """Find memories by tag."""
        query = "SELECT * FROM memories WHERE tags LIKE ?"
        params: list = [f'%"{tag}"%']
        if layer is not None:
            query += " AND layer = ?"
            params.append(layer)
        query += " ORDER BY access_count DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_by_parent(self, parent_key: str) -> list[MemoryEntry]:
        """Get all memories spawned from a parent."""
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE parent_key = ? ORDER BY created_at DESC",
            (parent_key,),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_recent(self, layer: Optional[int] = None, limit: int = 20) -> list[MemoryEntry]:
        """Most recently accessed memories."""
        if layer is not None:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE layer = ? ORDER BY last_accessed DESC LIMIT ?",
                (layer, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memories ORDER BY last_accessed DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_most_successful(self, layer: int, limit: int = 10) -> list[MemoryEntry]:
        """Memories with the highest success rate."""
        rows = self.conn.execute("""
            SELECT * FROM memories
            WHERE layer = ? AND access_count > 0
            ORDER BY CAST(success_count AS REAL) / MAX(access_count, 1) DESC
            LIMIT ?
        """, (layer, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # ── MANAGE ─────────────────────────────────────────────

    def crystallize(self, l5_key: str, l3_key: str, distilled_content: str) -> MemoryEntry:
        """Elevate episodic memory (L5) to a reusable skill (L3)."""
        raw = self.get(5, l5_key)
        entry = MemoryEntry(
            layer=3,
            key=l3_key,
            content=distilled_content,
            tags=raw.tags if raw else [],
            parent_key=l5_key,
        )
        self.store(entry)
        return entry

    def purge_expired(self) -> int:
        """Remove TTL-expired memories. Returns count purged."""
        cursor = self.conn.execute(
            "DELETE FROM memories WHERE ttl IS NOT NULL AND (created_at + ttl) < ?",
            (time.time(),),
        )
        self.conn.commit()
        return cursor.rowcount

    def forget(self, layer: int, key: str) -> bool:
        """Remove a memory."""
        cursor = self.conn.execute(
            "DELETE FROM memories WHERE layer = ? AND key = ?",
            (layer, key),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    # ── UTILS ──────────────────────────────────────────────

    def _row_to_entry(self, row) -> MemoryEntry:
        embedding = None
        if row[4]:
            try:
                embedding = json.loads(row[4])
            except (json.JSONDecodeError, TypeError):
                embedding = row[4]
        return MemoryEntry(
            layer=row[1],
            key=row[2],
            content=row[3],
            embedding=embedding,
            tags=json.loads(row[5]) if row[5] else [],
            access_count=row[6],
            success_count=row[7],
            created_at=row[8],
            last_accessed=row[9],
            ttl=row[10],
            parent_key=row[11],
        )

    @property
    def stats(self) -> dict:
        counts = {}
        for layer_id, layer_name in self.LAYERS.items():
            row = self.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE layer = ?", (layer_id,)
            ).fetchone()
            counts[layer_name] = row[0] if row else 0
        total = sum(counts.values())
        return {
            "total_memories": total,
            "by_layer": counts,
            "db_size_mb": self.db_path.stat().st_size / (1024 * 1024) if self.db_path.exists() else 0,
        }

    def close(self):
        self.conn.close()

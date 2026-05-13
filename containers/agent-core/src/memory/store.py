import hashlib
import json
import os
from pathlib import Path

import structlog

from .types import MemoryContent, MemoryType

logger = structlog.get_logger()

MEMORY_BASE = Path("/data/memory")


class MemoryStore:
    """Filesystem-based memory storage. Each memory entry is a JSON file."""

    def __init__(self, base_path: Path = MEMORY_BASE):
        self.base_path = base_path

    def _type_dir(self, memory_type: MemoryType) -> Path:
        return self.base_path / memory_type.value

    def _file_path(self, memory_type: MemoryType, memory_id: str) -> Path:
        return self._type_dir(memory_type) / f"{memory_id}.json"

    def write(self, entry: MemoryContent) -> str:
        """Write a memory entry to the filesystem. Returns the file path."""
        dir_path = self._type_dir(entry.memory_type)
        dir_path.mkdir(parents=True, exist_ok=True)

        file_path = self._file_path(entry.memory_type, entry.id)
        data = entry.model_dump(mode="json")

        with open(file_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.debug("memory_store.write", path=str(file_path), memory_type=entry.memory_type)
        return str(file_path.relative_to(self.base_path))

    def read(self, memory_type: MemoryType, memory_id: str) -> MemoryContent | None:
        """Read a memory entry from the filesystem."""
        file_path = self._file_path(memory_type, memory_id)
        if not file_path.exists():
            return None

        with open(file_path) as f:
            data = json.load(f)

        return MemoryContent(**data)

    def delete(self, memory_type: MemoryType, memory_id: str) -> bool:
        """Delete a memory entry from the filesystem."""
        file_path = self._file_path(memory_type, memory_id)
        if file_path.exists():
            file_path.unlink()
            logger.debug("memory_store.delete", path=str(file_path))
            return True
        return False

    def list_entries(self, memory_type: MemoryType) -> list[MemoryContent]:
        """List all memory entries of a given type."""
        dir_path = self._type_dir(memory_type)
        if not dir_path.exists():
            return []

        entries = []
        for file_path in sorted(dir_path.glob("*.json")):
            try:
                with open(file_path) as f:
                    data = json.load(f)
                entries.append(MemoryContent(**data))
            except Exception:
                logger.warning("memory_store.read_error", path=str(file_path))
        return entries

    def list_all(self) -> list[MemoryContent]:
        """List all memory entries across all types."""
        entries = []
        for memory_type in MemoryType:
            entries.extend(self.list_entries(memory_type))
        return entries

    def content_hash(self, entry: MemoryContent) -> str:
        """Generate a SHA-256 hash of the entry content."""
        raw = json.dumps(entry.content, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def count(self, memory_type: MemoryType | None = None) -> int:
        """Count memory entries."""
        if memory_type:
            dir_path = self._type_dir(memory_type)
            if not dir_path.exists():
                return 0
            return len(list(dir_path.glob("*.json")))

        total = 0
        for mt in MemoryType:
            total += self.count(mt)
        return total

    def total_size_bytes(self) -> int:
        """Total size of all memory files in bytes."""
        total = 0
        for mt in MemoryType:
            dir_path = self._type_dir(mt)
            if dir_path.exists():
                for f in dir_path.glob("*.json"):
                    total += f.stat().st_size
        return total

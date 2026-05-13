import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import structlog

from .store import MemoryStore
from .types import SnapshotInfo

logger = structlog.get_logger()

BACKUPS_BASE = Path("/data/backups/snapshots")


class SnapshotManager:
    """Manages cognitive snapshots for memory versioning and rollback."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def create(self, label: str, trigger: str = "manual") -> SnapshotInfo:
        """Create a point-in-time snapshot of all memory."""
        snapshot_id = str(uuid4())
        snapshot_dir = BACKUPS_BASE / snapshot_id

        BACKUPS_BASE.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.store.base_path, snapshot_dir / "memory")

        entry_count = self.store.count()
        size_bytes = self.store.total_size_bytes()

        info = SnapshotInfo(
            id=snapshot_id,
            label=label,
            created_at=datetime.now(timezone.utc).isoformat(),
            entry_count=entry_count,
            size_bytes=size_bytes,
            trigger=trigger,
        )

        # Save snapshot metadata
        with open(snapshot_dir / "snapshot.json", "w") as f:
            json.dump(info.model_dump(), f, indent=2)

        logger.info(
            "snapshot.created",
            id=snapshot_id,
            label=label,
            entries=entry_count,
            size=size_bytes,
        )
        return info

    def restore(self, snapshot_id: str) -> SnapshotInfo:
        """Restore memory from a snapshot. Creates a pre-rollback snapshot first."""
        snapshot_dir = BACKUPS_BASE / snapshot_id

        if not snapshot_dir.exists():
            raise FileNotFoundError(f"Snapshot {snapshot_id} not found")

        # Safety: create a pre-rollback snapshot
        self.create("pre-rollback-auto", trigger="pre-rollback")

        # Restore memory
        memory_backup = snapshot_dir / "memory"
        if memory_backup.exists():
            # Clear current memory and copy from snapshot
            for item in self.store.base_path.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
            for item in memory_backup.iterdir():
                if item.is_dir():
                    shutil.copytree(item, self.store.base_path / item.name)

        # Load and return snapshot info
        with open(snapshot_dir / "snapshot.json") as f:
            info = SnapshotInfo(**json.load(f))

        logger.info("snapshot.restored", id=snapshot_id, label=info.label)
        return info

    def list_snapshots(self) -> list[SnapshotInfo]:
        """List all available snapshots."""
        if not BACKUPS_BASE.exists():
            return []

        snapshots = []
        for snap_dir in sorted(BACKUPS_BASE.iterdir(), reverse=True):
            meta_file = snap_dir / "snapshot.json"
            if meta_file.exists():
                with open(meta_file) as f:
                    snapshots.append(SnapshotInfo(**json.load(f)))
        return snapshots

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Delete a snapshot."""
        snapshot_dir = BACKUPS_BASE / snapshot_id
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
            logger.info("snapshot.deleted", id=snapshot_id)
            return True
        return False

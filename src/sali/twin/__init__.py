"""The Desktop Digital Twin (spec §14–16): a deterministic, continuously-refreshable structural
model of Almir's machine, reconciled into the temporal knowledge graph."""

from sali.twin.daemon import TwinDaemon
from sali.twin.model import TwinEntity, TwinSnapshot
from sali.twin.service import TwinService
from sali.twin.sync import SyncResult, sync_snapshot

__all__ = [
    "SyncResult", "TwinDaemon", "TwinEntity", "TwinService", "TwinSnapshot", "sync_snapshot",
]

"""On-device memory: what the user's assistant remembers, kept only on the user's device.

``MemoryStore`` keeps one user's memory entries, facts and the log of their chats in a
SQLite file only they can read. The engine never keeps any of it: each turn the SDK sends
it a bounded slice, the engine sends back changes for the device to apply, and the device
answers the agent's memory searches from its own index. ``T2VMemory`` does that for a
``Talk2View`` client given ``memory_path``.
"""

from .client import T2VMemory
from .models import (
    TOPICS,
    AppliedBatch,
    ConsolidationSnapshot,
    ConversationRecord,
    Fact,
    MemoryEntry,
    SearchHit,
)
from .store import Backend, MemoryStore
from .text import (
    STOP_WORDS,
    code_points,
    index_tokens,
    normalise_dedupe,
    normalise_for_index,
    segment_cjk,
)

__all__ = [
    "STOP_WORDS",
    "TOPICS",
    "AppliedBatch",
    "Backend",
    "ConsolidationSnapshot",
    "ConversationRecord",
    "Fact",
    "MemoryEntry",
    "MemoryStore",
    "SearchHit",
    "T2VMemory",
    "code_points",
    "index_tokens",
    "normalise_dedupe",
    "normalise_for_index",
    "segment_cjk",
]

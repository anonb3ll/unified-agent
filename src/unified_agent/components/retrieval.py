"""MCP tools of the ``retrieval`` component.

Semantic retrieval: embeddings, vector math, memory chunks and semantic
backfill, task search, the symbol indexer, recall curation, PDF extraction.

Order is registration order inside the component. Add a tool with one line
here; the module that defines it must be placed in this component in
docs/architecture/components.toml."""

from __future__ import annotations

from .. import recall_curation
from ..tools import indexer
from .base import Component

COMPONENT = Component(
    name="retrieval",
    tools=(
        indexer.fs_index_symbols,
        indexer.fs_list_symbols,
        indexer.fs_search_symbols,
        recall_curation.memory_promote,
        recall_curation.recall_maintenance,
    ),
)

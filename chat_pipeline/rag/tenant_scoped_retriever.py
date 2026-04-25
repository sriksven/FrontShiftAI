"""ChromaDB wrapper that enforces a ``company`` filter on every query.

Phase 0.6D: the raw ``collection.query(...)`` API lets callers omit a
``where`` clause and accidentally return cross-tenant vectors. This wrapper
removes that footgun — its only public method requires ``company`` as a
positional kwarg, and there is no escape hatch.

Raw ``collection.query()`` should not be used outside this module and its
callers. :func:`chat_pipeline.rag.retriever.vector_retrieval` is the canonical
consumer.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class TenantScopedRetriever:
    """Enforces ``where={"company": ...}`` on every Chroma query.

    A missing/empty company raises ``ValueError`` so mistakes surface loudly
    during development rather than leaking across tenants in production.
    """

    def __init__(self, collection: Any):
        self._collection = collection

    def query(
        self,
        query_text: str,
        company: str,
        top_k: int = 5,
        extra_where: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not company or not isinstance(company, str) or not company.strip():
            raise ValueError("TenantScopedRetriever.query: 'company' is required")

        where: Dict[str, Any] = {"company": company}
        if extra_where:
            # Combine: `{"$and": [{"company": X}, <extra>]}` for ChromaDB.
            where = {"$and": [where, extra_where]}

        return self._collection.query(
            query_texts=[query_text],
            n_results=top_k,
            where=where,
        )
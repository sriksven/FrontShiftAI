"""Idempotency-Key support for mutation endpoints.

Usage pattern inside a handler::

    @router.post("/api/pto/chat")
    async def chat(request, current_user, db, idem = Depends(idempotency_guard)):
        cached = idem.lookup(db, current_user["company"], endpoint="/api/pto/chat")
        if cached is not None:
            return cached
        result = do_work(...)
        idem.store(db, current_user["company"], endpoint="/api/pto/chat",
                   status_code=200, body=result.model_dump())
        return result

When the caller (e.g. the voice agent) passes an ``Idempotency-Key`` header,
the first request is processed normally and its response body persisted. Any
subsequent retry with the same key returns the cached body verbatim — no
duplicate PTO rows, no duplicate HR tickets.

Scope is per-tenant: a stray key reuse across tenants cannot leak.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from db.models import IdempotencyRecord

logger = logging.getLogger(__name__)


class IdempotencyGuard:
    """Holds the header value (or None) and encapsulates lookup/store.

    Created per-request via the :func:`idempotency_guard` dependency. If the
    client didn't send a key, lookups are no-ops (``None``) and stores silently
    skip — idempotency is opt-in on mutation endpoints.
    """

    def __init__(self, key: Optional[str]):
        self.key = key

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def lookup(self, db: Session, company: str, endpoint: str) -> Optional[Dict[str, Any]]:
        """Return the cached response body if this key was already processed.

        Scoped per tenant: a record for another tenant's same key is invisible.
        Returns ``None`` on miss, on absent header, or on decode failure.
        """
        if not self.enabled or not company:
            return None
        try:
            record = (
                db.query(IdempotencyRecord)
                .filter(
                    IdempotencyRecord.key == self.key,
                    IdempotencyRecord.company == company,
                )
                .first()
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("idempotency lookup failed: %s", exc)
            return None
        if record is None:
            return None
        try:
            return json.loads(record.response_body)
        except (TypeError, ValueError):
            logger.error("idempotency record %s has non-JSON body; ignoring", self.key)
            return None

    def store(
        self,
        db: Session,
        company: str,
        endpoint: str,
        status_code: int,
        body: Any,
    ) -> None:
        """Persist the response for this key.

        Safe to call even when idempotency is not enabled — it just returns.
        Swallows integrity errors (two concurrent identical requests racing to
        INSERT the same key); the earlier write wins and the later one is a
        harmless duplicate-ignore.
        """
        if not self.enabled or not company:
            return
        try:
            serialized = json.dumps(body, default=str)
        except (TypeError, ValueError) as exc:
            logger.error("idempotency store: body not JSON-serializable: %s", exc)
            return

        record = IdempotencyRecord(
            key=self.key,
            company=company,
            endpoint=endpoint,
            status_code=status_code,
            response_body=serialized,
        )
        try:
            db.add(record)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("idempotency store race or failure for %s: %s", self.key, exc)


def idempotency_guard(
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> IdempotencyGuard:
    """FastAPI dependency: inject an :class:`IdempotencyGuard` for the request."""
    return IdempotencyGuard(key=idempotency_key)
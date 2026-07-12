"""Security audit events, deliberately separate from diagnostic logs."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from security import Principal

logger = logging.getLogger("rag.audit")


def record(event: str, *, request_id: str | None = None, principal: Principal | None = None, outcome: str, **fields: Any) -> None:
    """Write a compact JSON event without secrets, prompts or document text."""
    payload = {
        "event": event, "outcome": outcome, "timestamp": time.time(), "request_id": request_id,
        "subject": principal.subject if principal else None,
        "identity_provider": principal.provider if principal else None,
        **fields,
    }
    logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

"""Durable account-deletion fence for external memory projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, cast

from database.account_deletion_policy import (
    ACCOUNT_DELETION_ACTIVE_COLLECTION,
    ACCOUNT_DELETION_RECEIPT_COLLECTION,
    account_deletion_blocks_access,
    account_deletion_receipt_id,
    normalize_account_deletion_status,
)

ACCOUNT_DELETION_COLLECTION = ACCOUNT_DELETION_ACTIVE_COLLECTION


@dataclass(frozen=True)
class AccountDeletionProjectionFence:
    status: str | None
    blocks_projection_writes: bool


def read_account_deletion_projection_fence(
    uid: str,
    *,
    db_client: Any,
) -> AccountDeletionProjectionFence:
    """Read the top-level deletion authority that survives the user-data wipe."""
    if not uid.strip():
        raise ValueError("uid is required")
    snapshot = db_client.document(f"{ACCOUNT_DELETION_COLLECTION}/{uid}").get()
    if not getattr(snapshot, "exists", False):
        receipt_path = f"{ACCOUNT_DELETION_RECEIPT_COLLECTION}/{account_deletion_receipt_id(uid)}"
        snapshot = db_client.document(receipt_path).get()
        if not getattr(snapshot, "exists", False):
            return AccountDeletionProjectionFence(status=None, blocks_projection_writes=False)
    raw: object = snapshot.to_dict()
    payload = cast(Dict[str, Any], raw) if isinstance(raw, dict) else {}
    status = normalize_account_deletion_status(marker_exists=True, raw_status=payload.get("wipe_status"))
    return AccountDeletionProjectionFence(
        status=status,
        blocks_projection_writes=account_deletion_blocks_access(status),
    )


__all__ = [
    "ACCOUNT_DELETION_COLLECTION",
    "AccountDeletionProjectionFence",
    "read_account_deletion_projection_fence",
]

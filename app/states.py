"""Video status constants and approval-gate helpers."""
from __future__ import annotations

from .config import settings

IDEA_PENDING = "idea_pending"
IDEA_APPROVED = "idea_approved"
SCRIPTING = "scripting"
SCRIPT_REVIEW = "script_review"
SCRIPT_APPROVED = "script_approved"
PRODUCING = "producing"
FINAL_REVIEW = "final_review"
APPROVED = "approved"
UPLOADING = "uploading"
SCHEDULED = "scheduled"
PUBLISHED = "published"
REJECTED = "rejected"
FAILED = "failed"

ALL = [IDEA_PENDING, IDEA_APPROVED, SCRIPTING, SCRIPT_REVIEW, SCRIPT_APPROVED, PRODUCING,
       FINAL_REVIEW, APPROVED, UPLOADING, SCHEDULED, PUBLISHED, REJECTED, FAILED]

# review status -> gate name
REVIEW = {IDEA_PENDING: "idea", SCRIPT_REVIEW: "script", FINAL_REVIEW: "final"}
TRANSIENT = {SCRIPTING, PRODUCING, UPLOADING}
DEFAULT_GATES = ["idea", "script", "final"]


def gates_for(channel_cfg: dict | None) -> set[str]:
    cfg = channel_cfg or {}
    gates = cfg.get("approval_gates")
    if gates is None:
        gates = settings.get("approval.gates", DEFAULT_GATES)
    return set(gates or [])

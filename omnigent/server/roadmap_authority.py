"""Structured, session-bound authority envelopes for roadmap work."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL = "clai-roadmap-authority/v1"
STATE_KEY = "omnigent.roadmap_authority.v1"
SUPPORTED_BINDINGS = {
    "Audit": "futurmax-audit-lab",
    "Memory": "futurmax-memory-lab",
    "WPLab": "wplab",
}
HARD_STOP_FLAGS = frozenset(
    {
        "production",
        "central_control_plane",
        "recovery",
        "secrets",
        "destructive_data",
        "scope_expansion",
    }
)


class AuthorityRequest(BaseModel):
    """Exact envelope presented to the session owner for approval."""

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["clai-roadmap-authority/v1"]
    authority_ref: str = Field(min_length=1, max_length=256)
    roadmap_item: str = Field(min_length=1, max_length=256)
    project: Literal["Audit", "Memory", "WPLab"]
    host: Literal["futurmax-audit-lab", "futurmax-memory-lab", "wplab"]
    workspace: str = Field(min_length=1, max_length=4096)
    scope: list[str] = Field(min_length=1, max_length=64)
    allowed_operations: list[str] = Field(min_length=1, max_length=64)
    max_budget_usd: float = Field(ge=0, le=1_000_000)
    max_provider_calls: int = Field(ge=0, le=1_000_000)
    max_checkpoints: int = Field(gt=0, le=10_000)
    max_duration_seconds: int = Field(gt=0, le=604_800)
    stop_conditions: list[str] = Field(min_length=1, max_length=64)
    excluded_effects: list[
        Literal[
            "production",
            "central_control_plane",
            "recovery",
            "secrets",
            "destructive_data",
            "scope_expansion",
        ]
    ] = Field(min_length=6, max_length=6)

    @field_validator(
        "authority_ref",
        "roadmap_item",
        "workspace",
        mode="after",
    )
    @classmethod
    def _nonblank_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("scope", "allowed_operations", "stop_conditions")
    @classmethod
    def _unique_nonempty_strings(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("entries must be non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("entries must be unique")
        return value

    @field_validator("max_budget_usd")
    @classmethod
    def _finite_budget(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("budget must be finite")
        return value

    @model_validator(mode="after")
    def _binding_pair(self) -> AuthorityRequest:
        if SUPPORTED_BINDINGS[self.project] != self.host:
            raise ValueError("unsupported project/host binding")
        if set(self.excluded_effects) != HARD_STOP_FLAGS:
            raise ValueError("every mandatory hard stop must be excluded")
        return self


class AuthorityCheckpoint(BaseModel):
    """One consumptive reservation inside an approved envelope."""

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["clai-roadmap-authority/v1"]
    authority_id: str = Field(min_length=1, max_length=128)
    checkpoint_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    project: Literal["Audit", "Memory", "WPLab"]
    host: Literal["futurmax-audit-lab", "futurmax-memory-lab", "wplab"]
    workspace: str = Field(min_length=1, max_length=4096)
    scope: str = Field(min_length=1, max_length=1024)
    operation: str = Field(min_length=1, max_length=256)
    reserve_budget_usd: float = Field(default=0, ge=0, le=1_000_000)
    reserve_provider_calls: int = Field(default=0, ge=0, le=1_000_000)
    risk_flags: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("authority_id", "workspace", "scope", "operation")
    @classmethod
    def _nonblank_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("reserve_budget_usd")
    @classmethod
    def _finite_budget(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("budget must be finite")
        return value


def envelope_digest(envelope: AuthorityRequest) -> str:
    """Return a stable digest of the exact owner-visible envelope."""
    payload = envelope.model_dump_json(exclude_none=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_live_binding(
    envelope: AuthorityRequest,
    *,
    host_id: str | None,
    host_name: str | None,
    project: str | None,
    workspace: str | None,
) -> str | None:
    """Return a fail-closed binding error, or ``None`` on an exact match."""
    if not host_id or not host_name:
        return "unsupported_identity: session has no resolvable bound host"
    if host_name != envelope.host:
        return f"changed_envelope: session host is {host_name!r}, expected {envelope.host!r}"
    if project != envelope.project:
        return f"changed_envelope: session project is {project!r}, expected {envelope.project!r}"
    if workspace != envelope.workspace:
        return "changed_envelope: session workspace no longer matches the approved request"
    return None


def pending_state(envelope: AuthorityRequest, now: float | None = None) -> dict[str, Any]:
    """Build the durable state written before the owner prompt is published."""
    created_at = now if now is not None else time.time()
    return {
        "protocol": PROTOCOL,
        "authority_id": f"auth_{uuid.uuid4().hex}",
        "envelope": envelope.model_dump(mode="json"),
        "envelope_digest": envelope_digest(envelope),
        "status": "pending",
        "created_at": created_at,
        "expires_at": created_at + envelope.max_duration_seconds,
        "used_budget_usd": 0.0,
        "used_provider_calls": 0,
        "used_checkpoints": 0,
        "used_checkpoint_ids": [],
    }


def checkpoint_state(
    state: dict[str, Any], checkpoint: AuthorityCheckpoint, *, now: float | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and account one checkpoint against persisted state."""
    if state.get("protocol") != PROTOCOL:
        return None, "unsupported_protocol"
    if state.get("status") != "approved":
        return None, "missing_approval"
    current_time = now if now is not None else time.time()
    try:
        expires_at = float(state.get("expires_at", 0))
        envelope = AuthorityRequest.model_validate(state.get("envelope"))
    except (TypeError, ValueError):
        return None, "unsupported_protocol"
    if current_time >= expires_at:
        return None, "timeout"
    stored_digest = state.get("envelope_digest")
    if stored_digest != envelope_digest(envelope):
        return None, "changed_envelope: persisted envelope digest mismatch"
    if checkpoint.authority_id != state.get("authority_id"):
        return None, "changed_envelope: authority_id mismatch"
    if checkpoint.envelope_digest != stored_digest:
        return None, "changed_envelope: digest mismatch"
    used_ids = state.get("used_checkpoint_ids")
    if not isinstance(used_ids, list) or not all(isinstance(item, str) for item in used_ids):
        return None, "changed_envelope: invalid checkpoint history"
    if checkpoint.checkpoint_id in used_ids:
        return None, "replay"
    if (checkpoint.project, checkpoint.host, checkpoint.workspace) != (
        envelope.project,
        envelope.host,
        envelope.workspace,
    ):
        return None, "changed_envelope: binding mismatch"
    if (
        checkpoint.scope not in envelope.scope
        or checkpoint.operation not in envelope.allowed_operations
    ):
        return None, "scope_expansion"
    flags = set(checkpoint.risk_flags)
    if flags & HARD_STOP_FLAGS:
        return None, f"hard_stop: {sorted(flags & HARD_STOP_FLAGS)[0]}"
    try:
        used_budget = float(state.get("used_budget_usd", 0))
        used_calls = int(state.get("used_provider_calls", 0))
        used_count = int(state.get("used_checkpoints", 0))
    except (TypeError, ValueError):
        return None, "changed_envelope: invalid accounting state"
    if (
        not math.isfinite(used_budget)
        or used_budget < 0
        or used_calls < 0
        or used_count < 0
        or used_budget > envelope.max_budget_usd
        or used_calls > envelope.max_provider_calls
        or used_count > envelope.max_checkpoints
        or used_count != len(used_ids)
        or len(set(used_ids)) != len(used_ids)
    ):
        return None, "changed_envelope: invalid accounting state"
    budget = used_budget + checkpoint.reserve_budget_usd
    calls = used_calls + checkpoint.reserve_provider_calls
    count = used_count + 1
    if not math.isfinite(budget) or budget > envelope.max_budget_usd:
        return None, "exhausted_caps"
    if calls > envelope.max_provider_calls or count > envelope.max_checkpoints:
        return None, "exhausted_caps"
    updated = json.loads(json.dumps(state))
    updated.update(
        used_budget_usd=budget,
        used_provider_calls=calls,
        used_checkpoints=count,
    )
    updated["used_checkpoint_ids"] = [*used_ids, checkpoint.checkpoint_id]
    return updated, None

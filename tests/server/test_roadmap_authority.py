"""Unit coverage for structured roadmap authority accounting."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from omnigent.server.roadmap_authority import (
    HARD_STOP_FLAGS,
    AuthorityCheckpoint,
    AuthorityRequest,
    checkpoint_state,
    pending_state,
    validate_live_binding,
)


def _envelope() -> AuthorityRequest:
    return AuthorityRequest.model_validate(
        {
            "protocol": "clai-roadmap-authority/v1",
            "authority_ref": "PETER-TEST",
            "roadmap_item": "A7",
            "project": "Audit",
            "host": "futurmax-audit-lab",
            "workspace": "/work/repo",
            "scope": ["tests", "source"],
            "allowed_operations": ["edit", "test"],
            "max_budget_usd": 5,
            "max_provider_calls": 2,
            "max_checkpoints": 2,
            "max_duration_seconds": 60,
            "stop_conditions": ["failed test", "changed scope"],
            "excluded_effects": sorted(HARD_STOP_FLAGS),
        }
    )


def _checkpoint(state: dict[str, object], **updates: object) -> AuthorityCheckpoint:
    payload = {
        "protocol": "clai-roadmap-authority/v1",
        "authority_id": state["authority_id"],
        "checkpoint_id": "cp-1",
        "envelope_digest": state["envelope_digest"],
        "project": "Audit",
        "host": "futurmax-audit-lab",
        "workspace": "/work/repo",
        "scope": "source",
        "operation": "edit",
    }
    payload.update(updates)
    return AuthorityCheckpoint.model_validate(payload)


def test_request_requires_exact_binding_pair_and_all_hard_stops() -> None:
    payload = _envelope().model_dump()
    payload["host"] = "futurmax-memory-lab"
    with pytest.raises(ValidationError, match="unsupported project/host binding"):
        AuthorityRequest.model_validate(payload)

    payload = _envelope().model_dump()
    payload["excluded_effects"] = sorted(HARD_STOP_FLAGS - {"secrets"})
    with pytest.raises(ValidationError):
        AuthorityRequest.model_validate(payload)


def test_live_binding_is_exact_and_fail_closed() -> None:
    envelope = _envelope()
    assert (
        validate_live_binding(
            envelope,
            host_id="host_audit",
            host_name="futurmax-audit-lab",
            project="Audit",
            workspace="/work/repo",
        )
        is None
    )
    missing_host = validate_live_binding(
        envelope,
        host_id=None,
        host_name=None,
        project="Audit",
        workspace="/work/repo",
    )
    assert missing_host is not None and missing_host.startswith("unsupported_identity")
    changed_workspace = validate_live_binding(
        envelope,
        host_id="host_audit",
        host_name="futurmax-audit-lab",
        project="Audit",
        workspace="/other",
    )
    assert changed_workspace is not None and changed_workspace.startswith("changed_envelope")


def test_checkpoint_accounts_exact_approved_envelope() -> None:
    state = pending_state(_envelope(), now=100)
    state["status"] = "approved"
    updated, error = checkpoint_state(
        state,
        _checkpoint(state, reserve_budget_usd=2, reserve_provider_calls=1),
        now=101,
    )
    assert error is None and updated is not None
    assert updated["used_budget_usd"] == 2
    assert updated["used_provider_calls"] == 1
    assert updated["used_checkpoints"] == 1
    assert state["used_checkpoints"] == 0


def test_checkpoint_rejects_missing_approval_timeout_and_changed_envelope() -> None:
    state = pending_state(_envelope(), now=100)
    checkpoint = _checkpoint(state)
    assert checkpoint_state(state, checkpoint, now=101)[1] == "missing_approval"
    state["status"] = "approved"
    assert checkpoint_state(state, checkpoint, now=160)[1] == "timeout"
    assert (
        checkpoint_state(state, checkpoint.model_copy(update={"scope": "new"}), now=101)[1]
        == "scope_expansion"
    )
    changed = checkpoint.model_copy(update={"envelope_digest": "0" * 64})
    assert checkpoint_state(state, changed, now=101)[1] == "changed_envelope: digest mismatch"

    tampered = copy.deepcopy(state)
    tampered["envelope"]["scope"] = ["source", "new"]
    assert checkpoint_state(tampered, checkpoint, now=101)[1] == (
        "changed_envelope: persisted envelope digest mismatch"
    )


def test_checkpoint_rejects_caps_replay_and_every_hard_stop() -> None:
    state = pending_state(_envelope(), now=100)
    state["status"] = "approved"
    assert (
        checkpoint_state(state, _checkpoint(state, reserve_budget_usd=6), now=101)[1]
        == "exhausted_caps"
    )
    assert (
        checkpoint_state(state, _checkpoint(state, reserve_provider_calls=3), now=101)[1]
        == "exhausted_caps"
    )
    for flag in HARD_STOP_FLAGS:
        assert checkpoint_state(state, _checkpoint(state, risk_flags=[flag]), now=101)[1] == (
            f"hard_stop: {flag}"
        )
    first, error = checkpoint_state(state, _checkpoint(state), now=101)
    assert error is None and first is not None
    assert checkpoint_state(first, _checkpoint(state), now=102)[1] == "replay"
    second, error = checkpoint_state(
        first,
        _checkpoint(state, checkpoint_id="cp-2"),
        now=102,
    )
    assert error is None and second is not None
    assert (
        checkpoint_state(
            second,
            _checkpoint(state, checkpoint_id="cp-3"),
            now=103,
        )[1]
        == "exhausted_caps"
    )


def test_checkpoint_rejects_corrupt_accounting_state() -> None:
    state = pending_state(_envelope(), now=100)
    state["status"] = "approved"
    state["used_budget_usd"] = -1
    assert checkpoint_state(state, _checkpoint(state), now=101)[1] == (
        "changed_envelope: invalid accounting state"
    )

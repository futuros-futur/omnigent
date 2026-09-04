"""Request, owner elicitation, and consumptive checkpoint integration coverage."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_elicitation_resolve_url import (
    _create_session,
    _drain_until_elicitation_event,
)

pytestmark = pytest.mark.asyncio

_AUDIT_HOST_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _request() -> dict[str, object]:
    return {
        "action": "request",
        "request": {
            "protocol": "clai-roadmap-authority/v1",
            "authority_ref": "PETER-TEST",
            "roadmap_item": "A7",
            "project": "Audit",
            "host": "futurmax-audit-lab",
            "workspace": "/work/repo",
            "scope": ["source"],
            "allowed_operations": ["edit"],
            "max_budget_usd": 3,
            "max_provider_calls": 1,
            "max_checkpoints": 2,
            "max_duration_seconds": 30,
            "stop_conditions": ["failed test"],
            "excluded_effects": [
                "production",
                "central_control_plane",
                "recovery",
                "secrets",
                "destructive_data",
                "scope_expansion",
            ],
        },
    }


async def _bound_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    name: str,
) -> str:
    agent = await create_test_agent(client, name)
    session_id = await _create_session(client, agent["id"])
    host_store = HostStore(db_uri)
    app.state.host_store = host_store
    host_store.upsert_on_connect(_AUDIT_HOST_ID, "futurmax-audit-lab", "local")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(session_id, _AUDIT_HOST_ID, workspace="/work/repo")
    store.set_labels(session_id, {"omni_project": "Audit"})
    return session_id


async def _approve(
    client: httpx.AsyncClient,
    session_id: str,
) -> dict[str, object]:
    subscribed = asyncio.Event()
    event_task = asyncio.create_task(
        _drain_until_elicitation_event(session_id, subscribed=subscribed)
    )
    await subscribed.wait()
    request_task = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/roadmap-authority", json=_request())
    )
    event = await event_task
    assert event["params"]["authority_envelope"]["project"] == "Audit"
    verdict = await client.post(
        f"/v1/sessions/{session_id}/elicitations/{event['elicitation_id']}/resolve",
        json={"action": "accept"},
    )
    assert verdict.status_code == 202
    response = await request_task
    assert response.status_code == 200
    return response.json()


def _checkpoint(approved: dict[str, object], checkpoint_id: str) -> dict[str, object]:
    return {
        "action": "checkpoint",
        "checkpoint": {
            "protocol": "clai-roadmap-authority/v1",
            "authority_id": approved["authority_id"],
            "checkpoint_id": checkpoint_id,
            "envelope_digest": approved["envelope_digest"],
            "project": "Audit",
            "host": "futurmax-audit-lab",
            "workspace": "/work/repo",
            "scope": "source",
            "operation": "edit",
            "reserve_budget_usd": 1,
            "reserve_provider_calls": 1,
        },
    }


async def test_owner_approval_permits_exact_checkpoint(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
) -> None:
    session_id = await _bound_session(client, app, db_uri, "roadmap-authority-accept")
    approved = await _approve(client, session_id)
    assert approved["status"] == "APPROVED"

    response = await client.post(
        f"/v1/sessions/{session_id}/roadmap-authority",
        json=_checkpoint(approved, "source-edit-1"),
    )
    receipt = response.json()
    assert receipt["status"] == "PERMITTED"
    assert receipt["remaining_budget_usd"] == 2
    assert receipt["remaining_provider_calls"] == 0


@pytest.mark.parametrize("verdict", ["decline", "cancel"])
async def test_rejection_blocks_and_request_cannot_replay(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    verdict: str,
) -> None:
    session_id = await _bound_session(client, app, db_uri, f"roadmap-authority-{verdict}")
    subscribed = asyncio.Event()
    event_task = asyncio.create_task(
        _drain_until_elicitation_event(session_id, subscribed=subscribed)
    )
    await subscribed.wait()
    request_task = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/roadmap-authority", json=_request())
    )
    event = await event_task
    await client.post(
        f"/v1/sessions/{session_id}/elicitations/{event['elicitation_id']}/resolve",
        json={"action": verdict},
    )
    result = (await request_task).json()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == verdict
    replay = await client.post(
        f"/v1/sessions/{session_id}/roadmap-authority",
        json=_request(),
    )
    assert replay.json() == {"status": "BLOCKED", "reason": "replay"}


async def test_request_rejects_changed_host_project_binding(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
) -> None:
    session_id = await _bound_session(client, app, db_uri, "roadmap-authority-binding")
    body = _request()
    request = body["request"]
    assert isinstance(request, dict)
    request["project"] = "Memory"
    request["host"] = "futurmax-memory-lab"
    response = await client.post(
        f"/v1/sessions/{session_id}/roadmap-authority",
        json=body,
    )
    assert response.json()["status"] == "BLOCKED"
    assert response.json()["reason"].startswith("changed_envelope: session host")


async def test_accept_fails_closed_if_workspace_changes_while_prompt_is_pending(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
) -> None:
    session_id = await _bound_session(client, app, db_uri, "roadmap-authority-moved")
    subscribed = asyncio.Event()
    event_task = asyncio.create_task(
        _drain_until_elicitation_event(session_id, subscribed=subscribed)
    )
    await subscribed.wait()
    request_task = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/roadmap-authority", json=_request())
    )
    event = await event_task
    SqlAlchemyConversationStore(db_uri).set_host_id(
        session_id,
        _AUDIT_HOST_ID,
        workspace="/work/moved",
    )
    await client.post(
        f"/v1/sessions/{session_id}/elicitations/{event['elicitation_id']}/resolve",
        json={"action": "accept"},
    )
    result = (await request_task).json()
    assert result["status"] == "BLOCKED"
    assert result["reason"].startswith("changed_envelope: session workspace")


async def test_timeout_returns_deterministic_blocked_receipt(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _timeout(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.routes_roadmap_authority."
        "_publish_and_wait_for_harness_elicitation",
        _timeout,
    )
    session_id = await _bound_session(client, app, db_uri, "roadmap-authority-timeout")
    response = await client.post(
        f"/v1/sessions/{session_id}/roadmap-authority",
        json=_request(),
    )
    assert response.json()["status"] == "BLOCKED"
    assert response.json()["reason"] == "timeout"


async def test_concurrent_duplicate_checkpoint_is_single_flight(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
) -> None:
    session_id = await _bound_session(client, app, db_uri, "roadmap-authority-concurrent")
    approved = await _approve(client, session_id)
    payload = _checkpoint(approved, "same-checkpoint")
    first, second = await asyncio.gather(
        client.post(f"/v1/sessions/{session_id}/roadmap-authority", json=payload),
        client.post(f"/v1/sessions/{session_id}/roadmap-authority", json=payload),
    )
    results = [first.json(), second.json()]
    assert sorted(result["status"] for result in results) == ["BLOCKED", "PERMITTED"]
    blocked = next(result for result in results if result["status"] == "BLOCKED")
    assert blocked["reason"] == "replay"

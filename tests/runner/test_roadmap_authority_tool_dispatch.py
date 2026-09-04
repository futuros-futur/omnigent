"""Runner dispatch coverage for ``sys_roadmap_authority``."""

from __future__ import annotations

import json

import pytest

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_roadmap_authority_tool,
    execute_tool,
)
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager


class _Response:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {"status": "APPROVED", "authority_id": "auth_1"}


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: object,
    ) -> _Response:
        self.calls.append((url, json))
        return _Response()


@pytest.mark.asyncio
async def test_dispatch_proxies_exact_payload_to_current_session() -> None:
    client = _Client()
    payload: dict[str, object] = {
        "action": "request",
        "request": {"protocol": "clai-roadmap-authority/v1"},
    }
    output = await _execute_roadmap_authority_tool(
        payload,
        conversation_id="conv_test",
        server_client=client,  # type: ignore[arg-type]
    )
    assert client.calls == [("/v1/sessions/conv_test/roadmap-authority", payload)]
    assert json.loads(output) == {"status": "APPROVED", "authority_id": "auth_1"}


@pytest.mark.asyncio
async def test_execute_tool_uses_authority_dispatch_branch() -> None:
    client = _Client()
    payload: dict[str, object] = {
        "action": "checkpoint",
        "checkpoint": {"checkpoint_id": "cp-1"},
    }
    output = await execute_tool(
        tool_name="sys_roadmap_authority",
        arguments=json.dumps(payload),
        conversation_id="conv_test",
        server_client=client,  # type: ignore[arg-type]
    )
    assert client.calls == [("/v1/sessions/conv_test/roadmap-authority", payload)]
    assert json.loads(output)["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_dispatch_fails_closed_without_session_identity() -> None:
    output = await _execute_roadmap_authority_tool(
        {"action": "request"},
        conversation_id=None,
        server_client=None,
    )
    assert json.loads(output) == {
        "status": "BLOCKED",
        "reason": "missing_session_identity",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "reason"),
    [("{", "malformed_json"), ("[]", "malformed_request"), ("", "malformed_json")],
)
async def test_execute_tool_malformed_input_fails_closed(arguments: str, reason: str) -> None:
    output = await execute_tool(
        tool_name="sys_roadmap_authority",
        arguments=arguments,
        conversation_id="conv_test",
        server_client=_Client(),  # type: ignore[arg-type]
    )
    assert json.loads(output) == {"status": "BLOCKED", "reason": reason}


def test_tool_is_always_registered_and_relayed() -> None:
    schemas = ToolManager(AgentSpec(spec_version=1)).get_tool_schemas()
    names = {schema["function"]["name"] for schema in schemas}
    assert "sys_roadmap_authority" in names
    assert "sys_roadmap_authority" in _ALL_LOCAL_TOOLS
    assert "sys_roadmap_authority" in _NATIVE_RELAY_BUILTIN_TOOLS

"""Session routes for bounded roadmap authority envelopes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from omnigent.entities import Conversation
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.roadmap_authority import (
    PROTOCOL,
    STATE_KEY,
    AuthorityCheckpoint,
    AuthorityRequest,
    checkpoint_state,
    envelope_digest,
    pending_state,
    validate_live_binding,
)
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._sessions.orchestration import (
    _publish_and_wait_for_harness_elicitation,
)
from omnigent.server.schemas import ElicitationRequestParams
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY
from omnigent.stores.permission_store import PermissionStore

_roadmap_authority_locks: dict[str, asyncio.Lock] = {}


def register_roadmap_authority_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the roadmap-authority route on ``router``."""

    async def _live_binding(
        request: Request,
        session_id: str,
        envelope: AuthorityRequest,
    ) -> tuple[Conversation | None, str | None]:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            return None, "unsupported_identity: session not found"
        host_store = getattr(request.app.state, "host_store", None)
        try:
            host = (
                await asyncio.to_thread(host_store.get_host, conv.host_id)
                if host_store is not None and conv.host_id is not None
                else None
            )
        except Exception:
            return conv, "unsupported_identity: bound host cannot be resolved"
        error = validate_live_binding(
            envelope,
            host_id=conv.host_id,
            host_name=host.name if host is not None else None,
            project=conv.labels.get(PROJECT_LABEL_KEY),
            workspace=conv.workspace,
        )
        return conv, error

    async def _request_authority(
        request: Request,
        session_id: str,
        raw: Any,
    ) -> dict[str, Any]:
        try:
            envelope = AuthorityRequest.model_validate(raw)
        except ValidationError:
            return {"status": "BLOCKED", "reason": "unsupported_protocol_or_envelope"}

        lock = _roadmap_authority_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            conv, binding_error = await _live_binding(request, session_id, envelope)
            if binding_error is not None or conv is None:
                return {"status": "BLOCKED", "reason": binding_error or "unsupported_identity"}
            if STATE_KEY in conv.session_state:
                return {"status": "BLOCKED", "reason": "replay"}
            state = pending_state(envelope)
            session_state = dict(conv.session_state)
            session_state[STATE_KEY] = state
            await asyncio.to_thread(
                conversation_store.set_session_state,
                session_id,
                session_state,
            )

        summary = json.dumps(
            envelope.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=ElicitationRequestParams(
                mode="form",
                message="Approve this exact roadmap authority envelope?",
                requestedSchema={
                    "type": "object",
                    "properties": {"approved": {"type": "boolean", "const": True}},
                    "required": ["approved"],
                },
                phase="roadmap_authority",
                policy_name=PROTOCOL,
                content_preview=summary[:1024],
                authority_envelope=envelope.model_dump(mode="json"),
                authority_id=state["authority_id"],
                envelope_digest=state["envelope_digest"],
            ),
            timeout_s=float(envelope.max_duration_seconds),
            conversation_store=conversation_store,
        )

        async with lock:
            conv, binding_error = await _live_binding(request, session_id, envelope)
            stored = (conv.session_state if conv is not None else {}).get(STATE_KEY)
            state_matches = (
                isinstance(stored, dict)
                and stored.get("authority_id") == state["authority_id"]
                and stored.get("envelope_digest") == state["envelope_digest"]
                and stored.get("envelope_digest") == envelope_digest(envelope)
                and stored.get("envelope") == envelope.model_dump(mode="json")
                and stored.get("status") == "pending"
            )
            if not state_matches or conv is None:
                return {"status": "BLOCKED", "reason": "changed_envelope"}

            assert isinstance(stored, dict)
            stored = dict(stored)
            approved = result is not None and result.action == "accept" and binding_error is None
            stored["status"] = "approved" if approved else "denied"
            if binding_error is not None:
                stored["denial_reason"] = binding_error
            session_state = dict(conv.session_state)
            session_state[STATE_KEY] = stored
            await asyncio.to_thread(
                conversation_store.set_session_state,
                session_id,
                session_state,
            )

        if not approved:
            if binding_error is not None:
                reason = binding_error
            elif result is not None:
                reason = result.action
            else:
                reason = "disconnect" if await request.is_disconnected() else "timeout"
            return {
                "status": "BLOCKED",
                "reason": reason,
                "authority_id": state["authority_id"],
            }
        return {
            "status": "APPROVED",
            "authority_id": state["authority_id"],
            "envelope_digest": state["envelope_digest"],
            "expires_at": state["expires_at"],
        }

    async def _checkpoint_authority(
        request: Request,
        session_id: str,
        raw: Any,
    ) -> dict[str, Any]:
        try:
            checkpoint = AuthorityCheckpoint.model_validate(raw)
        except ValidationError:
            return {"status": "BLOCKED", "reason": "unsupported_protocol_or_checkpoint"}

        lock = _roadmap_authority_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            stored = (conv.session_state if conv is not None else {}).get(STATE_KEY)
            if not isinstance(stored, dict):
                return {"status": "BLOCKED", "reason": "missing_approval"}
            try:
                envelope = AuthorityRequest.model_validate(stored.get("envelope"))
            except ValidationError:
                return {"status": "BLOCKED", "reason": "unsupported_protocol"}
            conv, binding_error = await _live_binding(request, session_id, envelope)
            if binding_error is not None or conv is None:
                return {"status": "BLOCKED", "reason": binding_error or "unsupported_identity"}
            updated, error = checkpoint_state(stored, checkpoint)
            if error is not None or updated is None:
                return {"status": "BLOCKED", "reason": error or "invalid_checkpoint"}
            session_state = dict(conv.session_state)
            session_state[STATE_KEY] = updated
            await asyncio.to_thread(
                conversation_store.set_session_state,
                session_id,
                session_state,
            )

        return {
            "status": "PERMITTED",
            "authority_id": updated["authority_id"],
            "envelope_digest": updated["envelope_digest"],
            "checkpoint": updated["used_checkpoints"],
            "remaining_budget_usd": envelope.max_budget_usd - updated["used_budget_usd"],
            "remaining_provider_calls": (
                envelope.max_provider_calls - updated["used_provider_calls"]
            ),
            "notice": (
                "This receipt reserves declared use inside Omnigent; it does not intercept "
                "or guarantee enforcement by arbitrary external tools."
            ),
        }

    @router.post(
        "/sessions/{session_id}/roadmap-authority",
        dependencies=[Depends(require_json_content_type)],
    )
    async def roadmap_authority(request: Request, session_id: str) -> dict[str, Any]:
        """Request owner approval or reserve one exact authority checkpoint."""
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"status": "BLOCKED", "reason": "malformed_json"}
        if not isinstance(payload, dict):
            return {"status": "BLOCKED", "reason": "malformed_request"}
        action = payload.get("action")
        if action == "request":
            return await _request_authority(request, session_id, payload.get("request"))
        if action == "checkpoint":
            return await _checkpoint_authority(request, session_id, payload.get("checkpoint"))
        return {"status": "BLOCKED", "reason": "unsupported_protocol_action"}

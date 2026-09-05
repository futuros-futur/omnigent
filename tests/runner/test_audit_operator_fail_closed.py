"""Fail-closed regression coverage for the privileged Audit operator."""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.runner.app import _warn_unresolved_sub_agent


def test_unresolved_audit_runtime_operator_fails_closed() -> None:
    """A missing privileged child must never run as a CLAI parent clone."""
    with pytest.raises(OmnigentError, match="did not resolve exactly"):
        _warn_unresolved_sub_agent("session", "audit_runtime_operator")


def test_unresolved_ordinary_child_keeps_legacy_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The hard stop stays narrowly scoped to the privileged Audit child."""
    with caplog.at_level("WARNING"):
        _warn_unresolved_sub_agent("session", "ordinary_child")
    assert "falling back to the parent spec" in caplog.text

"""Tool schema for owner-approved roadmap authority envelopes."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysRoadmapAuthorityTool(Tool):
    """Request an authority envelope or reserve an in-envelope checkpoint."""

    @classmethod
    def name(cls) -> str:
        return "sys_roadmap_authority"

    @classmethod
    def description(cls) -> str:
        return (
            "Request the session owner's approval for a structured Audit, Memory, or "
            "WPLab roadmap envelope, then reserve exact checkpoints within that approved "
            "envelope. This coordinates authority; it does not enforce arbitrary external tools."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["request", "checkpoint"]},
                        "request": {"type": "object", "additionalProperties": True},
                        "checkpoint": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        }

"""PatchCommand model — high-level ergonomic commands compiled to BookPatch in Phase 3."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from epubforge.editor._validators import StrictModel, require_non_empty


class PatchCommand(StrictModel):
    """High-level ergonomic command. Compiled to BookPatch in Phase 3."""

    command_id: str
    op: str  # e.g. "split_block", "merge_blocks", "pair_footnote", etc.
    agent_id: str
    rationale: str
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("command_id")
    @classmethod
    def _validate_command_id(cls, value: str) -> str:
        from epubforge.editor._validators import validate_uuid4
        return validate_uuid4(value, field_name="command_id")

    @field_validator("op", "agent_id", "rationale")
    @classmethod
    def _validate_non_empty(cls, value: str, info) -> str:
        return require_non_empty(value, field_name=info.field_name)


__all__ = ["PatchCommand"]

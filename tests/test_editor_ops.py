"""Tests for the typed editor operation schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epubforge.editor.ops import InsertBlock, OpEnvelope


def _prov(page: int = 1) -> dict[str, object]:
    return {"page": page, "source": "passthrough"}


def _base_envelope(op: dict[str, object], **overrides: object) -> dict[str, object]:
    env: dict[str, object] = {
        "op_id": "550e8400-e29b-41d4-a716-446655440000",
        "ts": "2026-04-23T08:00:00Z",
        "agent_id": "agent-1",
        "base_version": 3,
        "preconditions": [],
        "op": op,
        "rationale": "test envelope",
    }
    env.update(overrides)
    return env


class TestOpEnvelopeRoundTrip:
    def test_round_trip_with_insert_block(self) -> None:
        env = OpEnvelope.model_validate(
            _base_envelope(
                {
                    "op": "insert_block",
                    "chapter_uid": "ch-1",
                    "after_uid": "blk-1",
                    "block_kind": "paragraph",
                    "new_block_uid": "blk-2",
                    "block_data": {
                        "text": "Inserted paragraph.",
                        "role": "body",
                        "provenance": _prov(),
                    },
                },
                preconditions=[
                    {"kind": "chapter_exists", "chapter_uid": "ch-1"},
                    {"kind": "version_at_least", "min_version": 3},
                ],
            )
        )

        restored = OpEnvelope.model_validate_json(env.model_dump_json())

        assert restored == env
        assert isinstance(restored.op, InsertBlock)
        assert restored.op.block_data["text"] == "Inserted paragraph."
        assert restored.preconditions[0].kind == "chapter_exists"

    def test_round_trip_with_meta_ops(self) -> None:
        compact_env = OpEnvelope.model_validate(
            _base_envelope(
                {
                    "op": "compact_marker",
                    "compacted_at_version": 12,
                    "archive_path": "log.archive/2026-04-23T08-00-00Z",
                    "archived_op_count": 47,
                },
                applied_version=3,
                applied_at="2026-04-23T08:00:01Z",
            )
        )
        revert_env = OpEnvelope.model_validate(
            _base_envelope(
                {"op": "revert", "target_op_id": "123e4567-e89b-42d3-a456-426614174000"},
                applied_version=3,
                applied_at="2026-04-23T08:00:02Z",
            )
        )

        assert OpEnvelope.model_validate_json(compact_env.model_dump_json()) == compact_env
        assert OpEnvelope.model_validate_json(revert_env.model_dump_json()) == revert_env


class TestValidators:
    def test_accepts_uuid4_and_utc_timestamps(self) -> None:
        env = OpEnvelope.model_validate(
            _base_envelope(
                {"op": "noop", "purpose": "milestone"},
                applied_version=3,
                applied_at="2026-04-23T08:00:01Z",
            )
        )

        assert env.op_id == "550e8400-e29b-41d4-a716-446655440000"
        assert env.ts == "2026-04-23T08:00:00Z"
        assert env.applied_at == "2026-04-23T08:00:01Z"

    @pytest.mark.parametrize("op_id", ["not-a-uuid", "550e8400-e29b-11d4-a716-446655440000"])
    def test_rejects_invalid_op_id_format(self, op_id: str) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {"op": "noop", "purpose": "milestone"},
                    op_id=op_id,
                )
            )

    @pytest.mark.parametrize("target_op_id", ["not-a-uuid", "550e8400-e29b-11d4-a716-446655440000"])
    def test_rejects_invalid_revert_target_op_id_format(self, target_op_id: str) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {"op": "revert", "target_op_id": target_op_id},
                )
            )

    @pytest.mark.parametrize("field,value", [("ts", "2026-04-23 08:00:00"), ("ts", "2026-04-23T08:00:00+08:00"), ("applied_at", "2026-04-23T08:00:01")])
    def test_rejects_non_utc_iso_timestamps(self, field: str, value: str) -> None:
        overrides: dict[str, object] = {}
        if field == "applied_at":
            overrides["applied_version"] = 3
            overrides["applied_at"] = value
        else:
            overrides["applied_version"] = 3
            overrides["applied_at"] = "2026-04-23T08:00:01Z"
            overrides["ts"] = value

        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {"op": "noop", "purpose": "milestone"},
                    **overrides,
                )
            )

    def test_rejects_generic_set_field_escape_hatch(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {
                        "op": "set_field",
                        "block_uid": "blk-1",
                        "field": "text",
                        "value": "bad",
                    }
                )
            )

    @pytest.mark.parametrize("field", ["uid", "kind"])
    def test_insert_block_rejects_block_data_identity_fields(self, field: str) -> None:
        block_data = {
            "text": "Inserted paragraph.",
            "role": "body",
            "provenance": _prov(),
            field: "bad-value",
        }

        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {
                        "op": "insert_block",
                        "chapter_uid": "ch-1",
                        "after_uid": None,
                        "block_kind": "paragraph",
                        "new_block_uid": "blk-new",
                        "block_data": block_data,
                    }
                )
            )

    def test_set_heading_level_only_accepts_1_to_3(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {"op": "set_heading_level", "block_uid": "h-1", "value": 4}
                )
            )

    def test_set_footnote_flag_requires_at_least_one_flag(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {"op": "set_footnote_flag", "block_uid": "fn-1"}
                )
            )

    def test_precondition_kind_is_closed_and_shape_checked(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {"op": "noop", "purpose": "milestone"},
                    preconditions=[{"kind": "unknown_kind", "block_uid": "blk-1"}],
                )
            )

        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {"op": "noop", "purpose": "milestone"},
                    preconditions=[{"kind": "block_exists", "block_uid": "blk-1", "min_version": 1}],
                )
            )

    def test_split_block_requires_strategy_specific_arguments(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {
                        "op": "split_block",
                        "block_uid": "blk-1",
                        "strategy": "at_line_index",
                        "max_splits": 1,
                        "new_block_uids": ["blk-2"],
                    }
                )
            )

        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {
                        "op": "split_block",
                        "block_uid": "blk-1",
                        "strategy": "at_marker",
                        "marker_occurrence": 1,
                        "max_splits": 2,
                        "new_block_uids": ["blk-2"],
                    }
                )
            )

    def test_merge_chapters_requires_uid_payloads_and_aligned_sections(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {
                        "op": "merge_chapters",
                        "source_chapter_uids": ["ch-1", "ch-2"],
                        "new_title": "Merged",
                        "sections": [
                            {"text": "One", "new_block_uid": "h-1"},
                            {"text": "Two", "new_block_uid": "h-2"},
                        ],
                    }
                )
            )

        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {
                        "op": "merge_chapters",
                        "source_chapter_uids": ["ch-1", "ch-2"],
                        "new_title": "Merged",
                        "new_chapter_uid": "ch-merged",
                        "sections": [{"text": "Only one", "new_block_uid": "h-1"}],
                    }
                )
            )

    def test_heading_spec_requires_new_block_uid(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {
                        "op": "merge_chapters",
                        "source_chapter_uids": ["ch-1", "ch-2"],
                        "new_title": "Merged",
                        "new_chapter_uid": "ch-merged",
                        "sections": [
                            {"text": "One"},
                            {"text": "Two", "new_block_uid": "h-2"},
                        ],
                    }
                )
            )

    def test_split_chapter_requires_new_chapter_uid(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {
                        "op": "split_chapter",
                        "chapter_uid": "ch-1",
                        "split_at_block_uid": "blk-9",
                        "new_chapter_title": "Part 2",
                    }
                )
            )

    def test_envelope_rejects_reverted_by_backfill(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {"op": "noop", "purpose": "milestone"},
                    reverted_by="op-99",
                )
            )

    def test_irreversible_flag_is_derived_for_topology_and_all_merge_blocks(self) -> None:
        relocate_env = OpEnvelope.model_validate(
            _base_envelope(
                {
                    "op": "relocate_block",
                    "block_uid": "blk-1",
                    "target_chapter_uid": "ch-2",
                    "after_uid": None,
                }
            )
        )
        merge_env = OpEnvelope.model_validate(
            _base_envelope(
                {
                    "op": "merge_blocks",
                    "block_uids": ["blk-1", "blk-2"],
                    "join": "cjk",
                    "target_field": "text",
                }
            )
        )
        merge_with_snapshot_env = OpEnvelope.model_validate(
            _base_envelope(
                {
                    "op": "merge_blocks",
                    "block_uids": ["blk-1", "blk-2"],
                    "join": "cjk",
                    "target_field": "text",
                    "original_blocks": [
                        {
                            "kind": "paragraph",
                            "uid": "blk-1",
                            "text": "First",
                            "role": "body",
                            "provenance": _prov(),
                        },
                        {
                            "kind": "paragraph",
                            "uid": "blk-2",
                            "text": "Second",
                            "role": "body",
                            "provenance": _prov(),
                        },
                    ],
                }
            )
        )

        assert relocate_env.irreversible is True
        assert merge_env.irreversible is True
        assert merge_with_snapshot_env.irreversible is True

    def test_merge_blocks_snapshot_must_align_with_source_uids(self) -> None:
        with pytest.raises(ValidationError):
            OpEnvelope.model_validate(
                _base_envelope(
                    {
                        "op": "merge_blocks",
                        "block_uids": ["blk-1", "blk-2"],
                        "join": "cjk",
                        "target_field": "text",
                        "original_blocks": [
                            {
                                "kind": "paragraph",
                                "uid": "blk-2",
                                "text": "Second",
                                "role": "body",
                                "provenance": _prov(),
                            },
                            {
                                "kind": "paragraph",
                                "uid": "blk-1",
                                "text": "First",
                                "role": "body",
                                "provenance": _prov(),
                            },
                        ],
                    }
                )
            )

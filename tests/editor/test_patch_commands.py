"""Comprehensive unit tests for epubforge.editor.patch_commands (Phase 3 schema)."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from epubforge.editor.patch_commands import (
    MarkOrphanParams,
    MergeBlocksParams,
    MergeChapterSection,
    MergeChaptersParams,
    PairFootnoteParams,
    PatchCommand,
    PatchCommandError,
    PatchCommandOp,
    RelocateBlockParams,
    SplitBlockParams,
    SplitChapterParams,
    SplitMergedTableParams,
    UnpairFootnoteParams,
    command_params,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cid() -> str:
    return str(uuid4())


_SPLIT_BLOCK_PARAMS = {
    "block_uid": "blk-001",
    "strategy": "at_sentence",
    "max_splits": 1,
    "new_block_uids": ["blk-002"],
}

_MERGE_BLOCKS_PARAMS = {
    "block_uids": ["blk-001", "blk-002"],
    "join": "concat",
}

_SPLIT_CHAPTER_PARAMS = {
    "chapter_uid": "ch-001",
    "split_at_block_uid": "blk-005",
    "new_chapter_title": "Part Two",
    "new_chapter_uid": "ch-002",
}

_MERGE_CHAPTERS_PARAMS = {
    "source_chapter_uids": ["ch-001", "ch-002"],
    "new_title": "Combined",
    "new_chapter_uid": "ch-merged",
    "sections": [
        {"text": "Intro text", "new_block_uid": "blk-x1"},
        {"text": "Body text", "new_block_uid": "blk-x2"},
    ],
}

_RELOCATE_BLOCK_PARAMS = {
    "block_uid": "blk-001",
    "target_chapter_uid": "ch-002",
}

_PAIR_FOOTNOTE_PARAMS = {
    "fn_block_uid": "fn-001",
    "source_block_uid": "blk-010",
}

_UNPAIR_FOOTNOTE_PARAMS = {
    "fn_block_uid": "fn-001",
}

_MARK_ORPHAN_PARAMS = {
    "fn_block_uid": "fn-001",
}

_SPLIT_MERGED_TABLE_PARAMS = {
    "block_uid": "blk-tbl",
    "segment_html": ["<table>A</table>", "<table>B</table>"],
    "segment_pages": [1, 2],
    "new_block_uids": ["blk-t1", "blk-t2"],
}


# ---------------------------------------------------------------------------
# §1 Valid construction for all 9 ops
# ---------------------------------------------------------------------------


class TestValidConstruction:
    def test_split_block(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="split_block",
            agent_id="fixer-1",
            rationale="Split at sentence boundary",
            params=_SPLIT_BLOCK_PARAMS,
        )
        assert cmd.op == "split_block"
        assert cmd.params["block_uid"] == "blk-001"

    def test_split_block_with_line_index(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="split_block",
            agent_id="fixer-1",
            rationale="Split at line",
            params={
                "block_uid": "blk-001",
                "strategy": "at_line_index",
                "line_index": 5,
                "max_splits": 1,
                "new_block_uids": ["blk-002"],
            },
        )
        assert cmd.params["line_index"] == 5

    def test_split_block_multiple_splits(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="split_block",
            agent_id="fixer-1",
            rationale="Split 3 ways",
            params={
                "block_uid": "blk-001",
                "strategy": "at_marker",
                "max_splits": 3,
                "new_block_uids": ["b2", "b3", "b4"],
            },
        )
        assert len(cmd.params["new_block_uids"]) == 3

    def test_merge_blocks(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="merge_blocks",
            agent_id="fixer-1",
            rationale="Merge two adjacent blocks",
            params=_MERGE_BLOCKS_PARAMS,
        )
        assert cmd.op == "merge_blocks"

    def test_merge_blocks_cjk(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="merge_blocks",
            agent_id="fixer-1",
            rationale="CJK merge",
            params={
                "block_uids": ["blk-001", "blk-002", "blk-003"],
                "join": "cjk",
            },
        )
        assert cmd.params["join"] == "cjk"

    def test_split_chapter(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="split_chapter",
            agent_id="fixer-1",
            rationale="Chapter too long",
            params=_SPLIT_CHAPTER_PARAMS,
        )
        assert cmd.op == "split_chapter"

    def test_merge_chapters(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="merge_chapters",
            agent_id="fixer-1",
            rationale="Combine short chapters",
            params=_MERGE_CHAPTERS_PARAMS,
        )
        assert cmd.op == "merge_chapters"

    def test_relocate_block(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="relocate_block",
            agent_id="fixer-1",
            rationale="Move block to next chapter",
            params=_RELOCATE_BLOCK_PARAMS,
        )
        assert cmd.op == "relocate_block"

    def test_relocate_block_with_after_uid(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="relocate_block",
            agent_id="fixer-1",
            rationale="Move block after specific block",
            params={
                "block_uid": "blk-001",
                "target_chapter_uid": "ch-002",
                "after_uid": "blk-anchor",
            },
        )
        assert cmd.params["after_uid"] == "blk-anchor"

    def test_pair_footnote(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="Pair orphan footnote",
            params=_PAIR_FOOTNOTE_PARAMS,
        )
        assert cmd.op == "pair_footnote"

    def test_unpair_footnote(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="unpair_footnote",
            agent_id="fixer-1",
            rationale="Remove incorrect pairing",
            params=_UNPAIR_FOOTNOTE_PARAMS,
        )
        assert cmd.op == "unpair_footnote"

    def test_mark_orphan(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="mark_orphan",
            agent_id="fixer-1",
            rationale="Mark unresolvable footnote",
            params=_MARK_ORPHAN_PARAMS,
        )
        assert cmd.op == "mark_orphan"

    def test_split_merged_table(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="Split table spanning pages",
            params=_SPLIT_MERGED_TABLE_PARAMS,
        )
        assert cmd.op == "split_merged_table"


# ---------------------------------------------------------------------------
# §2 Unknown op rejection
# ---------------------------------------------------------------------------


class TestUnknownOp:
    def test_unknown_op_rejected(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="unknown_op",  # type: ignore[arg-type]
                agent_id="fixer-1",
                rationale="Should fail",
                params={},
            )

    def test_empty_op_rejected(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="",  # type: ignore[arg-type]
                agent_id="fixer-1",
                rationale="Should fail",
                params={},
            )

    def test_partial_op_name_rejected(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split",  # type: ignore[arg-type]
                agent_id="fixer-1",
                rationale="Should fail",
                params={},
            )


# ---------------------------------------------------------------------------
# §3 Missing required params
# ---------------------------------------------------------------------------


class TestMissingRequiredParams:
    def test_split_block_missing_block_uid(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "strategy": "at_sentence",
                    "max_splits": 1,
                    "new_block_uids": ["b2"],
                },
            )

    def test_split_block_missing_strategy(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "blk-001",
                    "max_splits": 1,
                    "new_block_uids": ["b2"],
                },
            )

    def test_split_block_missing_new_block_uids(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "blk-001",
                    "strategy": "at_sentence",
                    "max_splits": 1,
                },
            )

    def test_merge_blocks_missing_block_uids(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="merge_blocks",
                agent_id="fixer-1",
                rationale="x",
                params={},
            )

    def test_split_chapter_missing_chapter_uid(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_chapter",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "split_at_block_uid": "blk-x",
                    "new_chapter_title": "T",
                    "new_chapter_uid": "ch-x",
                },
            )

    def test_merge_chapters_missing_source_uids(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="merge_chapters",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "new_title": "T",
                    "new_chapter_uid": "ch-x",
                    "sections": [{"text": "A", "new_block_uid": "b1"}],
                },
            )

    def test_relocate_block_missing_block_uid(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="relocate_block",
                agent_id="fixer-1",
                rationale="x",
                params={"target_chapter_uid": "ch-002"},
            )

    def test_pair_footnote_missing_fn_uid(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="pair_footnote",
                agent_id="fixer-1",
                rationale="x",
                params={"source_block_uid": "blk-001"},
            )

    def test_unpair_footnote_missing_fn_uid(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="unpair_footnote",
                agent_id="fixer-1",
                rationale="x",
                params={},
            )

    def test_mark_orphan_missing_fn_uid(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="mark_orphan",
                agent_id="fixer-1",
                rationale="x",
                params={},
            )

    def test_split_merged_table_missing_block_uid(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_merged_table",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "segment_html": ["<t>A</t>", "<t>B</t>"],
                    "segment_pages": [1, 2],
                    "new_block_uids": ["b1", "b2"],
                },
            )


# ---------------------------------------------------------------------------
# §4 Wrong param types
# ---------------------------------------------------------------------------


class TestWrongParamTypes:
    def test_split_block_uid_as_int(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": 123,  # should be str
                    "strategy": "at_sentence",
                    "max_splits": 1,
                    "new_block_uids": ["b2"],
                },
            )

    def test_split_block_max_splits_as_string(self):
        # Pydantic may coerce or reject; with StrictModel's int field, string should fail
        with pytest.raises(ValidationError):
            SplitBlockParams.model_validate({
                "block_uid": "blk-001",
                "strategy": "at_sentence",
                "max_splits": "one",  # should be int
                "new_block_uids": ["b2"],
            })

    def test_merge_blocks_join_invalid_value(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="merge_blocks",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uids": ["b1", "b2"],
                    "join": "space",  # not in Literal
                },
            )

    def test_split_block_strategy_invalid(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "blk-001",
                    "strategy": "by_magic",  # invalid literal
                    "max_splits": 1,
                    "new_block_uids": ["b2"],
                },
            )


# ---------------------------------------------------------------------------
# §5 Extra params rejected (StrictModel)
# ---------------------------------------------------------------------------


class TestExtraParamsRejected:
    def test_split_block_extra_field(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "blk-001",
                    "strategy": "at_sentence",
                    "max_splits": 1,
                    "new_block_uids": ["b2"],
                    "extra_field": "oops",  # forbidden
                },
            )

    def test_merge_blocks_extra_field(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="merge_blocks",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uids": ["b1", "b2"],
                    "extra": True,
                },
            )

    def test_pair_footnote_extra_field(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="pair_footnote",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "fn_block_uid": "fn-001",
                    "source_block_uid": "blk-010",
                    "bogus": "field",
                },
            )


# ---------------------------------------------------------------------------
# §6 PatchCommandError construction and attributes
# ---------------------------------------------------------------------------


class TestPatchCommandError:
    def test_basic_attributes(self):
        err = PatchCommandError(reason="params invalid", command_id="cmd-abc")
        assert err.reason == "params invalid"
        assert err.command_id == "cmd-abc"

    def test_str_representation(self):
        err = PatchCommandError(reason="missing block_uid", command_id="cmd-xyz")
        assert "cmd-xyz" in str(err)
        assert "missing block_uid" in str(err)

    def test_is_runtime_error(self):
        err = PatchCommandError(reason="boom", command_id="cmd-1")
        assert isinstance(err, RuntimeError)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(PatchCommandError) as exc_info:
            raise PatchCommandError(reason="test error", command_id="cmd-test")
        assert exc_info.value.command_id == "cmd-test"


# ---------------------------------------------------------------------------
# §7 command_params() returns correct typed model
# ---------------------------------------------------------------------------


class TestCommandParams:
    def test_split_block_returns_split_block_params(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="split_block",
            agent_id="fixer-1",
            rationale="x",
            params=_SPLIT_BLOCK_PARAMS,
        )
        result = command_params(cmd)
        assert isinstance(result, SplitBlockParams)
        assert result.block_uid == "blk-001"
        assert result.strategy == "at_sentence"

    def test_merge_blocks_returns_merge_blocks_params(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="merge_blocks",
            agent_id="fixer-1",
            rationale="x",
            params=_MERGE_BLOCKS_PARAMS,
        )
        result = command_params(cmd)
        assert isinstance(result, MergeBlocksParams)
        assert result.join == "concat"

    def test_split_chapter_returns_split_chapter_params(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="split_chapter",
            agent_id="fixer-1",
            rationale="x",
            params=_SPLIT_CHAPTER_PARAMS,
        )
        result = command_params(cmd)
        assert isinstance(result, SplitChapterParams)
        assert result.chapter_uid == "ch-001"

    def test_merge_chapters_returns_merge_chapters_params(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="merge_chapters",
            agent_id="fixer-1",
            rationale="x",
            params=_MERGE_CHAPTERS_PARAMS,
        )
        result = command_params(cmd)
        assert isinstance(result, MergeChaptersParams)
        assert result.new_title == "Combined"
        assert len(result.sections) == 2

    def test_relocate_block_returns_relocate_block_params(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="relocate_block",
            agent_id="fixer-1",
            rationale="x",
            params=_RELOCATE_BLOCK_PARAMS,
        )
        result = command_params(cmd)
        assert isinstance(result, RelocateBlockParams)
        assert result.after_uid is None

    def test_pair_footnote_returns_pair_footnote_params(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="x",
            params=_PAIR_FOOTNOTE_PARAMS,
        )
        result = command_params(cmd)
        assert isinstance(result, PairFootnoteParams)
        assert result.occurrence_index == 0

    def test_unpair_footnote_returns_unpair_footnote_params(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="unpair_footnote",
            agent_id="fixer-1",
            rationale="x",
            params=_UNPAIR_FOOTNOTE_PARAMS,
        )
        result = command_params(cmd)
        assert isinstance(result, UnpairFootnoteParams)
        assert result.fn_block_uid == "fn-001"

    def test_mark_orphan_returns_mark_orphan_params(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="mark_orphan",
            agent_id="fixer-1",
            rationale="x",
            params=_MARK_ORPHAN_PARAMS,
        )
        result = command_params(cmd)
        assert isinstance(result, MarkOrphanParams)
        assert result.fn_block_uid == "fn-001"

    def test_split_merged_table_returns_split_merged_table_params(self):
        cmd = PatchCommand(
            command_id=_cid(),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="x",
            params=_SPLIT_MERGED_TABLE_PARAMS,
        )
        result = command_params(cmd)
        assert isinstance(result, SplitMergedTableParams)
        assert result.block_uid == "blk-tbl"
        assert len(result.segment_html) == 2


# ---------------------------------------------------------------------------
# §8 model_dump_json shape preserved (round-trip)
# ---------------------------------------------------------------------------


class TestJsonShapePreserved:
    def test_round_trip_split_block(self):
        cid = _cid()
        cmd = PatchCommand(
            command_id=cid,
            op="split_block",
            agent_id="fixer-1",
            rationale="Split this block in two",
            params=_SPLIT_BLOCK_PARAMS,
        )
        dumped = json.loads(cmd.model_dump_json())
        assert dumped["command_id"] == cid
        assert dumped["op"] == "split_block"
        assert dumped["agent_id"] == "fixer-1"
        assert dumped["rationale"] == "Split this block in two"
        assert isinstance(dumped["params"], dict)
        assert dumped["params"]["block_uid"] == "blk-001"
        assert dumped["params"]["strategy"] == "at_sentence"
        # No extra top-level keys
        assert set(dumped.keys()) == {"command_id", "op", "agent_id", "rationale", "params"}

    def test_json_key_order_shape(self):
        """Verify exact top-level keys match the Phase 2 shape."""
        cmd = PatchCommand(
            command_id=_cid(),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="Pair it",
            params=_PAIR_FOOTNOTE_PARAMS,
        )
        dumped = json.loads(cmd.model_dump_json())
        assert list(dumped.keys()) == ["command_id", "op", "agent_id", "rationale", "params"]

    def test_model_validate_from_json(self):
        """model_validate on round-tripped dict works."""
        cmd = PatchCommand(
            command_id=_cid(),
            op="merge_blocks",
            agent_id="fixer-1",
            rationale="Merge",
            params=_MERGE_BLOCKS_PARAMS,
        )
        data = json.loads(cmd.model_dump_json())
        restored = PatchCommand.model_validate(data)
        assert restored.op == cmd.op
        assert restored.params == cmd.params


# ---------------------------------------------------------------------------
# §9 Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_split_block_new_block_uids_length_mismatch(self):
        """new_block_uids length must equal max_splits."""
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "blk-001",
                    "strategy": "at_sentence",
                    "max_splits": 2,
                    "new_block_uids": ["b2"],  # length 1, but max_splits=2
                },
            )

    def test_split_block_new_block_uids_too_many(self):
        """new_block_uids length > max_splits also fails."""
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "blk-001",
                    "strategy": "at_marker",
                    "max_splits": 1,
                    "new_block_uids": ["b2", "b3"],  # length 2, but max_splits=1
                },
            )

    def test_split_block_max_splits_zero_rejected(self):
        """max_splits < 1 is invalid."""
        with pytest.raises(ValidationError):
            SplitBlockParams.model_validate({
                "block_uid": "blk-001",
                "strategy": "at_sentence",
                "max_splits": 0,
                "new_block_uids": [],
            })

    def test_merge_blocks_single_uid_rejected(self):
        """block_uids with fewer than 2 items must fail."""
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="merge_blocks",
                agent_id="fixer-1",
                rationale="x",
                params={"block_uids": ["blk-001"]},
            )

    def test_merge_blocks_empty_list_rejected(self):
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="merge_blocks",
                agent_id="fixer-1",
                rationale="x",
                params={"block_uids": []},
            )

    def test_split_merged_table_length_mismatch_html_vs_pages(self):
        """segment_html and segment_pages must have same length."""
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_merged_table",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "blk-tbl",
                    "segment_html": ["<t>A</t>", "<t>B</t>", "<t>C</t>"],
                    "segment_pages": [1, 2],  # shorter
                    "new_block_uids": ["b1", "b2", "b3"],
                },
            )

    def test_split_merged_table_length_mismatch_pages_vs_uids(self):
        """segment_pages and new_block_uids must have same length."""
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_merged_table",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "blk-tbl",
                    "segment_html": ["<t>A</t>", "<t>B</t>"],
                    "segment_pages": [1, 2],
                    "new_block_uids": ["b1"],  # shorter
                },
            )

    def test_split_merged_table_only_one_segment_rejected(self):
        """segment_html must contain at least 2 items."""
        with pytest.raises(ValidationError):
            SplitMergedTableParams.model_validate({
                "block_uid": "blk-tbl",
                "segment_html": ["<t>A</t>"],
                "segment_pages": [1],
                "new_block_uids": ["b1"],
            })

    def test_merge_chapters_source_chapter_uids_single_rejected(self):
        """source_chapter_uids must have at least 2 items."""
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="merge_chapters",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "source_chapter_uids": ["ch-001"],  # only 1
                    "new_title": "T",
                    "new_chapter_uid": "ch-x",
                    "sections": [{"text": "A", "new_block_uid": "b1"}],
                },
            )

    def test_split_block_empty_block_uid_rejected(self):
        """Empty string block_uid should fail."""
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "   ",  # whitespace-only
                    "strategy": "at_sentence",
                    "max_splits": 1,
                    "new_block_uids": ["b2"],
                },
            )

    def test_split_block_empty_new_block_uid_in_list_rejected(self):
        """Empty string inside new_block_uids should fail."""
        with pytest.raises(ValidationError):
            PatchCommand(
                command_id=_cid(),
                op="split_block",
                agent_id="fixer-1",
                rationale="x",
                params={
                    "block_uid": "blk-001",
                    "strategy": "at_sentence",
                    "max_splits": 1,
                    "new_block_uids": [""],  # empty string
                },
            )

    def test_merge_chapter_section_fields(self):
        """MergeChapterSection validates its own fields."""
        section = MergeChapterSection(text="Hello", new_block_uid="blk-x")
        assert section.id is None
        assert section.style_class is None

    def test_merge_chapter_section_empty_text_rejected(self):
        with pytest.raises(ValidationError):
            MergeChapterSection(text="", new_block_uid="blk-x")

    def test_merge_chapter_section_empty_new_block_uid_rejected(self):
        with pytest.raises(ValidationError):
            MergeChapterSection(text="Hello", new_block_uid="")

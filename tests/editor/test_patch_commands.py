"""Comprehensive unit tests for epubforge.editor.patch_commands (Phase 3 schema)."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from epubforge.editor.patch_commands import (
    CompiledCommands,
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
    _COMPILERS,
    _check_uid_collision,
    _find_block,
    _find_chapter,
    command_params,
    compile_patch_command,
    compile_patch_commands,
)
from epubforge.editor.patches import apply_book_patch
from epubforge.editor.text_split import split_text
from epubforge.ir.semantic import Book, Chapter, Footnote, Heading, Paragraph, Provenance, Table
from epubforge.markers import make_fn_marker


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


# ---------------------------------------------------------------------------
# §10 WP2 Compiler Infrastructure
# ---------------------------------------------------------------------------


def _make_book() -> Book:
    """Create a simple test book with known UIDs."""
    prov = Provenance(page=1, source="passthrough")
    return Book(
        title="Test",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                blocks=[
                    Paragraph(uid="blk-1", text="Hello", role="body", provenance=prov),
                    Paragraph(uid="blk-2", text="World", role="body", provenance=prov),
                ],
            ),
        ],
    )


class TestCompilerInfrastructure:
    def test_compile_empty_commands_returns_empty(self):
        """Compiling empty command list returns empty patches, same book."""
        book = _make_book()
        result = compile_patch_commands(
            book, [], output_kind="supervisor", output_chapter_uid=None
        )
        assert result.patches == []
        assert result.book_after_commands == book

    def test_compile_unimplemented_op_raises(self):
        """Compiling a command whose op compiler is not registered raises PatchCommandError.

        All 9 ops now have compilers, so we simulate an unregistered op by
        temporarily removing 'split_block' from the _COMPILERS registry.
        """
        book = _make_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_block",
            agent_id="fixer-1",
            rationale="test",
            params={
                "block_uid": "blk-1",
                "strategy": "at_sentence",
                "max_splits": 1,
                "new_block_uids": ["blk-new-1"],
            },
        )
        saved = _COMPILERS.pop("split_block")
        try:
            with pytest.raises(PatchCommandError) as exc_info:
                compile_patch_commands(
                    book, [cmd], output_kind="supervisor", output_chapter_uid=None
                )
            assert "not implemented" in str(exc_info.value)
            assert exc_info.value.command_id == cmd.command_id
        finally:
            _COMPILERS["split_block"] = saved

    def test_compile_result_is_compiled_commands(self):
        """compile_patch_commands returns a CompiledCommands instance."""
        book = _make_book()
        result = compile_patch_commands(
            book, [], output_kind="fixer", output_chapter_uid="ch-1"
        )
        assert isinstance(result, CompiledCommands)

    def test_find_block_helper(self):
        """_find_block returns correct chapter, block, index."""
        book = _make_book()
        chapter, block, idx = _find_block(book, "blk-1", "cmd-test")
        assert chapter.uid == "ch-1"
        assert block.uid == "blk-1"
        assert idx == 0

        chapter2, block2, idx2 = _find_block(book, "blk-2", "cmd-test")
        assert block2.uid == "blk-2"
        assert idx2 == 1

    def test_find_block_not_found_raises(self):
        """_find_block raises PatchCommandError for unknown uid."""
        book = _make_book()
        with pytest.raises(PatchCommandError) as exc_info:
            _find_block(book, "nonexistent-uid", "cmd-test")
        assert exc_info.value.command_id == "cmd-test"
        assert "blk" in str(exc_info.value) or "not found" in str(exc_info.value)

    def test_find_chapter_helper(self):
        """_find_chapter returns correct chapter and index."""
        book = _make_book()
        chapter, idx = _find_chapter(book, "ch-1", "cmd-test")
        assert chapter.uid == "ch-1"
        assert idx == 0

    def test_find_chapter_not_found_raises(self):
        """_find_chapter raises PatchCommandError for unknown uid."""
        book = _make_book()
        with pytest.raises(PatchCommandError) as exc_info:
            _find_chapter(book, "nonexistent-chapter", "cmd-test")
        assert exc_info.value.command_id == "cmd-test"
        assert "not found" in str(exc_info.value)

    def test_uid_collision_detected_chapter(self):
        """_check_uid_collision raises for an existing chapter uid."""
        book = _make_book()
        with pytest.raises(PatchCommandError) as exc_info:
            _check_uid_collision(book, "ch-1", "cmd-test")
        assert "already exists" in str(exc_info.value)
        assert exc_info.value.command_id == "cmd-test"

    def test_uid_collision_detected_block(self):
        """_check_uid_collision raises for an existing block uid."""
        book = _make_book()
        with pytest.raises(PatchCommandError) as exc_info:
            _check_uid_collision(book, "blk-1", "cmd-test")
        assert "already exists" in str(exc_info.value)

    def test_uid_no_collision(self):
        """_check_uid_collision does not raise for a fresh uid."""
        book = _make_book()
        # Should not raise
        _check_uid_collision(book, "brand-new-uid-xyz", "cmd-test")


# ---------------------------------------------------------------------------
# §11 WP3 Compiler: split_block
# ---------------------------------------------------------------------------


def _prov(page: int = 1) -> Provenance:
    return Provenance(page=page, source="passthrough")


def _make_test_book() -> Book:
    """Book with 2 chapters, several blocks each."""
    return Book(
        title="Test Book",
        uid_seed="test-seed",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter 1",
                blocks=[
                    Paragraph(uid="p1", text="Hello world. This is a test.", role="body", provenance=_prov()),
                    Paragraph(uid="p2", text="Second paragraph.", role="body", provenance=_prov()),
                    Paragraph(uid="p3", text="Third paragraph.", role="body", provenance=_prov()),
                ],
            ),
            Chapter(
                uid="ch-2",
                title="Chapter 2",
                blocks=[
                    Paragraph(uid="p4", text="Chapter two text.", role="body", provenance=_prov()),
                ],
            ),
        ],
    )


class TestSplitBlockCompiler:
    def test_split_at_sentence(self):
        """Valid split_block at_sentence produces correct changes."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_block",
            agent_id="fixer-1",
            rationale="Split at sentence",
            params={
                "block_uid": "p1",
                "strategy": "at_sentence",
                "max_splits": 1,
                "new_block_uids": ["p1-b"],
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-1"
        )
        # Should have 2 changes: set_field + insert_node
        assert len(patch.changes) == 2
        assert patch.scope.chapter_uid == "ch-1"

    def test_split_at_text_match(self):
        """Valid split_block at_text_match produces correct changes."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_block",
            agent_id="fixer-1",
            rationale="Split at text match",
            params={
                "block_uid": "p1",
                "strategy": "at_text_match",
                "text_match": "This is",
                "max_splits": 1,
                "new_block_uids": ["p1-b"],
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-1"
        )
        assert len(patch.changes) == 2
        # Verify the set_field change has correct old and new values
        set_field = patch.changes[0]
        assert set_field.op == "set_field"
        assert set_field.old == "Hello world. This is a test."  # full original text
        assert set_field.new == "Hello world. "  # first segment (up to "This is")

    def test_split_block_on_heading_with_text(self):
        """split_block works on a Heading block (which has a 'text' field)."""
        prov = _prov()
        book = Book(
            title="T",
            chapters=[
                Chapter(
                    uid="ch-x",
                    title="X",
                    blocks=[
                        Heading(uid="h1", text="First sentence. Second sentence.", level=1, provenance=prov),
                    ],
                )
            ],
        )
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_block",
            agent_id="fixer-1",
            rationale="test",
            params={
                "block_uid": "h1",
                "strategy": "at_sentence",
                "max_splits": 1,
                "new_block_uids": ["h2"],
            },
        )
        # Heading has text field — compiler should succeed
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-x"
        )
        assert patch is not None
        assert len(patch.changes) == 2

    def test_new_block_uid_collision_fails(self):
        """new_block_uids that collide with existing UIDs raises PatchCommandError."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_block",
            agent_id="fixer-1",
            rationale="test",
            params={
                "block_uid": "p1",
                "strategy": "at_sentence",
                "max_splits": 1,
                "new_block_uids": ["p2"],  # p2 already exists
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(
                book, cmd, output_kind="fixer", output_chapter_uid="ch-1"
            )
        assert "already exists" in str(exc_info.value)

    def test_compiled_patch_applies(self):
        """split_block compiled patch can be applied via apply_book_patch."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_block",
            agent_id="fixer-1",
            rationale="Split at sentence boundary",
            params={
                "block_uid": "p1",
                "strategy": "at_sentence",
                "max_splits": 1,
                "new_block_uids": ["p1-new"],
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-1"
        )
        new_book = apply_book_patch(book, patch)
        # ch-1 should now have 4 blocks (was 3, +1 from split)
        assert len(new_book.chapters[0].blocks) == 4
        # Verify the new block uid exists
        uids = [b.uid for b in new_book.chapters[0].blocks]
        assert "p1-new" in uids


# ---------------------------------------------------------------------------
# §12 WP3 Compiler: merge_blocks
# ---------------------------------------------------------------------------


class TestMergeBlocksCompiler:
    def test_merge_two_adjacent_blocks_concat(self):
        """Valid merge_blocks of 2 adjacent blocks with concat join."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_blocks",
            agent_id="fixer-1",
            rationale="Merge adjacent",
            params={
                "block_uids": ["p1", "p2"],
                "join": "concat",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-1"
        )
        # Should have 2 changes: set_field + delete_node
        assert len(patch.changes) == 2
        assert patch.scope.chapter_uid == "ch-1"
        assert patch.changes[0].op == "set_field"
        assert patch.changes[1].op == "delete_node"

    def test_merge_with_cjk_join(self):
        """merge_blocks with cjk join produces correct merged text."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_blocks",
            agent_id="fixer-1",
            rationale="CJK merge",
            params={
                "block_uids": ["p2", "p3"],
                "join": "cjk",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-1"
        )
        assert len(patch.changes) == 2
        # The new text should be the cjk_join of "Second paragraph." and "Third paragraph."
        set_change = patch.changes[0]
        assert set_change.op == "set_field"
        assert "Second paragraph" in set_change.new
        assert "Third paragraph" in set_change.new

    def test_blocks_in_different_chapters_fails(self):
        """merge_blocks across different chapters raises PatchCommandError."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_blocks",
            agent_id="fixer-1",
            rationale="test",
            params={
                "block_uids": ["p3", "p4"],  # p3 in ch-1, p4 in ch-2
                "join": "concat",
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(
                book, cmd, output_kind="fixer", output_chapter_uid=None
            )
        assert "same chapter" in str(exc_info.value)

    def test_non_contiguous_blocks_fails(self):
        """merge_blocks with non-contiguous blocks raises PatchCommandError."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_blocks",
            agent_id="fixer-1",
            rationale="test",
            params={
                "block_uids": ["p1", "p3"],  # p1 and p3 are not adjacent (p2 is between)
                "join": "concat",
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(
                book, cmd, output_kind="fixer", output_chapter_uid="ch-1"
            )
        assert "contiguous" in str(exc_info.value)

    def test_compiled_merge_applies(self):
        """merge_blocks compiled patch can be applied via apply_book_patch."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_blocks",
            agent_id="fixer-1",
            rationale="Merge blocks",
            params={
                "block_uids": ["p1", "p2"],
                "join": "concat",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-1"
        )
        new_book = apply_book_patch(book, patch)
        # ch-1 should now have 2 blocks (was 3, -1 from merge)
        assert len(new_book.chapters[0].blocks) == 2
        # p2 should be gone
        uids = [b.uid for b in new_book.chapters[0].blocks]
        assert "p2" not in uids
        assert "p1" in uids


# ---------------------------------------------------------------------------
# §13 WP3 Compiler: relocate_block
# ---------------------------------------------------------------------------


class TestRelocateBlockCompiler:
    def test_same_chapter_move(self):
        """relocate_block within same chapter produces chapter scope."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="relocate_block",
            agent_id="fixer-1",
            rationale="Reorder within chapter",
            params={
                "block_uid": "p1",
                "target_chapter_uid": "ch-1",
                "after_uid": "p3",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-1"
        )
        assert patch.scope.chapter_uid == "ch-1"
        assert len(patch.changes) == 1
        assert patch.changes[0].op == "move_node"

    def test_cross_chapter_move_book_scope(self):
        """relocate_block across chapters produces book-wide scope (chapter_uid=None)."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="relocate_block",
            agent_id="fixer-1",
            rationale="Move to chapter 2",
            params={
                "block_uid": "p1",
                "target_chapter_uid": "ch-2",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid=None
        )
        assert patch.scope.chapter_uid is None
        assert len(patch.changes) == 1
        assert patch.changes[0].op == "move_node"

    def test_after_uid_not_in_target_chapter_fails(self):
        """after_uid not in target chapter raises PatchCommandError."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="relocate_block",
            agent_id="fixer-1",
            rationale="test",
            params={
                "block_uid": "p1",
                "target_chapter_uid": "ch-2",
                "after_uid": "p2",  # p2 is in ch-1, not ch-2
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(
                book, cmd, output_kind="fixer", output_chapter_uid=None
            )
        assert "after_uid" in str(exc_info.value)

    def test_compiled_relocate_applies(self):
        """relocate_block compiled patch can be applied via apply_book_patch."""
        book = _make_test_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="relocate_block",
            agent_id="fixer-1",
            rationale="Move p1 to chapter 2",
            params={
                "block_uid": "p1",
                "target_chapter_uid": "ch-2",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid=None
        )
        new_book = apply_book_patch(book, patch)
        # ch-1 should have 2 blocks now (was 3)
        assert len(new_book.chapters[0].blocks) == 2
        # ch-2 should have 2 blocks now (was 1)
        assert len(new_book.chapters[1].blocks) == 2
        ch2_uids = [b.uid for b in new_book.chapters[1].blocks]
        assert "p1" in ch2_uids


# ---------------------------------------------------------------------------
# §14 text_split.split_text unit tests
# ---------------------------------------------------------------------------


class TestSplitText:
    def test_at_sentence_basic(self):
        """at_sentence splits text at sentence boundary."""
        result = split_text(
            "Hello world. This is second.",
            strategy="at_sentence",
            max_splits=1,
        )
        assert len(result) == 2
        assert result[0] == "Hello world. "
        assert result[1] == "This is second."

    def test_at_text_match_basic(self):
        """at_text_match splits at the given substring."""
        result = split_text(
            "Hello world. This is second.",
            strategy="at_text_match",
            text_match="This",
        )
        assert len(result) == 2
        assert result[0] == "Hello world. "
        assert result[1] == "This is second."

    def test_at_text_match_no_match_fails(self):
        """at_text_match raises ValueError when text_match not found."""
        with pytest.raises(ValueError, match="not found"):
            split_text("Hello world.", strategy="at_text_match", text_match="xyz")

    def test_at_text_match_requires_text_match(self):
        """at_text_match raises ValueError when text_match is None."""
        with pytest.raises(ValueError, match="text_match is required"):
            split_text("Hello world.", strategy="at_text_match")

    def test_at_line_index_basic(self):
        """at_line_index splits at the given line boundary."""
        result = split_text(
            "line1\nline2\nline3",
            strategy="at_line_index",
            line_index=0,
            display_lines=["line1", "line2", "line3"],
        )
        assert len(result) == 2
        assert result[0] == "line1"
        assert result[1] == "line2\nline3"

    def test_at_line_index_requires_display_lines(self):
        """at_line_index raises ValueError when display_lines is None."""
        with pytest.raises(ValueError, match="display_lines"):
            split_text("hello", strategy="at_line_index", line_index=0)

    def test_at_sentence_not_enough_breaks_fails(self):
        """at_sentence raises ValueError if not enough sentence breaks."""
        with pytest.raises(ValueError, match="could not produce"):
            split_text("No sentence break here", strategy="at_sentence", max_splits=1)

    def test_at_sentence_multiple_splits(self):
        """at_sentence with max_splits=2 produces 3 segments."""
        result = split_text(
            "First. Second. Third.",
            strategy="at_sentence",
            max_splits=2,
        )
        assert len(result) == 3


# ---------------------------------------------------------------------------
# §15 WP4 Compiler: split_chapter
# ---------------------------------------------------------------------------


def _make_split_chapter_book() -> Book:
    """Book with one chapter containing 3 blocks, for split_chapter tests."""
    prov = _prov()
    return Book(
        title="Split Test",
        chapters=[
            Chapter(
                uid="ch-A",
                title="Long Chapter",
                level=1,
                blocks=[
                    Paragraph(uid="blk-A1", text="First block.", role="body", provenance=prov),
                    Paragraph(uid="blk-A2", text="Second block.", role="body", provenance=prov),
                    Paragraph(uid="blk-A3", text="Third block.", role="body", provenance=prov),
                ],
            ),
        ],
    )


class TestSplitChapterCompiler:
    def test_valid_split_at_second_block(self):
        """Splitting at block 2 keeps original chapter with 1 block, new chapter gets 2."""
        book = _make_split_chapter_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_chapter",
            agent_id="fixer-1",
            rationale="Split chapter into two",
            params={
                "chapter_uid": "ch-A",
                "split_at_block_uid": "blk-A2",
                "new_chapter_title": "Second Half",
                "new_chapter_uid": "ch-B",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid=None
        )
        # Must be book-wide scope
        assert patch.scope.chapter_uid is None
        # 1 insert_node + 2 move_nodes (blk-A2 and blk-A3)
        assert len(patch.changes) == 3
        assert patch.changes[0].op == "insert_node"
        assert patch.changes[1].op == "move_node"
        assert patch.changes[2].op == "move_node"

    def test_split_at_first_block_fails(self):
        """Splitting at the first block would empty original chapter — must fail."""
        book = _make_split_chapter_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_chapter",
            agent_id="fixer-1",
            rationale="test",
            params={
                "chapter_uid": "ch-A",
                "split_at_block_uid": "blk-A1",
                "new_chapter_title": "New",
                "new_chapter_uid": "ch-B",
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "first block" in str(exc_info.value)

    def test_split_at_block_uid_not_in_chapter_fails(self):
        """split_at_block_uid not belonging to chapter must fail."""
        book = _make_split_chapter_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_chapter",
            agent_id="fixer-1",
            rationale="test",
            params={
                "chapter_uid": "ch-A",
                "split_at_block_uid": "nonexistent-blk",
                "new_chapter_title": "New",
                "new_chapter_uid": "ch-B",
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "not found" in str(exc_info.value)

    def test_new_chapter_uid_collision_fails(self):
        """new_chapter_uid that already exists must fail."""
        book = _make_split_chapter_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_chapter",
            agent_id="fixer-1",
            rationale="test",
            params={
                "chapter_uid": "ch-A",
                "split_at_block_uid": "blk-A2",
                "new_chapter_title": "New",
                "new_chapter_uid": "ch-A",  # collision with existing chapter
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "already exists" in str(exc_info.value)

    def test_compiled_patch_applies_correctly(self):
        """Compiled split_chapter patch applies: original keeps 1 block, new gets 2."""
        book = _make_split_chapter_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_chapter",
            agent_id="fixer-1",
            rationale="Split at second block",
            params={
                "chapter_uid": "ch-A",
                "split_at_block_uid": "blk-A2",
                "new_chapter_title": "Second Half",
                "new_chapter_uid": "ch-B",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid=None
        )
        new_book = apply_book_patch(book, patch)

        # Should now have 2 chapters
        assert len(new_book.chapters) == 2
        ch_a = new_book.chapters[0]
        ch_b = new_book.chapters[1]

        assert ch_a.uid == "ch-A"
        assert ch_b.uid == "ch-B"
        assert ch_b.title == "Second Half"
        assert ch_b.level == 1

        # Original chapter keeps only the first block
        assert len(ch_a.blocks) == 1
        assert ch_a.blocks[0].uid == "blk-A1"

        # New chapter gets the remaining 2 blocks in order
        assert len(ch_b.blocks) == 2
        assert ch_b.blocks[0].uid == "blk-A2"
        assert ch_b.blocks[1].uid == "blk-A3"


# ---------------------------------------------------------------------------
# §16 WP4 Compiler: merge_chapters
# ---------------------------------------------------------------------------


def _make_merge_chapters_book() -> Book:
    """Book with 2 chapters each having 2 blocks, for merge_chapters tests."""
    prov = _prov()
    return Book(
        title="Merge Test",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter One",
                level=1,
                blocks=[
                    Paragraph(uid="p1", text="Intro para.", role="body", provenance=prov),
                    Paragraph(uid="p2", text="Second intro.", role="body", provenance=prov),
                ],
            ),
            Chapter(
                uid="ch-2",
                title="Chapter Two",
                level=1,
                blocks=[
                    Paragraph(uid="p3", text="Body para.", role="body", provenance=prov),
                    Paragraph(uid="p4", text="Body end.", role="body", provenance=prov),
                ],
            ),
        ],
    )


class TestMergeChaptersCompiler:
    def test_valid_merge_two_chapters(self):
        """Valid merge of 2 chapters produces insert + section headings + moves + deletes."""
        book = _make_merge_chapters_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_chapters",
            agent_id="fixer-1",
            rationale="Combine chapters into one",
            params={
                "source_chapter_uids": ["ch-1", "ch-2"],
                "new_title": "Combined Chapter",
                "new_chapter_uid": "ch-merged",
                "sections": [
                    {"text": "Part One", "new_block_uid": "sec-1"},
                    {"text": "Part Two", "new_block_uid": "sec-2"},
                ],
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid=None
        )
        assert patch.scope.chapter_uid is None
        # Changes: 1 insert chapter + 2*(1 insert heading + 2 move blocks) + 2 delete chapters
        # = 1 + 2*3 + 2 = 9
        assert len(patch.changes) == 9
        assert patch.changes[0].op == "insert_node"  # new chapter

    def test_sections_count_mismatch_fails(self):
        """sections count != source_chapter_uids count must fail."""
        book = _make_merge_chapters_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_chapters",
            agent_id="fixer-1",
            rationale="test",
            params={
                "source_chapter_uids": ["ch-1", "ch-2"],
                "new_title": "Combined",
                "new_chapter_uid": "ch-new",
                "sections": [
                    {"text": "Only one section", "new_block_uid": "sec-1"},
                    # Missing second section
                ],
            },
        )
        # This will actually fail at PatchCommand validation since we have 2 source_chapter_uids
        # and 1 section. Let's make it clear: sections=[1] while source_chapter_uids=[2] items.
        # The compiler should raise PatchCommandError.
        # NOTE: PatchCommand model allows mismatched counts; compiler catches it.
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "sections length" in str(exc_info.value)

    def test_new_chapter_uid_collision_fails(self):
        """new_chapter_uid that already exists must fail."""
        book = _make_merge_chapters_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_chapters",
            agent_id="fixer-1",
            rationale="test",
            params={
                "source_chapter_uids": ["ch-1", "ch-2"],
                "new_title": "Combined",
                "new_chapter_uid": "ch-1",  # collision
                "sections": [
                    {"text": "S1", "new_block_uid": "sec-1"},
                    {"text": "S2", "new_block_uid": "sec-2"},
                ],
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "already exists" in str(exc_info.value)

    def test_compiled_patch_applies_correctly(self):
        """Merged book has correct structure: headings inserted, blocks moved, sources deleted."""
        book = _make_merge_chapters_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_chapters",
            agent_id="fixer-1",
            rationale="Merge two chapters",
            params={
                "source_chapter_uids": ["ch-1", "ch-2"],
                "new_title": "Combined",
                "new_chapter_uid": "ch-merged",
                "sections": [
                    {"text": "Part One", "new_block_uid": "sec-1"},
                    {"text": "Part Two", "new_block_uid": "sec-2"},
                ],
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid=None
        )
        new_book = apply_book_patch(book, patch)

        # Only the merged chapter should remain
        assert len(new_book.chapters) == 1
        merged = new_book.chapters[0]
        assert merged.uid == "ch-merged"
        assert merged.title == "Combined"

        # Should have: sec-1 heading, p1, p2, sec-2 heading, p3, p4
        assert len(merged.blocks) == 6
        block_uids = [b.uid for b in merged.blocks]
        assert block_uids == ["sec-1", "p1", "p2", "sec-2", "p3", "p4"]

        # Section headings should be heading kind at level 2
        assert merged.blocks[0].kind == "heading"
        assert merged.blocks[0].level == 2  # type: ignore[attr-defined]
        assert merged.blocks[0].text == "Part One"  # type: ignore[attr-defined]

        assert merged.blocks[3].kind == "heading"
        assert merged.blocks[3].level == 2  # type: ignore[attr-defined]
        assert merged.blocks[3].text == "Part Two"  # type: ignore[attr-defined]

    def test_source_chapters_deleted_after_merge(self):
        """Source chapters are deleted from the book after merge."""
        book = _make_merge_chapters_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_chapters",
            agent_id="fixer-1",
            rationale="Merge",
            params={
                "source_chapter_uids": ["ch-1", "ch-2"],
                "new_title": "All Together",
                "new_chapter_uid": "ch-all",
                "sections": [
                    {"text": "Section A", "new_block_uid": "sa"},
                    {"text": "Section B", "new_block_uid": "sb"},
                ],
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid=None
        )
        new_book = apply_book_patch(book, patch)

        chapter_uids = [ch.uid for ch in new_book.chapters]
        assert "ch-1" not in chapter_uids
        assert "ch-2" not in chapter_uids
        assert "ch-all" in chapter_uids

    def test_merge_with_empty_source_chapter(self):
        """Merging chapters where one source chapter has no blocks still works."""
        prov = _prov()
        book = Book(
            title="Test",
            chapters=[
                Chapter(uid="ch-e1", title="Empty", level=1, blocks=[]),
                Chapter(
                    uid="ch-e2",
                    title="With Blocks",
                    level=1,
                    blocks=[
                        Paragraph(uid="pe1", text="Only block.", role="body", provenance=prov),
                    ],
                ),
            ],
        )
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="merge_chapters",
            agent_id="fixer-1",
            rationale="Merge empty + non-empty",
            params={
                "source_chapter_uids": ["ch-e1", "ch-e2"],
                "new_title": "Merged",
                "new_chapter_uid": "ch-m",
                "sections": [
                    {"text": "Empty Section", "new_block_uid": "se1"},
                    {"text": "Content Section", "new_block_uid": "se2"},
                ],
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid=None
        )
        new_book = apply_book_patch(book, patch)

        assert len(new_book.chapters) == 1
        merged = new_book.chapters[0]
        # se1 heading (empty source), se2 heading, pe1 block
        assert len(merged.blocks) == 3
        assert merged.blocks[0].uid == "se1"
        assert merged.blocks[1].uid == "se2"
        assert merged.blocks[2].uid == "pe1"


# ---------------------------------------------------------------------------
# §17 WP5 Helpers: footnote test book builders
# ---------------------------------------------------------------------------


def _make_footnote_book() -> Book:
    """Book with one chapter containing a paragraph with raw callout and a footnote block."""
    prov_p1 = Provenance(page=3, source="passthrough")
    prov_fn = Provenance(page=3, source="passthrough")
    return Book(
        title="Footnote Test",
        chapters=[
            Chapter(
                uid="ch-fn",
                title="Chapter with Footnotes",
                blocks=[
                    Paragraph(
                        uid="p-src",
                        text="See note 1) for details.",
                        role="body",
                        provenance=prov_p1,
                    ),
                    Footnote(
                        uid="fn-1",
                        callout="1)",
                        text="This is footnote text.",
                        paired=False,
                        orphan=False,
                        provenance=prov_fn,
                    ),
                ],
            ),
        ],
    )


def _make_footnote_book_paired() -> Book:
    """Book where footnote is already paired (marker in source text)."""
    prov_p1 = Provenance(page=3, source="passthrough")
    prov_fn = Provenance(page=3, source="passthrough")
    marker = make_fn_marker(3, "1)")
    return Book(
        title="Footnote Test",
        chapters=[
            Chapter(
                uid="ch-fn",
                title="Chapter with Footnotes",
                blocks=[
                    Paragraph(
                        uid="p-src",
                        text=f"See {marker} for details.",
                        role="body",
                        provenance=prov_p1,
                    ),
                    Footnote(
                        uid="fn-1",
                        callout="1)",
                        text="This is footnote text.",
                        paired=True,
                        orphan=False,
                        provenance=prov_fn,
                    ),
                ],
            ),
        ],
    )


def _make_footnote_book_orphan() -> Book:
    """Book where footnote is orphan=True (no marker in source text, raw callout missing too)."""
    prov_fn = Provenance(page=3, source="passthrough")
    return Book(
        title="Footnote Test",
        chapters=[
            Chapter(
                uid="ch-fn",
                title="Chapter with Footnotes",
                blocks=[
                    Paragraph(
                        uid="p-src",
                        text="See note 1) for details.",
                        role="body",
                        provenance=Provenance(page=3, source="passthrough"),
                    ),
                    Footnote(
                        uid="fn-1",
                        callout="1)",
                        text="This is footnote text.",
                        paired=False,
                        orphan=True,
                        provenance=prov_fn,
                    ),
                ],
            ),
        ],
    )


def _make_cross_chapter_footnote_book() -> Book:
    """Book with footnote in ch-2, source paragraph in ch-1 (cross-chapter scenario)."""
    prov1 = Provenance(page=1, source="passthrough")
    prov2 = Provenance(page=2, source="passthrough")
    return Book(
        title="Cross Chapter Test",
        chapters=[
            Chapter(
                uid="ch-1",
                title="Chapter One",
                blocks=[
                    Paragraph(
                        uid="p-cross",
                        text="Text with 2) callout here.",
                        role="body",
                        provenance=prov1,
                    ),
                ],
            ),
            Chapter(
                uid="ch-2",
                title="Chapter Two",
                blocks=[
                    Footnote(
                        uid="fn-cross",
                        callout="2)",
                        text="Cross-chapter footnote.",
                        paired=False,
                        orphan=False,
                        provenance=prov2,
                    ),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# §18 WP5 Compiler: pair_footnote
# ---------------------------------------------------------------------------


class TestPairFootnoteCompiler:
    def test_valid_pair(self):
        """Valid pair_footnote: source block has raw callout, footnote gets paired."""
        book = _make_footnote_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="Pair footnote with callout in source",
            params={
                "fn_block_uid": "fn-1",
                "source_block_uid": "p-src",
                "occurrence_index": 0,
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        # Changes: set_field text, set_field paired (2 changes; no orphan change since orphan=False)
        assert len(patch.changes) == 2
        assert patch.scope.chapter_uid == "ch-fn"
        text_change = patch.changes[0]
        paired_change = patch.changes[1]
        assert text_change.op == "set_field"
        assert text_change.target_uid == "p-src"
        assert text_change.field == "text"
        assert paired_change.op == "set_field"
        assert paired_change.target_uid == "fn-1"
        assert paired_change.field == "paired"
        assert paired_change.new is True

    def test_fn_block_not_footnote_raises(self):
        """fn_block_uid pointing to non-footnote raises PatchCommandError."""
        book = _make_footnote_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="test",
            params={
                "fn_block_uid": "p-src",  # paragraph, not footnote
                "source_block_uid": "p-src",
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "not a footnote" in str(exc_info.value)

    def test_source_block_no_raw_callout_raises(self):
        """Source block without the raw callout raises PatchCommandError."""
        prov = Provenance(page=3, source="passthrough")
        book = Book(
            title="Test",
            chapters=[
                Chapter(
                    uid="ch-fn",
                    title="Ch",
                    blocks=[
                        Paragraph(
                            uid="p-no-callout",
                            text="No footnote reference here.",
                            role="body",
                            provenance=prov,
                        ),
                        Footnote(
                            uid="fn-1",
                            callout="1)",
                            text="Footnote text.",
                            paired=False,
                            orphan=False,
                            provenance=prov,
                        ),
                    ],
                )
            ],
        )
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="test",
            params={
                "fn_block_uid": "fn-1",
                "source_block_uid": "p-no-callout",
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "no raw callout" in str(exc_info.value)

    def test_occurrence_index_out_of_range_raises(self):
        """occurrence_index >= callout count raises PatchCommandError."""
        book = _make_footnote_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="test",
            params={
                "fn_block_uid": "fn-1",
                "source_block_uid": "p-src",
                "occurrence_index": 5,  # callout appears only once
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "out of range" in str(exc_info.value)

    def test_compiled_patch_applies_correctly(self):
        """Compiled pair_footnote patch applies: marker in text, paired=True."""
        book = _make_footnote_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="Pair footnote",
            params={
                "fn_block_uid": "fn-1",
                "source_block_uid": "p-src",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        new_book = apply_book_patch(book, patch)

        ch = new_book.chapters[0]
        src_block = ch.blocks[0]
        fn_block = ch.blocks[1]
        assert isinstance(fn_block, Footnote)
        assert fn_block.paired is True
        assert fn_block.orphan is False
        # Raw callout should be replaced with marker
        expected_marker = make_fn_marker(3, "1)")
        assert expected_marker in src_block.text  # type: ignore[union-attr]
        # The raw callout should not appear outside markers
        from epubforge.markers import strip_markers
        assert "1)" not in strip_markers(src_block.text)  # type: ignore[union-attr]

    def test_pair_orphan_footnote_clears_orphan_first(self):
        """pair_footnote on orphan=True footnote generates orphan=False change first."""
        book = _make_footnote_book_orphan()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="Pair an orphan footnote",
            params={
                "fn_block_uid": "fn-1",
                "source_block_uid": "p-src",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        # Changes: orphan=False, text update, paired=True (3 changes)
        assert len(patch.changes) == 3
        orphan_change = patch.changes[0]
        paired_change = patch.changes[2]
        assert orphan_change.op == "set_field"
        assert orphan_change.field == "orphan"
        assert orphan_change.old is True
        assert orphan_change.new is False
        assert paired_change.field == "paired"  # type: ignore[union-attr]
        assert paired_change.new is True  # type: ignore[union-attr]

    def test_pair_orphan_applies_correctly(self):
        """Compiled pair_footnote on orphan footnote results in paired=True, orphan=False."""
        book = _make_footnote_book_orphan()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="Pair an orphan footnote",
            params={
                "fn_block_uid": "fn-1",
                "source_block_uid": "p-src",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        new_book = apply_book_patch(book, patch)
        fn_block = new_book.chapters[0].blocks[1]
        assert isinstance(fn_block, Footnote)
        assert fn_block.paired is True
        assert fn_block.orphan is False

    def test_cross_chapter_pair_is_book_wide(self):
        """pair_footnote where fn and source are in different chapters → scope book-wide."""
        book = _make_cross_chapter_footnote_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="pair_footnote",
            agent_id="fixer-1",
            rationale="Cross-chapter pair",
            params={
                "fn_block_uid": "fn-cross",
                "source_block_uid": "p-cross",
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid=None
        )
        assert patch.scope.chapter_uid is None


# ---------------------------------------------------------------------------
# §19 WP5 Compiler: unpair_footnote
# ---------------------------------------------------------------------------


class TestUnpairFootnoteCompiler:
    def test_valid_unpair(self):
        """Valid unpair_footnote: marker in source replaced with raw callout, paired=False."""
        book = _make_footnote_book_paired()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="unpair_footnote",
            agent_id="fixer-1",
            rationale="Remove pairing",
            params={
                "fn_block_uid": "fn-1",
                "occurrence_index": 0,
            },
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        # 2 changes: text field update, paired=False
        assert len(patch.changes) == 2
        assert patch.scope.chapter_uid == "ch-fn"
        text_change = patch.changes[0]
        paired_change = patch.changes[1]
        assert text_change.op == "set_field"
        assert text_change.target_uid == "p-src"
        assert text_change.field == "text"
        assert paired_change.op == "set_field"
        assert paired_change.field == "paired"
        assert paired_change.old is True
        assert paired_change.new is False

    def test_footnote_not_paired_raises(self):
        """unpair_footnote on unpaired footnote raises PatchCommandError."""
        book = _make_footnote_book()  # paired=False by default
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="unpair_footnote",
            agent_id="fixer-1",
            rationale="test",
            params={"fn_block_uid": "fn-1"},
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "not currently paired" in str(exc_info.value)

    def test_no_marker_found_raises(self):
        """unpair_footnote when footnote.paired=True but no marker in book raises."""
        prov = Provenance(page=5, source="passthrough")
        book = Book(
            title="Test",
            chapters=[
                Chapter(
                    uid="ch-x",
                    title="Ch",
                    blocks=[
                        Paragraph(
                            uid="p-x",
                            text="No marker here.",
                            role="body",
                            provenance=prov,
                        ),
                        Footnote(
                            uid="fn-x",
                            callout="*",
                            text="Orphan-like.",
                            paired=True,  # inconsistent state — no marker in book
                            orphan=False,
                            provenance=prov,
                        ),
                    ],
                )
            ],
        )
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="unpair_footnote",
            agent_id="fixer-1",
            rationale="test",
            params={"fn_block_uid": "fn-x"},
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "no marker found" in str(exc_info.value)

    def test_fn_block_not_footnote_raises(self):
        """unpair_footnote with non-footnote uid raises PatchCommandError."""
        book = _make_footnote_book_paired()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="unpair_footnote",
            agent_id="fixer-1",
            rationale="test",
            params={"fn_block_uid": "p-src"},  # paragraph, not footnote
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "not a footnote" in str(exc_info.value)

    def test_compiled_patch_applies_correctly(self):
        """Compiled unpair_footnote patch: marker removed from source, paired=False."""
        book = _make_footnote_book_paired()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="unpair_footnote",
            agent_id="fixer-1",
            rationale="Unpair footnote",
            params={"fn_block_uid": "fn-1"},
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        new_book = apply_book_patch(book, patch)

        ch = new_book.chapters[0]
        src_block = ch.blocks[0]
        fn_block = ch.blocks[1]
        assert isinstance(fn_block, Footnote)
        assert fn_block.paired is False
        # Marker should be gone, replaced with raw callout
        marker = make_fn_marker(3, "1)")
        assert marker not in src_block.text  # type: ignore[union-attr]
        assert "1)" in src_block.text  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# §20 WP5 Compiler: mark_orphan
# ---------------------------------------------------------------------------


class TestMarkOrphanCompiler:
    def test_valid_mark_orphan_no_marker(self):
        """mark_orphan with no marker in book: orphan=True set, chapter-scoped."""
        book = _make_footnote_book()
        # Source block has raw callout "1)" but no marker — so find_markers returns []
        # First, remove the raw callout from source so no marker exists at all
        prov = Provenance(page=3, source="passthrough")
        book_no_callout = Book(
            title="Test",
            chapters=[
                Chapter(
                    uid="ch-fn",
                    title="Ch",
                    blocks=[
                        Paragraph(
                            uid="p-src",
                            text="No reference here.",
                            role="body",
                            provenance=prov,
                        ),
                        Footnote(
                            uid="fn-1",
                            callout="1)",
                            text="Footnote text.",
                            paired=False,
                            orphan=False,
                            provenance=prov,
                        ),
                    ],
                )
            ],
        )
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="mark_orphan",
            agent_id="fixer-1",
            rationale="Mark as orphan",
            params={"fn_block_uid": "fn-1"},
        )
        patch = compile_patch_command(
            book_no_callout, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        # 1 change: orphan=True (no paired=False needed since paired=False already)
        assert len(patch.changes) == 1
        assert patch.scope.chapter_uid == "ch-fn"
        orphan_change = patch.changes[0]
        assert orphan_change.op == "set_field"
        assert orphan_change.field == "orphan"
        assert orphan_change.new is True

    def test_valid_mark_orphan_with_marker(self):
        """mark_orphan with existing marker: marker removed, paired=False, orphan=True."""
        book = _make_footnote_book_paired()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="mark_orphan",
            agent_id="fixer-1",
            rationale="Mark as orphan despite marker",
            params={"fn_block_uid": "fn-1"},
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        # 3 changes: restore source text, paired=False, orphan=True
        assert len(patch.changes) == 3
        text_change = patch.changes[0]
        paired_change = patch.changes[1]
        orphan_change = patch.changes[2]
        assert text_change.op == "set_field"
        assert text_change.target_uid == "p-src"
        assert paired_change.field == "paired"  # type: ignore[union-attr]
        assert paired_change.old is True  # type: ignore[union-attr]
        assert paired_change.new is False  # type: ignore[union-attr]
        assert orphan_change.field == "orphan"  # type: ignore[union-attr]
        assert orphan_change.new is True  # type: ignore[union-attr]

    def test_already_orphan_raises(self):
        """mark_orphan on already-orphan footnote raises PatchCommandError."""
        book = _make_footnote_book_orphan()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="mark_orphan",
            agent_id="fixer-1",
            rationale="test",
            params={"fn_block_uid": "fn-1"},
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "already marked as orphan" in str(exc_info.value)

    def test_fn_block_not_footnote_raises(self):
        """mark_orphan with non-footnote uid raises PatchCommandError."""
        book = _make_footnote_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="mark_orphan",
            agent_id="fixer-1",
            rationale="test",
            params={"fn_block_uid": "p-src"},  # paragraph
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "not a footnote" in str(exc_info.value)

    def test_compiled_mark_orphan_no_marker_applies(self):
        """Compiled mark_orphan (no marker) patch applies: orphan=True."""
        prov = Provenance(page=3, source="passthrough")
        book = Book(
            title="Test",
            chapters=[
                Chapter(
                    uid="ch-fn",
                    title="Ch",
                    blocks=[
                        Paragraph(
                            uid="p-src",
                            text="No reference.",
                            role="body",
                            provenance=prov,
                        ),
                        Footnote(
                            uid="fn-1",
                            callout="1)",
                            text="Footnote.",
                            paired=False,
                            orphan=False,
                            provenance=prov,
                        ),
                    ],
                )
            ],
        )
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="mark_orphan",
            agent_id="fixer-1",
            rationale="Mark orphan",
            params={"fn_block_uid": "fn-1"},
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        new_book = apply_book_patch(book, patch)
        fn_block = new_book.chapters[0].blocks[1]
        assert isinstance(fn_block, Footnote)
        assert fn_block.orphan is True
        assert fn_block.paired is False

    def test_compiled_mark_orphan_with_marker_applies(self):
        """Compiled mark_orphan (with marker) applies: marker removed, orphan=True, paired=False."""
        book = _make_footnote_book_paired()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="mark_orphan",
            agent_id="fixer-1",
            rationale="Mark orphan",
            params={"fn_block_uid": "fn-1"},
        )
        patch = compile_patch_command(
            book, cmd, output_kind="fixer", output_chapter_uid="ch-fn"
        )
        new_book = apply_book_patch(book, patch)

        ch = new_book.chapters[0]
        src_block = ch.blocks[0]
        fn_block = ch.blocks[1]
        assert isinstance(fn_block, Footnote)
        assert fn_block.orphan is True
        assert fn_block.paired is False
        marker = make_fn_marker(3, "1)")
        assert marker not in src_block.text  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# §20 WP6 Compiler: split_merged_table
# ---------------------------------------------------------------------------


def _make_table_book(
    *,
    multi_page: bool = True,
    table_uid: str = "tbl-1",
    table_title: str = "Table 1",
    caption: str = "This is the caption.",
    extra_blocks_before: bool = False,
    extra_blocks_after: bool = False,
) -> Book:
    """Create a book with a chapter containing a multi-page merged Table block."""
    from epubforge.ir.semantic import Table, TableMergeRecord

    prov = Provenance(
        page=5,
        bbox=[10.0, 20.0, 300.0, 400.0],
        source="llm",
        raw_ref="raw-table-1",
        raw_label="Table 1.1",
        artifact_id="artifact-abc123",
        evidence_ref="evidence/page_005.json",
    )
    table = Table(
        uid=table_uid,
        html="<table><tr><td>merged</td></tr></table>",
        table_title=table_title,
        caption=caption,
        continuation=False,
        multi_page=multi_page,
        bbox=[0.0, 0.0, 100.0, 200.0],
        provenance=prov,
        merge_record=TableMergeRecord(
            segment_html=["<tr><td>A</td></tr>", "<tr><td>B</td></tr>"],
            segment_pages=[5, 6],
            segment_order=[0, 1],
            column_widths=[1, 1],
        ),
    )

    blocks: list = []
    if extra_blocks_before:
        blocks.append(
            Paragraph(
                uid="pre-blk",
                text="Before table",
                role="body",
                provenance=Provenance(page=4, source="passthrough"),
            )
        )
    blocks.append(table)
    if extra_blocks_after:
        blocks.append(
            Paragraph(
                uid="post-blk",
                text="After table",
                role="body",
                provenance=Provenance(page=7, source="passthrough"),
            )
        )

    return Book(
        title="Table Test Book",
        chapters=[Chapter(uid="ch-tbl", title="Chapter with Table", blocks=blocks)],
    )


class TestSplitMergedTableCompiler:
    """Tests for _compile_split_merged_table (WP6)."""

    # ---- Error cases ----

    def test_block_not_a_table_raises(self):
        """block_uid pointing to non-Table block raises PatchCommandError."""
        book = _make_book()  # has Paragraph blocks
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "blk-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [1, 2],
                "new_block_uids": ["n1", "n2"],
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "not a table" in str(exc_info.value)

    def test_table_not_multi_page_raises(self):
        """Table with multi_page=False raises PatchCommandError."""
        book = _make_table_book(multi_page=False)
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["n1", "n2"],
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "multi_page" in str(exc_info.value) or "multi-page" in str(exc_info.value)

    def test_new_block_uids_collision_raises(self):
        """new_block_uid that already exists in book raises PatchCommandError."""
        book = _make_table_book()
        book.chapters[0].blocks.append(
            Paragraph(
                uid="existing-uid",
                text="x",
                role="body",
                provenance=Provenance(page=1, source="passthrough"),
            )
        )
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["existing-uid", "n2"],
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "already exists" in str(exc_info.value)

    def test_new_block_uids_duplicates_raises(self):
        """Duplicate UIDs within new_block_uids raises PatchCommandError."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["same-uid", "same-uid"],
            },
        )
        with pytest.raises(PatchCommandError) as exc_info:
            compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert "duplicate" in str(exc_info.value)

    # ---- Valid 2-segment split ----

    def test_valid_split_2_segments_change_count(self):
        """Valid 2-segment split produces 1 delete + 2 insert = 3 changes."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<table>A</table>", "<table>B</table>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert len(patch.changes) == 3
        assert patch.changes[0].op == "delete_node"
        assert patch.changes[1].op == "insert_node"
        assert patch.changes[2].op == "insert_node"

    def test_valid_split_3_segments_change_count(self):
        """Valid 3-segment split produces 1 delete + 3 insert = 4 changes."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>", "<t>C</t>"],
                "segment_pages": [5, 6, 7],
                "new_block_uids": ["seg-1", "seg-2", "seg-3"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert len(patch.changes) == 4

    def test_scope_is_chapter_scoped(self):
        """Compiled patch scope is chapter_uid of the containing chapter."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        assert patch.scope.chapter_uid == "ch-tbl"

    # ---- Table is first block: after_uid=None for first segment ----

    def test_table_is_first_block_after_uid_none(self):
        """When table is the first block, first insert has after_uid=None."""
        book = _make_table_book()  # table is the only block (index 0)
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        first_insert = patch.changes[1]
        assert first_insert.op == "insert_node"
        assert first_insert.after_uid is None  # type: ignore[union-attr]

    # ---- Table is middle block: after_uid = previous block's uid ----

    def test_table_is_middle_block_correct_previous_uid(self):
        """When table has a preceding block, first insert uses that block's uid as after_uid."""
        book = _make_table_book(extra_blocks_before=True)  # pre-blk before tbl-1
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        first_insert = patch.changes[1]
        assert first_insert.after_uid == "pre-blk"  # type: ignore[union-attr]

    # ---- Full apply correctness ----

    def test_applied_patch_segment_order(self):
        """After applying, book has segments in correct order (original table removed)."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<table>seg-A</table>", "<table>seg-B</table>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        new_book = apply_book_patch(book, patch)
        ch = new_book.chapters[0]
        assert len(ch.blocks) == 2
        assert ch.blocks[0].uid == "seg-1"
        assert ch.blocks[1].uid == "seg-2"

    def test_applied_patch_caption_only_on_last_segment(self):
        """After applying, caption is on last segment only; others have empty caption."""
        book = _make_table_book(caption="My Caption")
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>", "<t>C</t>"],
                "segment_pages": [5, 6, 7],
                "new_block_uids": ["seg-1", "seg-2", "seg-3"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        new_book = apply_book_patch(book, patch)
        ch = new_book.chapters[0]
        assert ch.blocks[0].caption == ""          # type: ignore[union-attr]
        assert ch.blocks[1].caption == ""          # type: ignore[union-attr]
        assert ch.blocks[2].caption == "My Caption"  # type: ignore[union-attr]

    def test_applied_patch_continuation_flags(self):
        """After applying, continuation=False for first segment, True for rest."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>", "<t>C</t>"],
                "segment_pages": [5, 6, 7],
                "new_block_uids": ["seg-1", "seg-2", "seg-3"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        new_book = apply_book_patch(book, patch)
        ch = new_book.chapters[0]
        assert ch.blocks[0].continuation is False  # type: ignore[union-attr]
        assert ch.blocks[1].continuation is True   # type: ignore[union-attr]
        assert ch.blocks[2].continuation is True   # type: ignore[union-attr]

    def test_applied_patch_provenance_full_preserved(self):
        """After applying, each segment preserves all provenance fields with only page overridden."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        new_book = apply_book_patch(book, patch)
        ch = new_book.chapters[0]

        # First segment: page=5, all other fields from original provenance
        assert ch.blocks[0].provenance == Provenance(  # type: ignore[union-attr]
            page=5,
            bbox=[10.0, 20.0, 300.0, 400.0],
            source="llm",
            raw_ref="raw-table-1",
            raw_label="Table 1.1",
            artifact_id="artifact-abc123",
            evidence_ref="evidence/page_005.json",
        )

        # Second segment: page=6, all other fields from original provenance
        assert ch.blocks[1].provenance == Provenance(  # type: ignore[union-attr]
            page=6,
            bbox=[10.0, 20.0, 300.0, 400.0],
            source="llm",
            raw_ref="raw-table-1",
            raw_label="Table 1.1",
            artifact_id="artifact-abc123",
            evidence_ref="evidence/page_005.json",
        )

    def test_applied_patch_table_title_inherited(self):
        """After applying, all segments inherit table_title from original."""
        book = _make_table_book(table_title="Revenue Table")
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        new_book = apply_book_patch(book, patch)
        ch = new_book.chapters[0]
        assert ch.blocks[0].table_title == "Revenue Table"  # type: ignore[union-attr]
        assert ch.blocks[1].table_title == "Revenue Table"  # type: ignore[union-attr]

    def test_applied_patch_multi_page_false_on_segments(self):
        """After applying, all segments have multi_page=False."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        new_book = apply_book_patch(book, patch)
        ch = new_book.chapters[0]
        assert ch.blocks[0].multi_page is False  # type: ignore[union-attr]
        assert ch.blocks[1].multi_page is False  # type: ignore[union-attr]

    def test_applied_patch_merge_record_none(self):
        """After applying, all segments have merge_record=None."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        new_book = apply_book_patch(book, patch)
        ch = new_book.chapters[0]
        assert ch.blocks[0].merge_record is None  # type: ignore[union-attr]
        assert ch.blocks[1].merge_record is None  # type: ignore[union-attr]

    def test_applied_patch_html_per_segment(self):
        """After applying, each segment has correct html from segment_html."""
        book = _make_table_book()
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<table>FIRST</table>", "<table>SECOND</table>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        new_book = apply_book_patch(book, patch)
        ch = new_book.chapters[0]
        assert ch.blocks[0].html == "<table>FIRST</table>"   # type: ignore[union-attr]
        assert ch.blocks[1].html == "<table>SECOND</table>"  # type: ignore[union-attr]

    def test_applied_patch_surrounding_blocks_preserved(self):
        """Blocks surrounding the table are preserved in correct positions."""
        book = _make_table_book(extra_blocks_before=True, extra_blocks_after=True)
        cmd = PatchCommand(
            command_id=str(uuid4()),
            op="split_merged_table",
            agent_id="fixer-1",
            rationale="split",
            params={
                "block_uid": "tbl-1",
                "segment_html": ["<t>A</t>", "<t>B</t>"],
                "segment_pages": [5, 6],
                "new_block_uids": ["seg-1", "seg-2"],
            },
        )
        patch = compile_patch_command(book, cmd, output_kind="fixer", output_chapter_uid=None)
        new_book = apply_book_patch(book, patch)
        ch = new_book.chapters[0]
        assert len(ch.blocks) == 4  # pre-blk, seg-1, seg-2, post-blk
        assert ch.blocks[0].uid == "pre-blk"
        assert ch.blocks[1].uid == "seg-1"
        assert ch.blocks[2].uid == "seg-2"
        assert ch.blocks[3].uid == "post-blk"

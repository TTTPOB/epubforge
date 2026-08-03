"""Tests for logging setup and stage timing."""

from __future__ import annotations

import logging

import pytest

import epubforge.observability as obs


def _reset():
    """Reset module-level state for test isolation."""
    obs._CONFIGURED = False
    # Remove all handlers from root logger
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()


# ---------------------------------------------------------------------------
# setup_logging idempotency
# ---------------------------------------------------------------------------


class TestSetupLogging:
    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_first_call_configures_root(self):
        obs.setup_logging("DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) >= 1

    def test_idempotent_second_call(self):
        obs.setup_logging("INFO")
        handler_count = len(logging.getLogger().handlers)
        obs.setup_logging("DEBUG")  # should be a no-op
        assert len(logging.getLogger().handlers) == handler_count

    def test_with_log_file(self, tmp_path):
        log_file = tmp_path / "logs" / "run-test.log"
        obs.setup_logging("INFO", log_file)
        assert log_file.exists()

    def test_returns_log_file_path(self, tmp_path):
        log_file = tmp_path / "run.log"
        result = obs.setup_logging("INFO", log_file)
        assert result == log_file


# ---------------------------------------------------------------------------
# stage_timer
# ---------------------------------------------------------------------------


class TestStageTimer:
    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_normal_yield(self, caplog):
        obs.setup_logging("INFO")
        logger = logging.getLogger("test.stage")
        with caplog.at_level(logging.INFO, logger="test.stage"):
            with obs.stage_timer(logger, "test"):
                pass
        messages = [r.message for r in caplog.records]
        assert any("▶ Stage test started" in m for m in messages)
        assert any("✔ Stage test done" in m for m in messages)

    def test_exception_propagates(self):
        _reset()
        logger = logging.getLogger("test.stage_exc")
        with pytest.raises(ValueError, match="boom"):
            with obs.stage_timer(logger, "failing"):
                raise ValueError("boom")

    def test_completion_log_contains_elapsed_time(self, caplog):
        obs.setup_logging("INFO")
        logger = logging.getLogger("test.elapsed")
        with caplog.at_level(logging.INFO, logger="test.elapsed"):
            with obs.stage_timer(logger, "elapsed"):
                pass
        done_msgs = [r.message for r in caplog.records if "✔" in r.message]
        assert done_msgs
        assert "done in" in done_msgs[0]
        assert "requests=" not in done_msgs[0]

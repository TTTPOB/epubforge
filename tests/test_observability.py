"""Tests for observability module: UsageTracker, setup_logging, stage_timer."""

from __future__ import annotations

import logging

import pytest

import epubforge.observability as obs


def _reset():
    """Reset module-level state for test isolation."""
    obs._CONFIGURED = False
    obs._tracker = obs.UsageTracker()
    # Remove all handlers from root logger
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()


# ---------------------------------------------------------------------------
# UsageTracker
# ---------------------------------------------------------------------------


class TestUsageTracker:
    def test_record_hit(self):
        t = obs.UsageTracker()
        t.record_hit()
        assert t.requests == 1
        assert t.cache_hits == 1
        assert t.cache_misses == 0
        assert t.total_tokens == 0

    def test_record_miss(self):
        t = obs.UsageTracker()
        t.record_miss(prompt=100, completion=50, elapsed=1.5)
        assert t.requests == 1
        assert t.cache_misses == 1
        assert t.prompt_tokens == 100
        assert t.completion_tokens == 50
        assert t.total_tokens == 150
        assert abs(t.elapsed_s - 1.5) < 1e-9

    def test_delta(self):
        t = obs.UsageTracker()
        before = t.snapshot()
        t.record_miss(prompt=200, completion=80, elapsed=2.0)
        t.record_hit()
        d = t.delta(before)
        assert d.requests == 2
        assert d.cache_hits == 1
        assert d.cache_misses == 1
        assert d.prompt_tokens == 200
        assert d.total_tokens == 280

    def test_summary_line_hit_rate(self):
        t = obs.UsageTracker()
        t.record_hit()
        t.record_hit()
        t.record_miss(prompt=10, completion=5, elapsed=0.1)
        line = t.summary_line()
        assert "67%" in line
        assert "requests=3" in line

    def test_summary_line_no_requests(self):
        t = obs.UsageTracker()
        line = t.summary_line()
        assert "requests=0" in line
        assert "0%" in line


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

    def test_usage_delta_in_summary(self, caplog):
        obs.setup_logging("INFO")
        logger = logging.getLogger("test.usage")
        tracker = obs.get_tracker()
        with caplog.at_level(logging.INFO, logger="test.usage"):
            with obs.stage_timer(logger, "with_usage"):
                tracker.record_miss(prompt=50, completion=25, elapsed=0.5)
        done_msgs = [r.message for r in caplog.records if "✔" in r.message]
        assert done_msgs
        assert "requests=1" in done_msgs[0]

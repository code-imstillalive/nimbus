"""Tests for health.py's always-on WARNING+/ERROR log capture
(2026-08-25, direct ask: "at all times log any errors from nimbus...
i wanna know what fails and what flatlines and what is not running").

Pure-python, stdlib `logging` only -- no HA stubs needed, since health.py
itself has zero homeassistant.* imports (see its own module docstring:
a plain logging.Handler is the whole mechanism).
"""

import logging
import unittest

import _solver_path  # noqa: F401
import health


class TestLogBufferHandler(unittest.TestCase):
    def setUp(self):
        health.reset_log_buffer_for_tests()
        # Real installs only ever call install_log_buffer_handler() once
        # (via __init__.py's async_setup_entry, guarded), but a test
        # process may run many test modules in the same interpreter --
        # force a clean re-install so each test gets a fresh handler
        # attached to a logger it can freely emit on, regardless of
        # whichever earlier test file ran first.
        health._handler_installed = False
        logging.getLogger(health._LOGGER_NAMESPACE).handlers.clear()
        health.install_log_buffer_handler()
        self.logger = logging.getLogger(f"{health._LOGGER_NAMESPACE}.test")

    def tearDown(self):
        logging.getLogger(health._LOGGER_NAMESPACE).handlers.clear()
        health._handler_installed = False
        health.reset_log_buffer_for_tests()

    def test_warning_level_is_captured(self):
        self.logger.warning("a real warning: %s", "something")
        entries = health.get_recent_log_entries(min_level=logging.WARNING)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["level"], "WARNING")
        self.assertIn("a real warning: something", entries[0]["message"])
        self.assertEqual(entries[0]["logger"], self.logger.name)
        self.assertIn("timestamp", entries[0])

    def test_error_level_is_captured(self):
        self.logger.error("a real error")
        entries = health.get_recent_log_entries(min_level=logging.ERROR)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["level"], "ERROR")

    def test_info_and_debug_are_not_captured(self):
        self.logger.info("routine info, not actionable")
        self.logger.debug("routine debug, not actionable")
        self.assertEqual(health.get_recent_log_entries(min_level=logging.WARNING), [])

    def test_get_recent_log_entries_min_level_filters_out_warnings(self):
        self.logger.warning("just a warning")
        self.logger.error("a real error")
        errors_only = health.get_recent_log_entries(min_level=logging.ERROR)
        self.assertEqual(len(errors_only), 1)
        self.assertEqual(errors_only[0]["level"], "ERROR")

    def test_count_recent_log_entries_matches_get_recent_log_entries(self):
        for _ in range(3):
            self.logger.error("repeated error")
        self.logger.warning("one warning")
        self.assertEqual(health.count_recent_log_entries(min_level=logging.ERROR), 3)
        self.assertEqual(health.count_recent_log_entries(min_level=logging.WARNING), 4)

    def test_newest_first_ordering(self):
        self.logger.warning("first")
        self.logger.warning("second")
        self.logger.warning("third")
        entries = health.get_recent_log_entries(min_level=logging.WARNING)
        messages = [e["message"] for e in entries]
        self.assertEqual(messages, ["third", "second", "first"])

    def test_limit_caps_the_returned_count_not_the_underlying_buffer(self):
        for i in range(5):
            self.logger.warning("entry %d", i)
        limited = health.get_recent_log_entries(min_level=logging.WARNING, limit=2)
        self.assertEqual(len(limited), 2)
        self.assertEqual(health.count_recent_log_entries(min_level=logging.WARNING), 5)

    def test_install_is_idempotent_no_double_capture(self):
        # A second install call (e.g. a config-entry reload) must not
        # attach a second handler -- otherwise every future log line
        # would be captured twice.
        health.install_log_buffer_handler()
        health.install_log_buffer_handler()
        self.logger.warning("only once")
        self.assertEqual(health.count_recent_log_entries(min_level=logging.WARNING), 1)

    def test_a_broken_log_call_does_not_break_logging_itself(self):
        # A malformed %-format call is a real, if rare, failure mode --
        # emit() must degrade to SOMETHING captured, never raise back
        # into the caller's own log statement.
        try:
            self.logger.warning("missing arg: %s")
        except Exception as e:
            self.fail(f"a malformed log call must never raise: {e}")
        self.assertEqual(
            len(health.get_recent_log_entries(min_level=logging.WARNING)), 1
        )


if __name__ == "__main__":
    unittest.main()

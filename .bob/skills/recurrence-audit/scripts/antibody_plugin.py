"""
antibody_plugin.py — pytest plugin loaded with -p antibody_plugin.

Records collected node IDs, failed node IDs, and collection errors into
a JSON report file. The report path is given via the ANTIBODY_REPORT_PATH
environment variable.

Fixes vs. original:
- Errors in setup or teardown phases are counted as failures (not just call).
- xfailed, xpassed, and skipped tests are recorded in separate counters so
  that setup.json's baseline reflects what pytest actually reported.
"""
from __future__ import annotations

import json
import os


def pytest_configure(config):
    report_path = os.environ.get("ANTIBODY_REPORT_PATH")
    if report_path:
        plugin = AntibodyPlugin(report_path)
        config.pluginmanager.register(plugin, "antibody_plugin_instance")


class AntibodyPlugin:
    def __init__(self, report_path: str) -> None:
        self.report_path = report_path
        self.collected: list[str] = []
        self.failed: list[str] = []
        self.errors: list[str] = []  # collection errors
        self.skipped: int = 0
        self.xfailed: int = 0
        self.xpassed: int = 0

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        """Called after each collector finishes. Capture collection errors."""
        if report.failed:
            self.errors.append(str(report.nodeid))

    def pytest_runtest_logreport(self, report):
        # Fix 1: count errors in setup and teardown as failures too.
        if report.failed and report.when in ("setup", "call", "teardown"):
            # Avoid double-counting: a test that errors in setup will not
            # reach "call", so appending on any failed phase is correct.
            # But if somehow a node appears in both setup-error and teardown-error
            # (unusual), deduplicate by nodeid.
            if report.nodeid not in self.failed:
                self.failed.append(report.nodeid)

        # Fix 2: record skipped / xfailed / xpassed counts.
        if report.when == "call":
            wasxfail = getattr(report, "wasxfail", None)
            if wasxfail is not None:
                if report.passed:
                    # xpassed: marked xfail but unexpectedly passed
                    self.xpassed += 1
                else:
                    # xfailed: marked xfail and failed as expected
                    self.xfailed += 1
        if report.skipped:
            # skipped can happen in setup or call (e.g. pytest.skip in fixture)
            # only count once per phase; "call" skips are pytest.skip() in the body,
            # "setup" skips are pytest.skip() in a fixture.
            self.skipped += 1

    def pytest_sessionfinish(self, session, exitstatus):
        data = {
            "collected": self.collected,
            "failed": self.failed,
            "collection_errors": self.errors,
            "skipped": self.skipped,
            "xfailed": self.xfailed,
            "xpassed": self.xpassed,
        }
        try:
            with open(self.report_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            # Last resort: print to stderr so the runner can detect the problem.
            import sys
            print(f"antibody_plugin: failed to write report: {e}", file=sys.stderr)

"""
antibody_plugin.py — pytest plugin loaded with -p antibody_plugin.

Records collected node IDs, failed node IDs, and collection errors into
a JSON report file. The report path is given via the ANTIBODY_REPORT_PATH
environment variable.
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

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        """Called after each collector finishes. Capture collection errors."""
        if report.failed:
            self.errors.append(str(report.nodeid))

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.failed:
            self.failed.append(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        data = {
            "collected": self.collected,
            "failed": self.failed,
            "collection_errors": self.errors,
        }
        try:
            with open(self.report_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            # Last resort: print to stderr so the runner can detect the problem.
            import sys
            print(f"antibody_plugin: failed to write report: {e}", file=sys.stderr)

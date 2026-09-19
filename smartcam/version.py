"""The tool name and version recorded against every hash the system computes.

A hash report has to say what computed each value; "Smart Cam Monitoring" with no version cannot
be re-run or audited later.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def software_version() -> str:
    try:
        return f"Smart Cam Monitoring {version('smartcam')}"
    except PackageNotFoundError:
        return "Smart Cam Monitoring (unversioned)"

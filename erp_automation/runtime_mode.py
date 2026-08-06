"""Runtime identity and fixed paths for source-only local testing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


LOCAL_TEST_ENVIRONMENT_VARIABLE = "ERP_AUTOMATION_LOCAL_TEST"
LOCAL_TEST_HOME_DIRECTORY = "LingxingERP-LocalTest"


def is_local_test_mode(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this process is an explicitly launched local test."""

    environment = os.environ if environ is None else environ
    return str(environment.get(LOCAL_TEST_ENVIRONMENT_VARIABLE) or "").strip() == "1"


def expected_local_test_home(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the only writable root accepted for source local-test runs."""

    environment = os.environ if environ is None else environ
    local_appdata = str(environment.get("LOCALAPPDATA") or "").strip()
    if not local_appdata:
        raise RuntimeError("本机测试运行缺少 Windows LOCALAPPDATA。")
    return (Path(local_appdata) / LOCAL_TEST_HOME_DIRECTORY).resolve()


__all__ = [
    "LOCAL_TEST_ENVIRONMENT_VARIABLE",
    "LOCAL_TEST_HOME_DIRECTORY",
    "expected_local_test_home",
    "is_local_test_mode",
]

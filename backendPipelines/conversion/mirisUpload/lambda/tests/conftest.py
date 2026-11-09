#  Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Pytest configuration for the conversion/mirisUpload pipeline lambda tests.

Puts the lambda directory on the import path and stubs `customLogging` (aws_lambda_powertools is not
installed locally), so the handlers import without the production dependency.
"""

import os
import sys
from unittest.mock import MagicMock

_LAMBDA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LAMBDA_DIR not in sys.path:
    sys.path.insert(0, _LAMBDA_DIR)


def _make_customLogging_stub():
    """Return a minimal customLogging.logger stub module."""

    class _SafeLogger:
        def __init__(self, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

    stub = MagicMock()
    stub.safeLogger = _SafeLogger
    return stub


# Registered before any test module is collected, so the first handler import inside a test body
# succeeds.
_customLogging_stub = _make_customLogging_stub()
sys.modules.setdefault("customLogging", _customLogging_stub)
sys.modules.setdefault("customLogging.logger", _customLogging_stub)


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: standalone unit test (no AWS calls)")

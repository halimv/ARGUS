"""Every test session writes to its own workspace, never ARGUS/workspace/."""
import os
import tempfile

os.environ.setdefault("ARGUS_3D", "0")  # no 3D viewer servers in tests
os.environ.setdefault("ARGUS_WORKSPACE", tempfile.mkdtemp(prefix="argus-test-"))

import pytest  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"

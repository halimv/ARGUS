"""Native Windows can import simulation without bypassing physical robot locks."""
import errno
import os
import sys

import pytest

from argus.platform_compat import _unsupported_hardware_lock, load_simulation_server


def test_unsupported_lock_fails_explicitly():
    with pytest.raises(OSError) as caught:
        _unsupported_hardware_lock(1, 6)
    assert caught.value.errno == errno.ENOSYS


@pytest.mark.skipif(os.name != "nt", reason="Native Windows import compatibility")
def test_simulation_import_does_not_install_global_fcntl():
    factory = load_simulation_server()
    assert callable(factory)
    assert "fcntl" not in sys.modules


@pytest.mark.skipif(os.name != "nt", reason="Native Windows physical locks must fail closed")
def test_actual_ahc_robot_lock_cannot_succeed(tmp_path):
    load_simulation_server()
    from ahc.workspace.robot_lock import RobotLock
    lock = RobotLock("argus-compatibility-test")
    lock.path = tmp_path / "test.lock"
    with pytest.raises(OSError) as caught:
        lock.acquire({"purpose": "test"})
    assert caught.value.errno == errno.ENOSYS
    assert not lock.held

"""Import the pinned AHC simulator on Windows without enabling hardware access.

AHC imports Unix hardware-lock modules even when its requested mode is simulation.
The scoped import below supplies only the constants needed at import time. Any
attempt to use that unsupported lock fails explicitly; it is never a no-op lock.
The shim is removed from sys.modules after AHC imports, leaving unrelated imports
and the upstream submodule unchanged. POSIX uses the normal AHC implementation.
"""
from __future__ import annotations

import errno
import importlib
import os
import sys
import threading
import types

_IMPORT_LOCK = threading.RLock()


def _unsupported_hardware_lock(*_args, **_kwargs):
    raise OSError(errno.ENOSYS,
                  "Native Windows ARGUS supports AHC simulation only. "
                  "Hardware locking requires a supported AHC hardware runtime.")


def load_simulation_server():
    if os.name != "nt":
        return importlib.import_module("ahc.server").create_server
    with _IMPORT_LOCK:
        shim = types.ModuleType("fcntl")
        shim.LOCK_SH, shim.LOCK_EX, shim.LOCK_NB, shim.LOCK_UN = 1, 2, 4, 8
        shim.flock = _unsupported_hardware_lock
        shim.__doc__ = "ARGUS import compatibility only; hardware locking is unsupported."
        prior = sys.modules.get("fcntl")
        if prior is not None:
            raise RuntimeError("Unexpected fcntl implementation on native Windows; refusing ambiguous hardware-lock behavior")
        sys.modules["fcntl"] = shim
        try:
            # Both pinned AHC modules that import fcntl are initialized here.
            importlib.import_module("ahc.devices.hamilton.star_bridge")
            return importlib.import_module("ahc.server").create_server
        finally:
            if sys.modules.get("fcntl") is shim:
                del sys.modules["fcntl"]

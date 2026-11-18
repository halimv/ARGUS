"""Manage the unchanged full LanM application on loopback, using ARGUS's store.

The linked UI retains its own origin/security policy. Only an empty default
workspace receives shared starter data; existing directories are never restored.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import threading

from argus import config


def ensure_data_directory(data_dir: Path | None = None, *, existing_only: bool | None = None) -> Path:
    data = Path(data_dir or config.LANM_DATA).expanduser().resolve()
    existing_only = config.LANM_DATA_EXPLICIT if existing_only is None else existing_only
    database = data / "research.sqlite3"
    if database.is_file():
        return data
    if existing_only:
        raise ValueError(f"Existing LanM data directory must contain research.sqlite3: {data}. "
                         "Check the path; no data has been created or replaced.")
    if data.exists() and (not data.is_dir() or any(data.iterdir())):
        raise ValueError(f"LanM directory contains files but no research.sqlite3: {data}. "
                         "Refusing to restore over existing files.")
    data.parent.mkdir(parents=True, exist_ok=True)
    lock = data.parent / ("." + data.name + ".starter.lock")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RuntimeError(f"Another LanM initialization may be in progress: {lock}") from error
    os.close(descriptor)
    try:
        if database.is_file():
            return data
        if data.exists() and (not data.is_dir() or any(data.iterdir())):
            raise ValueError("LanM files appeared during initialization; refusing to overwrite them")
        root = config.LANM_SRC.parents[1]
        source = root / "scripts" / "research_snapshot.py"
        specification = importlib.util.spec_from_file_location("argus_lanm_snapshot", source)
        if specification is None or specification.loader is None:
            raise RuntimeError("LanM starter restoration utility is unavailable; initialize the submodule")
        snapshot = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(snapshot)
        with tempfile.TemporaryDirectory(prefix=".lanm-starter-", dir=data.parent) as temporary:
            snapshot.restore_snapshot(root / "portable-data" / "research-snapshot.zip", Path(temporary))
            if data.exists():
                data.rmdir()  # Refuses any nonempty directory; never remove research files.
            os.rename(Path(temporary) / "research-data", data)
    finally:
        lock.unlink()
    return data


class LanMWorkspace:
    """Own one full LanM HTTP service; startup makes no model requests."""

    def __init__(self, data_dir: Path | None = None, *, port: int | None = None,
                 existing_only: bool | None = None):
        self.data_dir = Path(data_dir or config.LANM_DATA).resolve()
        self.port = config.LANM_PORT if port is None else port
        self.existing_only = config.LANM_DATA_EXPLICIT if existing_only is None else existing_only
        self.application = self.server = self.thread = None

    def start(self):
        if self.server is not None:
            return self
        from http.server import ThreadingHTTPServer
        from lanm_platform.server import Application, handler_for

        ensure_data_directory(self.data_dir, existing_only=self.existing_only)
        application = Application(self.data_dir)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.port), handler_for(application))
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, name="argus-lanm-workspace", daemon=True)
            thread.start()
        except BaseException:
            application.close()
            if "server" in locals():
                server.server_close()
            raise
        self.application, self.server, self.thread = application, server, thread
        return self

    def info(self):
        return {"available": self.server is not None,
                "url": f"http://127.0.0.1:{self.server.server_port}/" if self.server else None,
                "embedded_url": "/research/?embed=argus",
                "data_dir": str(self.data_dir), "shared_with_argus_ranking": True,
                "capabilities": ["research_chat", "evidence", "all_lanm_tools", "build", "custom_methods"]}

    def close(self):
        if self.server is None:
            return
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            self.application.close()
            self.application = self.server = self.thread = None

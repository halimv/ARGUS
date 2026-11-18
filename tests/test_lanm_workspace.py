"""The complete research service shares ARGUS's store without migrating history."""
import hashlib
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from argus import config
from argus.lanm_workspace import LanMWorkspace, ensure_data_directory
from argus.tools import lanm


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    data = tmp_path / "lanm"
    monkeypatch.setattr(config, "LANM_DATA", data)
    monkeypatch.setattr(config, "LANM_DATA_EXPLICIT", False)
    monkeypatch.setattr(config, "LANM_PORT", 0)
    monkeypatch.setattr(lanm, "_store", None)
    monkeypatch.setenv("LANM_DATA_DIR", str(data))
    monkeypatch.delenv("LANM_PROFILES_FILE", raising=False)
    monkeypatch.delenv("LANM_DEFAULT_PROFILE", raising=False)
    return data


def request(workspace, path, payload=None, **headers):
    body = json.dumps(payload).encode() if payload is not None else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    return urlopen(Request(workspace.info()["url"] + path.lstrip("/"), body, headers), timeout=10)


def read_json(workspace, path, payload=None):
    with request(workspace, path, payload) as response:
        return json.load(response)


def test_default_restores_shared_records_and_registered_methods_only(data_dir):
    ensure_data_directory()
    with closing(sqlite3.connect(data_dir / "research.sqlite3")) as database:
        assert database.execute("SELECT count(*) FROM candidates").fetchone()[0] == 616
        assert database.execute("SELECT count(*) FROM library_records").fetchone()[0] == 441
        for table in ("projects", "messages", "experiments", "runs", "sources"):
            assert database.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    from lanm_platform.methods import Registry
    versions = [method for method in Registry(data_dir).list() if method.get("status") == "registered"]
    assert len(versions) == 2


def test_existing_data_and_method_files_are_never_restored_over(data_dir):
    store = lanm.store()
    project = store.create_project("Retained in ARGUS", "Dy", "Y")
    message = store.add_message(project["id"], "user", "Keep this research conversation")
    methods = {str(path.relative_to(data_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (data_dir / "methods").rglob("*") if path.is_file()}
    before = (data_dir / "research.sqlite3").read_bytes()
    assert ensure_data_directory(existing_only=True) == data_dir
    assert (data_dir / "research.sqlite3").read_bytes() == before
    workspace = LanMWorkspace(existing_only=True).start()
    try:
        assert read_json(workspace, f"/api/projects/{project['id']}/messages")["messages"] == [message]
        assert len(read_json(workspace, "/api/projects")["projects"]) == 1
    finally:
        workspace.close()
    assert methods == {str(path.relative_to(data_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in (data_dir / "methods").rglob("*") if path.is_file()}


def test_mistyped_explicit_path_and_partial_default_refuse_without_writes(data_dir):
    with pytest.raises(ValueError, match="must contain research.sqlite3"):
        ensure_data_directory(existing_only=True)
    assert not data_dir.exists()
    data_dir.mkdir()
    sentinel = data_dir / "keep.txt"
    sentinel.write_text("existing content")
    with pytest.raises(ValueError, match="Refusing to restore"):
        ensure_data_directory()
    assert sentinel.read_text() == "existing content"
    assert not (data_dir / "research.sqlite3").exists()


def test_full_ui_evidence_build_and_methods_use_original_handler(data_dir, monkeypatch):
    workspace = LanMWorkspace().start()
    thread = workspace.thread
    submissions = []
    monkeypatch.setattr(workspace.application.runtime, "submit",
                        lambda *args: submissions.append(args) or SimpleNamespace(id="isolated-job"))
    try:
        with request(workspace, "/") as response:
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert b"LanM" in response.read()
        assert read_json(workspace, "/api/status")["modes"] == ["research", "build"]
        project = read_json(workspace, "/api/projects")["projects"][0]
        assert len(read_json(workspace, f"/api/projects/{project['id']}/sources")["sources"]) == 14
        assert len(read_json(workspace, "/api/methods")["methods"]) >= 3
        registered = next(method for method in workspace.application.registry.list()
                          if method.get("status") == "registered")
        method = workspace.application.registry.get(registered["id"])
        reference = method["reference_cases"][0]
        calculation = read_json(workspace, f"/api/projects/{project['id']}/calculate",
                                {"method_id": method["id"], "inputs": reference["inputs"]})
        assert calculation["method_id"] == method["id"]
        for mode in ("research", "build"):
            assert read_json(workspace, f"/api/projects/{project['id']}/chat",
                             {"message": "isolated routing check", "mode": mode})["job_id"] == "isolated-job"
        assert [call[2] for call in submissions] == ["research", "build"]
        with pytest.raises(HTTPError) as refused:
            request(workspace, "/api/projects", {"name": "Cross-origin"}, Origin="https://example.invalid")
        assert refused.value.code == 403
    finally:
        workspace.close()
    assert not thread.is_alive()
    assert workspace.info()["available"] is False


def inputs(store, project_id):
    return {key: value for key, value in store.get_project(project_id).items() if key != "latest_run"}


def test_project_ranking_and_http_share_records_without_changing_inputs(data_dir):
    workspace = LanMWorkspace().start()
    try:
        project = read_json(workspace, "/api/projects", {"name": "Selected Dy/Y", "target": "Dy", "competitor": "Y"})
        store = lanm.store()
        candidate = store.rank(project["id"], pool="untested", limit=1)["rows"][0]["candidate_id"]
        store.update_assumptions(project["id"], {"usable_lifetime_cycles": 25},
                                 "Isolated candidate-specific record", candidate_id=candidate)
        before = inputs(store, project["id"])
        project_count = len(store.list_projects())
        result = lanm.rank_variants("Dy", "Y", 3, project_id=project["id"])
        assert result["lanm_project_id"] == project["id"]
        assert len(result["variants"]) == 3
        assert len(store.list_projects()) == project_count
        assert inputs(store, project["id"]) == before
        with pytest.raises(ValueError, match="Selected project uses Dy/Y"):
            lanm.rank_variants("Nd", "La", 3, project_id=project["id"])
        assert inputs(store, project["id"]) == before
        runs = read_json(workspace, f"/api/projects/{project['id']}/runs")["runs"]
        assert result["lanm_ranking_run_id"] in {run["id"] for run in runs}
    finally:
        workspace.close()


def test_argus_starts_research_without_venus_and_rejects_protocol_before_model(data_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from argus import server
    from pipeline import paths
    monkeypatch.setattr(paths, "labware", lambda: data_dir.parent / "no-venus")
    with TestClient(server.app) as client:
        assert client.get("/").status_code == 200
        workspace = client.get("/api/lanm-workspace").json()
        assert workspace["available"]
        assert workspace["data_dir"] == str(data_dir)
        assert client.get("/lanm", follow_redirects=False).headers["location"] == workspace["url"]
        assert client.get("/api/lanm-projects").json()["projects"]
        assert client.get("/api/defaults").json()["protocol_setup"]["available"] is False
        response = client.post("/api/runs", json={"prompt": "No model call", "sop_text": "Not executed"})
        assert response.status_code == 503
        assert "configure the VENUS labware" in response.json()["detail"]


@pytest.mark.anyio
async def test_run_keeps_selected_project_and_explains_its_pair(data_dir, monkeypatch):
    from argus.run import Run
    from argus.agent import first_message
    monkeypatch.setattr(config, "RUNS", data_dir.parent / "runs")
    project = lanm.store().create_project("Shared workflow", "Dy", "Y")
    before = inputs(lanm.store(), project["id"])
    run = Run("Rank three candidates", "Not executed", project_id=project["id"])
    assert "target Dy, competitor Y" in first_message(run)
    result = await run.rank_variants("Dy", "Y", 3)
    assert result["lanm_project_id"] == project["id"]
    assert run.snapshot()["project_id"] == project["id"]
    assert inputs(lanm.store(), project["id"]) == before
    with pytest.raises(ValueError, match="Project not found"):
        Run("Invalid reference", "Not executed", project_id="missing-project")

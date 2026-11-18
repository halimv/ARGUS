"""The embedded workspace uses the real service without weakening its boundary."""
import asyncio
from types import SimpleNamespace
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from argus import config
from argus.tools import lanm


@pytest.fixture
def workspace_client(tmp_path, monkeypatch):
    data = tmp_path / "research"
    monkeypatch.setattr(config, "LANM_DATA", data)
    monkeypatch.setattr(config, "LANM_DATA_EXPLICIT", False)
    monkeypatch.setattr(config, "LANM_PORT", 0)
    monkeypatch.setattr(lanm, "_store", None)
    monkeypatch.setenv("LANM_DATA_DIR", str(data))
    monkeypatch.delenv("LANM_PROFILES_FILE", raising=False)
    monkeypatch.delenv("LANM_DEFAULT_PROFILE", raising=False)
    from argus import server
    with TestClient(server.app, base_url="http://127.0.0.1:8780") as client:
        yield client, server.app.state.lanm_workspace


def test_complete_workspace_routes_use_same_project_and_methods(workspace_client, monkeypatch):
    client, workspace = workspace_client
    response = client.get("/research/?embed=argus")
    assert response.status_code == 200
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/research/app.js").status_code == 200
    assert client.get("/research/integration.js").status_code == 200
    assert client.get("/research/styles.css").status_code == 200
    assert client.get("/research?embed=argus", follow_redirects=False).headers["location"] == "/research/?embed=argus"
    assert client.get("/api/lanm-workspace").json()["embedded_url"] == "/research/?embed=argus"
    with urlopen(workspace.info()["url"], timeout=5) as direct:
        assert "frame-ancestors 'none'" in direct.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in client.get("/research/api/status").headers["content-security-policy"]

    response = client.post("/research/api/projects", json={"name": "Embedded test", "target": "Dy", "competitor": "Y"},
                           headers={"Origin": "http://127.0.0.1:8780", "Referer": "http://127.0.0.1:8780/research/?embed=argus"})
    assert response.status_code == 201
    project = response.json()
    assert project["id"] in {row["id"] for row in client.get("/api/lanm-projects").json()["projects"]}
    changed = client.patch(f"/research/api/projects/{project['id']}", json={"changes": {"name": "Shared selected project"}})
    assert changed.status_code == 200
    assert workspace.application.store.get_project(project["id"])["name"] == "Shared selected project"
    candidates = client.get("/research/api/candidates", params={"project_id": project["id"], "limit": 5})
    assert candidates.status_code == 200
    assert len(candidates.json()["candidates"]) == 5
    full_library = client.get("/research/api/candidates", params={"project_id": project["id"], "limit": 1000}).json()
    assert len(full_library["candidates"]) == full_library["total"] == 616
    assert len(client.get(f"/research/api/projects/{project['id']}/sources").json()["sources"]) == 14
    methods = client.get("/research/api/methods").json()["methods"]
    registered = next(method for method in methods if method.get("status") == "registered")
    method = workspace.application.registry.get(registered["id"])
    calculation = client.post(f"/research/api/projects/{project['id']}/calculate",
                              json={"method_id": method["id"], "inputs": method["reference_cases"][0]["inputs"]})
    assert calculation.status_code == 200
    assert calculation.json()["method_id"] == method["id"]
    submissions = []
    monkeypatch.setattr(workspace.application.runtime, "submit",
                        lambda *args: submissions.append(args) or SimpleNamespace(id="stubbed-only"))
    for mode in ("research", "build"):
        response = client.post(f"/research/api/projects/{project['id']}/chat", json={"message": "Route only", "mode": mode})
        assert response.status_code == 202
    assert [call[2] for call in submissions] == ["research", "build"]


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.invalid"}, {"Host": "127.0.0.1.attacker.invalid:8780"},
    {"Origin": "https://attacker.invalid"}, {"Origin": "null"},
    {"Origin": "http://127.0.0.1:8781"}, {"Origin": "http://localhost:8780"},
    {"Referer": "https://attacker.invalid/form"}, {"Sec-Fetch-Site": "cross-site"},
])
def test_foreign_requests_are_refused_before_mutation(workspace_client, headers):
    client, workspace = workspace_client
    before = workspace.application.store.list_projects()
    assert client.get("/research/api/projects", headers=headers).status_code == 403
    assert client.post("/research/api/projects", json={"name": "Must not exist"}, headers=headers).status_code == 403
    assert workspace.application.store.list_projects() == before


def test_no_arbitrary_target_body_limit_or_preflight(workspace_client):
    client, workspace = workspace_client
    assert client.get("/research/%2F%2Fattacker.invalid/").status_code == 400
    assert client.get("/research/%5C%5Cattacker.invalid/").status_code == 400
    assert client.get("/research/%2E%2E/api/status").status_code == 400
    assert client.options("/research/api/projects", headers={"Origin": "https://attacker.invalid"}).status_code == 405
    response = client.post("/research/api/projects", content=b"x" * 2_000_001,
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    # Forwarding headers cannot redirect the destination or bypass Origin checks.
    response = client.get("/research/api/status", headers={"X-Forwarded-Host": "attacker.invalid", "X-Upstream-URL": "https://attacker.invalid"})
    assert response.status_code == 200
    assert response.json()["data_dir"] == str(workspace.data_dir)


def test_download_bytes_ranges_and_original_disposition_survive(workspace_client, monkeypatch):
    client, workspace = workspace_client
    from lanm_platform import server as research_server
    raw = b"%PDF-1.4\nverified original file bytes\n"
    monkeypatch.setattr(research_server, "read_original", lambda *args: (raw, "application/pdf", "evidence.pdf"))
    project = workspace.application.store.list_projects()[0]
    route = f"/research/api/projects/{project['id']}/sources/fixture/file"
    downloaded = client.get(route + "?download=1")
    assert downloaded.content == raw
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    ranged = client.get(route, headers={"Range": "bytes=0-7"})
    assert ranged.status_code == 206
    assert ranged.content == raw[:8]
    assert ranged.headers["content-range"] == f"bytes 0-7/{len(raw)}"
    assert ranged.headers["accept-ranges"] == "bytes"
    assert client.get(route, headers={"Range": "bytes=9999-"}).status_code == 416


@pytest.mark.anyio
async def test_sse_delivers_an_event_before_job_completion(workspace_client):
    _, workspace = workspace_client
    from argus.research_proxy import proxy_research
    from lanm_platform.runtime import Job
    project = workspace.application.store.list_projects()[0]
    job = Job(project["id"], "No model request", "research", "test")
    workspace.application.runtime.jobs[job.id] = job
    job.emit("text", "First event")
    path = f"/research/api/jobs/{job.id}/events"
    request = Request({"type": "http", "method": "GET", "scheme": "http", "path": path,
                       "raw_path": path.encode(), "query_string": b"", "headers": [(b"host", b"127.0.0.1:8780")]})
    response = await proxy_research(request, workspace)
    iterator = response.body_iterator
    try:
        first = await asyncio.wait_for(anext(iterator), timeout=3)
        assert b"First event" in first
        assert job.done is False
        job.emit("done")
        remaining = b"".join([chunk async for chunk in iterator])
        assert b'"type": "done"' in remaining
        assert response.headers["x-accel-buffering"] == "no"
    finally:
        job.emit("done")
        await iterator.aclose()

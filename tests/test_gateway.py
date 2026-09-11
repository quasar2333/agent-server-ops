import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_server_ops.app import create_app
from agent_server_ops.config import load_config
from agent_server_ops.gateway import initialize
from conftest import python_command, wait


def submit(client, command="echo ready", **kwargs):
    return client.post("/ops/api/jobs", json={"command": command, **kwargs},
                       headers={"Idempotency-Key": uuid.uuid4().hex})


def test_health_public_but_operations_need_auth(client):
    assert client.get("/ops/healthz", headers={"Authorization": ""}).json()["ok"]
    for route in ("status", "jobs", "operations", "files"):
        response = client.get("/ops/api/" + route, headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
    assert client.get("/ops/api/status").json()["disk"]["total"] > 0


def test_init_preserves_credentials_and_validates_layout(setup):
    cfg, headers, root = setup
    original = (root / "gateway.token").read_bytes()
    with pytest.raises(ValueError, match="Already initialized"):
        initialize(root)
    assert (root / "gateway.token").read_bytes() == original
    cfg["state_dir"] = cfg["workspace"]
    (root / "gateway.json").write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="separate"):
        load_config(root / "gateway.json")


def test_json_and_idempotency_validation(client):
    assert client.post("/ops/api/jobs", json={"command": "echo no"}).status_code == 400
    headers = {"Idempotency-Key": "validation-1234"}
    assert client.post("/ops/api/jobs", content=b"x", headers=headers).status_code == 400
    assert client.post("/ops/api/jobs", content=b"x" * 140000, headers=headers).status_code == 413
    assert client.post("/ops/api/jobs", json={"command": "echo a", "timeout": -1}, headers=headers).status_code == 422
    assert client.post("/ops/api/jobs", json={"command": "echo a", "resources": []}, headers=headers).status_code == 422


def test_duplicate_submit_runs_once_and_conflicting_key_rejected(client, setup):
    command = python_command("from pathlib import Path; p=Path('counter'); p.write_text(p.read_text()+'x' if p.exists() else 'x')")
    headers = {"Idempotency-Key": "duplicate-1234"}
    a = client.post("/ops/api/jobs", json={"command": command}, headers=headers).json()
    b = client.post("/ops/api/jobs", json={"command": command}, headers=headers).json()
    assert a["jobId"] == b["jobId"]
    assert wait(client, a["jobId"])["ok"]
    assert (Path(setup[0]["workspace"]) / "counter").read_text() == "x"
    assert client.post("/ops/api/jobs", json={"command": "echo changed"}, headers=headers).status_code == 409


def test_failure_is_not_success_and_unsafe_resume_rejected(client):
    job = submit(client, python_command("import sys; print('失败'); sys.exit(7)")).json()
    result = wait(client, job["jobId"])
    assert result["status"] == "failed" and result["exitCode"] == 7 and result["ok"] is False
    assert client.post(f"/ops/api/jobs/{job['jobId']}/resume", headers={"Idempotency-Key": "unsafe-resume"}).status_code == 409


def test_unicode_logs_use_byte_cursor(client):
    job = submit(client, python_command("print('中文 🌏'); print('done')")).json()
    wait(client, job["jobId"])
    logs = client.get(f"/ops/api/jobs/{job['jobId']}/logs").json()
    assert "中文 🌏" in "".join(r["text"] for r in logs["rows"])
    assert logs["done"]
    next_page = client.get(f"/ops/api/jobs/{job['jobId']}/logs?cursor={logs['nextCursor']}").json()
    assert next_page["rows"] == [] and next_page["nextCursor"] == logs["nextCursor"]


def test_resource_lock_serializes_jobs(client, setup):
    code = "import time; from pathlib import Path; p=Path('order'); p.open('a').write('start\\n'); time.sleep(.2); p.open('a').write('end\\n')"
    jobs = [submit(client, python_command(code), resources=["same-app"]).json() for _ in range(3)]
    for job in jobs:
        assert wait(client, job["jobId"])["ok"]
    assert (Path(setup[0]["workspace"]) / "order").read_text().splitlines() == ["start", "end"] * 3


def test_cancel_and_timeout_stop_processes(client, setup):
    command = python_command("import time; from pathlib import Path; time.sleep(3); Path('must-not-exist').write_text('oops')")
    first = submit(client, command).json()
    second = submit(client, command).json()
    assert client.post(f"/ops/api/jobs/{second['jobId']}/cancel").json()["status"] == "cancelled"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/ops/api/jobs/{first['jobId']}").json()["status"] == "running":
            break
        time.sleep(.01)
    assert client.post(f"/ops/api/jobs/{first['jobId']}/cancel").json()["status"] == "cancelled"
    third = submit(client, command, timeout=1, retrySafe=True).json()
    assert wait(client, third["jobId"])["status"] == "timed_out"
    resumed = client.post(f"/ops/api/jobs/{third['jobId']}/resume", headers={"Idempotency-Key": "resume-key-1234"}).json()
    assert resumed["jobId"] != third["jobId"] and resumed["parent"] == third["jobId"]
    assert client.post(f"/ops/api/jobs/{third['jobId']}/resume", headers={"Idempotency-Key": "resume-key-1234"}).json()["jobId"] == resumed["jobId"]
    assert wait(client, resumed["jobId"])["status"] == "timed_out"
    time.sleep(1.1)
    assert not (Path(setup[0]["workspace"]) / "must-not-exist").exists()


def test_operation_stops_at_first_failed_stage(setup):
    cfg, headers, root = setup
    cfg["allow_shell"] = False
    cfg["operations"]["deploy"] = {"stages": [
        {"name": "build", "command": [sys.executable, "-c", "import sys; sys.exit(4)"]},
        {"name": "activate", "command": [sys.executable, "-c", "from pathlib import Path; Path('activated').touch()"]}]}
    with TestClient(create_app(cfg), headers=headers) as client:
        assert submit(client).status_code == 403
        catalog = client.get("/ops/api/operations").json()
        assert any(x["name"] == "deploy" for x in catalog["operations"])
        response = client.post("/ops/api/operations/deploy", json={}, headers={"Idempotency-Key": "operation-1234"})
        assert response.status_code == 202
        result = wait(client, response.json()["jobId"])
        assert result["status"] == "failed" and result["stage"] == "build"
    assert not (Path(cfg["workspace"]) / "activated").exists()


def test_transfer_checksum_limits_and_overwrite(client, setup):
    data = "中文 contents".encode()
    headers = {"X-Content-SHA256": hashlib.sha256(data).hexdigest()}
    route = "/ops/api/files/content?path=folder/file.txt"
    assert client.put(route, content=data, headers=headers).json()["ok"]
    assert client.get(route).content == data
    assert client.put(route, content=data, headers=headers).status_code == 409
    assert client.put(route + "&overwrite=true", content=b"wrong", headers=headers).status_code == 422
    assert client.get(route).content == data
    setup[0]["max_upload_bytes"] = 1
    assert client.put(route + "&overwrite=true", content=data, headers=headers).status_code == 413
    assert client.get(route).content == data
    assert not list(Path(setup[0]["workspace"]).rglob(".upload-*"))


@pytest.mark.parametrize("path", ["../gateway.token", "../../outside", "/etc/passwd", "C:/Windows", "..\\outside"])
def test_file_paths_cannot_escape(client, path):
    response = client.get("/ops/api/files/content", params={"path": path})
    assert response.status_code in {400, 403}


def test_symlink_escape_rejected(client, setup):
    cfg, _, root = setup
    try:
        (Path(cfg["workspace"]) / "escape").symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks needs Windows developer mode or administrator rights")
    assert client.get("/ops/api/files/content?path=escape/gateway.token").status_code == 403


def test_restart_preserves_jobs_and_marks_running_interrupted(setup):
    cfg, headers, root = setup
    with TestClient(create_app(cfg), headers=headers) as client:
        job = submit(client, python_command("import time; time.sleep(20)")).json()
        deadline = time.monotonic() + 5
        while client.get(f"/ops/api/jobs/{job['jobId']}").json()["status"] != "running":
            assert time.monotonic() < deadline
            time.sleep(.01)
    with TestClient(create_app(cfg), headers=headers) as client:
        result = client.get(f"/ops/api/jobs/{job['jobId']}").json()
        assert result["status"] == "interrupted" and not result["ok"]
        assert client.get("/ops/api/status").status_code == 200

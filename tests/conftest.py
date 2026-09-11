import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from agent_server_ops.app import create_app
from agent_server_ops.config import load_config
from agent_server_ops.gateway import initialize


@pytest.fixture
def setup(tmp_path):
    initialize(tmp_path / "gateway")
    root = tmp_path / "gateway"
    cfg = load_config(root / "gateway.json")
    token = (root / "gateway.token").read_text().strip()
    return cfg, {"Authorization": "Bearer " + token}, root


@pytest.fixture
def client(setup):
    cfg, headers, root = setup
    with TestClient(create_app(cfg), headers=headers) as c:
        yield c


def wait(client, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = client.get("/ops/api/jobs/" + job_id).json()
        if value["status"] not in {"queued", "running"}:
            return value
        time.sleep(0.025)
    raise AssertionError("job did not finish: " + json.dumps(value))


def python_command(code):
    if sys.platform == "win32":
        quote = lambda s: "'" + s.replace("'", "''") + "'"
        return "& " + quote(sys.executable) + " -c " + quote(code)
    import shlex
    return shlex.join([sys.executable, "-c", code])

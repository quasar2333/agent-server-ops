import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_server_ops.cli import Client, NoRedirect, submit, validate_base


def test_client_requires_secure_remote_transport():
    assert validate_base("http://127.0.0.1:9876")
    assert validate_base("http://[::1]:9876")
    assert validate_base("https://ops.example.com")
    with pytest.raises(ValueError):
        validate_base("http://ops.example.com")
    with pytest.raises(ValueError):
        validate_base("https://token@ops.example.com")
    assert validate_base("http://10.0.0.3:9876", True)


def test_receipt_exists_before_network_and_is_never_overwritten(tmp_path):
    receipt = tmp_path / "receipt.json"
    class BrokenClient:
        base = "https://ops.example.com"
        def request(self, method, route, payload, key):
            saved = json.loads(receipt.read_text())
            assert saved["requestKey"] == key
            assert saved["payload"] == payload
            raise OSError("response lost")
    args = argparse.Namespace(request_key="stable-key", receipt=str(receipt), server="test")
    with pytest.raises(OSError, match="response lost"):
        submit(BrokenClient(), "/ops/api/jobs", {"command": "echo ready"}, args)
    before = receipt.read_bytes()
    with pytest.raises(FileExistsError):
        submit(BrokenClient(), "/ops/api/jobs", {"command": "echo changed"}, args)
    assert receipt.read_bytes() == before
    if os.name != "nt":
        assert receipt.stat().st_mode & 0o777 == 0o600


def test_client_does_not_follow_redirect_or_forward_token():
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(f"http://127.0.0.1:{server.server_port}/initial", timeout=2)
        assert error.value.code == 302
        assert hits == ["/initial"]
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


@contextmanager
def live_gateway_process(setup, tmp_path):
    cfg, headers, root = setup
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = (tmp_path / "gateway.log").open("w+")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = subprocess.Popen([sys.executable, "-m", "agent_server_ops.gateway", "serve",
                                "--config", str(root / "gateway.json"), "--port", str(port)],
                               stdout=log, stderr=log, env=env)
    client_cfg = tmp_path / "client.json"
    client_cfg.write_text(json.dumps({"servers": {"default": {"url": f"http://127.0.0.1:{port}",
                                      "tokenFile": str(root / "gateway.token")}}}))
    client = Client(json.loads(client_cfg.read_text())["servers"]["default"])
    try:
        for _ in range(200):
            try:
                if client.request("GET", "/ops/healthz")["ok"]:
                    break
            except OSError:
                time.sleep(.05)
        else:
            log.seek(0)
            raise AssertionError(log.read())
        def cli(*args):
            result = subprocess.run([sys.executable, "-m", "agent_server_ops.cli", "--config", str(client_cfg), *map(str, args)],
                                    capture_output=True, text=True, encoding="utf-8", env=env, timeout=15)
            return result, json.loads(result.stdout)
        yield cli, client, process
    finally:
        if process.poll() is None:
            for job in client.request("GET", "/ops/api/jobs")["jobs"]:
                if job["status"] in {"queued", "running"}:
                    client.request("POST", f"/ops/api/jobs/{job['jobId']}/cancel", {})
            process.terminate()
        process.wait(timeout=15)
        log.close()


@pytest.fixture
def live_gateway(setup, tmp_path):
    with live_gateway_process(setup, tmp_path) as gateway:
        yield gateway


def test_real_cli_server_roundtrip(live_gateway, tmp_path):
    cli, client, process = live_gateway
    result, value = cli("status")
    assert result.returncode == 0 and value["hostname"]
    receipt = tmp_path / "receipt.json"
    command = tmp_path / "command.txt"
    command.write_text("echo ready", encoding="utf-8")
    result, value = cli("run", "--command-file", command, "--receipt", receipt, "--stream")
    assert result.returncode == 0 and value["ok"] and "ready" in result.stderr
    result, replay = cli("job", "reconcile", receipt)
    assert result.returncode == 0 and replay["jobId"] == value["jobId"]
    result, logs = cli("job", "logs", value["jobId"])
    assert logs["done"]
    source, dest = tmp_path / "source.txt", tmp_path / "downloaded.txt"
    source.write_bytes("hello 世界".encode())
    result, upload = cli("files", "upload", source, "demo.txt")
    assert result.returncode == 0 and upload["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    result, download = cli("files", "download", "demo.txt", dest)
    assert result.returncode == 0 and dest.read_bytes() == source.read_bytes()
    result, catalog = cli("operation", "list")
    assert catalog["operations"][0]["name"] == "python-check"
    result, operation = cli("operation", "run", "python-check", "--receipt", tmp_path / "op-receipt.json")
    assert result.returncode == 0 and operation["ok"]
    result, skill = cli("install-skill", "--target", tmp_path / "skill")
    assert result.returncode == 0 and (Path(skill["skill"]) / "SKILL.md").is_file()


def test_cli_wait_expiry_does_not_cancel(live_gateway, tmp_path):
    from conftest import python_command
    cli, client, process = live_gateway
    result, value = cli("run", "--command", python_command("import time; time.sleep(2)"),
                        "--receipt", tmp_path / "slow.json", "--wait-seconds", 0)
    assert result.returncode == 2 and value["waitingExpired"]
    result, finished = cli("job", "wait", value["jobId"])
    assert result.returncode == 0 and finished["ok"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX forced-kill recovery smoke; Windows covered by lifecycle and queue tests")
def test_hard_crash_recovers_orphan_process(live_gateway, setup):
    import psutil
    from conftest import python_command
    cli, client, process = live_gateway
    job = client.request("POST", "/ops/api/jobs", {"command": python_command("import time; time.sleep(60)")}, key="crash-recovery-123")
    for _ in range(100):
        state = client.request("GET", f"/ops/api/jobs/{job['jobId']}")
        if state["pid"]:
            break
        time.sleep(.025)
    assert state["pid"]
    pid = state["pid"]
    process.kill()
    process.wait(timeout=10)
    from fastapi.testclient import TestClient
    from agent_server_ops.app import create_app
    with TestClient(create_app(setup[0]), headers=setup[1]) as recovery:
        result = recovery.get(f"/ops/api/jobs/{job['jobId']}").json()
        assert result["status"] == "interrupted" and not result["ok"]
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE

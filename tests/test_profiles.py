import json
import os

import pytest

from agent_server_ops import cli, profiles
from test_cli import live_gateway, live_gateway_process


def invoke(config, *args):
    return cli.execute(cli.parser().parse_args(["--config", str(config), *args]))


def register(config, token, node, *, name=None, url="https://ops.example.com"):
    return invoke(config, "server", "add", *([name] if name else []), "--school", "东城区培新小学",
                  "--project", "电子书包-整书阅读", "--node", node, "--url", url, "--token-file", str(token))


def test_registration_preserves_legacy_alias_and_never_prints_credentials(tmp_path):
    config, token = tmp_path / "client.json", tmp_path / "private.token"
    token.write_text("secret-test-token-" * 4)
    config.write_text(json.dumps({"custom": "preserve", "servers": {"production": {
        "url": "https://old.example.com", "tokenFile": str(token), "caFile": "private-ca.pem",
        "futureSetting": 7,
    }}}))
    before = json.loads(config.read_text(encoding="utf-8"))["servers"]["production"]
    labeled = invoke(config, "server", "label", "production", "--school", "东城区培新小学",
                     "--project", "电子书包-整书阅读", "--node", "生产")
    assert labeled["name"] == "production" and labeled["displayName"].endswith("/生产")
    identity = labeled["profileId"]
    invoke(config, "server", "label", "production", "--school", "东城区培新小学",
           "--project", "电子书包-整书阅读", "--node", "生产")
    after = json.loads(config.read_text(encoding="utf-8"))
    assert after["custom"] == "preserve"
    assert all(after["servers"]["production"][k] == v for k, v in before.items())
    assert after["servers"]["production"]["profileId"] == identity
    added = register(config, token, "影子验证")
    assert added["name"] == "东城区培新小学/电子书包-整书阅读/影子验证"
    listing = invoke(config, "server", "list")
    output = json.dumps(listing)
    assert len(listing["servers"]) == 2
    assert "tokenFile" not in output and str(token) not in output and token.read_text(encoding="utf-8") not in output
    assert set(json.loads(config.read_text(encoding="utf-8"))["servers"]) == {"production", added["name"]}
    if os.name != "nt": assert config.stat().st_mode & 0o777 == 0o600


def test_ambiguous_or_implicit_selection_never_sends_network(tmp_path, monkeypatch):
    config, token = tmp_path / "client.json", tmp_path / "private.token"
    token.write_text("x" * 48)
    register(config, token, "生产", name="default")
    register(config, token, "影子验证")
    def forbidden(*_args, **_kwargs): raise AssertionError("client constructed before selection")
    monkeypatch.setattr(cli, "Client", forbidden)
    for args in [("health",), ("--school", "东城区培新小学", "status"),
                 ("--school", "东城区培新小学", "--project", "电子书包-整书阅读", "status"),
                 ("--server", "default", "--node", "生产", "health"),
                 ("--server", "missing", "run", "--command", "echo must-not-run")]:
        with pytest.raises(ValueError): invoke(config, *args)


def test_labeled_routing_and_receipt_cannot_cross_profile_even_at_same_url(tmp_path, monkeypatch):
    config, token = tmp_path / "client.json", tmp_path / "private.token"
    token.write_text("x" * 48)
    a = register(config, token, "生产")
    b = register(config, token, "影子验证")
    sent = []
    class Client:
        def __init__(self, profile): self.base = profile["url"]; self.profile = profile
        def request(self, method, route, payload=None, **kwargs):
            sent.append((self.profile["profileId"], method, route, payload, kwargs))
            return {"ok": True, "jobId": "a" * 32}
    monkeypatch.setattr(cli, "Client", Client)
    receipt = tmp_path / "receipt.json"
    value = invoke(config, "--school", "东城区培新小学", "--project", "电子书包-整书阅读",
                   "--node", "生产", "run", "--command", "echo ready", "--detach", "--receipt", str(receipt))
    assert value["target"]["profileId"] == a["profileId"] == sent[0][0]
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert saved["server"] == a["name"] and saved["target"] == value["target"]
    with pytest.raises(ValueError, match="different server profile identity"):
        invoke(config, "--server", b["name"], "job", "reconcile", str(receipt))
    assert len(sent) == 1
    invoke(config, "--server", a["name"], "job", "reconcile", str(receipt))
    assert len(sent) == 2 and sent[1][4]["key"] == saved["requestKey"]
    saved.pop("target"); receipt.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="Legacy receipt"):
        invoke(config, "--server", b["name"], "job", "reconcile", str(receipt))
    assert len(sent) == 2


def test_conflicts_validation_and_failed_writes_preserve_previous_config(tmp_path, monkeypatch):
    config, token = tmp_path / "client.json", tmp_path / "private.token"
    token.write_text("x" * 48)
    register(config, token, "生产")
    original = config.read_bytes()
    for action in [lambda: register(config, token, "生产", name="other-alias"),
                   lambda: register(config, token, "bad\nlabel"),
                   lambda: invoke(config, "server", "add", "incomplete", "--school", "学校", "--url", "https://a.example", "--token-file", str(token))]:
        with pytest.raises(ValueError): action()
        assert config.read_bytes() == original
    def fail_replace(*args): raise OSError("simulated disk failure")
    monkeypatch.setattr(profiles.os, "replace", fail_replace)
    with pytest.raises(OSError): register(config, token, "影子验证")
    assert config.read_bytes() == original
    assert not list(tmp_path.glob(".client.json-*"))
    assert not config.with_name("client.json.lock").exists()


def test_concurrent_edit_fails_closed_and_single_legacy_default_still_works(tmp_path):
    config = tmp_path / "client.json"
    with profiles.edit(config) as data:
        data["servers"]["default"] = {"url": "https://ops.example.com"}
        with pytest.raises(ValueError, match="locked"):
            with profiles.edit(config): pass
    assert profiles.select(json.loads(config.read_text(encoding="utf-8"))["servers"])[0] == "default"


def test_exact_profile_routes_to_selected_real_gateway(live_gateway, tmp_path):
    # Existing fixture is a real HTTP gateway with its own isolated process/DB.
    command, client, _process = live_gateway
    result, labeled = command("server", "label", "default", "--school", "培新小学",
                              "--project", "电子书包", "--node", "影子")
    assert result.returncode == 0
    result, state = command("--school", "培新小学", "--project", "电子书包", "--node", "影子", "status")
    assert result.returncode == 0 and state["hostname"]
    assert state["target"]["profileId"] == labeled["profileId"]
    result, job = command("--server", "default", "run", "--command", "echo scoped", "--receipt", tmp_path / "live-receipt.json")
    assert result.returncode == 0 and job["ok"] and job["target"]["node"] == "影子"


def test_two_live_gateways_keep_jobs_and_files_separate(live_gateway, tmp_path):
    from agent_server_ops.config import load_config
    from agent_server_ops.gateway import initialize
    command, first, _ = live_gateway
    second_dir = tmp_path / "second"
    root = second_dir / "gateway"
    initialize(root)
    setup = (load_config(root / "gateway.json"), {}, root)
    with live_gateway_process(setup, second_dir) as (_, second, _process):
        result, added = command("server", "add", "shadow", "--school", "培新小学",
                                "--project", "电子书包", "--node", "影子",
                                "--url", second.base, "--token-file", root / "gateway.token")
        assert result.returncode == 0
        result, _ = command("run", "--command", "echo must-not-run")
        assert result.returncode == 1
        assert first.request("GET", "/ops/api/jobs")["jobs"] == []
        assert second.request("GET", "/ops/api/jobs")["jobs"] == []
        result, job = command("--server", "shadow", "run", "--command", "echo second-only",
                              "--receipt", tmp_path / "second-job.json")
        assert result.returncode == 0 and job["ok"]
        assert job["target"]["profileId"] == added["profileId"]
        assert first.request("GET", "/ops/api/jobs")["jobs"] == []
        assert [j["jobId"] for j in second.request("GET", "/ops/api/jobs")["jobs"]] == [job["jobId"]]
        source = tmp_path / "payload.txt"
        source.write_text("separate workspace", encoding="utf-8")
        result, _ = command("--node", "影子", "files", "upload", source, "selected.txt")
        assert result.returncode == 0
        assert (root / "workspace/selected.txt").read_bytes() == source.read_bytes()
        assert not (tmp_path / "gateway/workspace/selected.txt").exists()

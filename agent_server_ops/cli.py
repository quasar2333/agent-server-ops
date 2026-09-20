"""JSON CLI. Persist a submission receipt before touching the network."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import private_write
from . import profiles

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "timed_out"}
CONFIG_HOME = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config") / "server-ops"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_base(url: str, allow_http: bool = False) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Expected an HTTP(S) URL without credentials, query or fragment")
    try:
        loopback = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        loopback = parts.hostname.lower() == "localhost"
    if parts.scheme == "http" and not loopback and not allow_http:
        raise ValueError("Remote HTTP needs --allow-http on server add (private trusted network), or use HTTPS")
    return url.rstrip("/")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def save_json(path, value, *, exclusive=True):
    private_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2) + "\n", exclusive=exclusive)


class Client:
    def __init__(self, profile):
        self.base = validate_base(profile["url"], profile.get("allowHttp", False))
        if "tokenFile" in profile:
            self.token = Path(profile["tokenFile"]).expanduser().read_text(encoding="utf-8").strip()
        else:
            self.token = os.environ.get(profile.get("tokenEnv", "SERVER_OPS_TOKEN"), "").strip()
        if len(self.token) < 32:
            raise ValueError("Token is missing or shorter than 32 characters")
        context = ssl.create_default_context(cafile=profile.get("caFile"))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=context))

    def open(self, method, route, payload=None, *, key=None, headers=None, data=None):
        values = {"Authorization": "Bearer " + self.token, **(headers or {})}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            values["Content-Type"] = "application/json"
        if key:
            values["Idempotency-Key"] = key
        req = urllib.request.Request(self.base + route, data=data, method=method, headers=values)
        return self.opener.open(req, timeout=30)

    def request(self, method, route, payload=None, **kwargs):
        with self.open(method, route, payload, **kwargs) as response:
            return json.load(response)


def submit(client, route, payload, args, *, key=None):
    key = key or args.request_key or uuid.uuid4().hex
    receipt = Path(args.receipt).expanduser() if args.receipt else CONFIG_HOME / "receipts" / (uuid.uuid4().hex + ".json")
    receipt = receipt.resolve()
    value = {"url": client.base, "route": route, "payload": payload, "requestKey": key,
             "server": args.server, "created": time.time()}
    if getattr(args, "resolved_target", None):
        value["target"] = args.resolved_target
    # Never destroy a previous receipt. An uncertain request reuses this exact file.
    save_json(receipt, value)
    print(json.dumps({"receipt": str(receipt), "requestKey": key}), file=sys.stderr)
    result = client.request("POST", route, payload, key=key)
    result["receipt"] = str(receipt)
    return result


def wait_job(client, job_id, seconds=60, stream=False):
    deadline, cursor = time.monotonic() + seconds, 0
    while True:
        result = client.request("GET", "/ops/api/jobs/" + job_id)
        if stream:
            while True:
                logs = client.request("GET", f"/ops/api/jobs/{job_id}/logs?cursor={cursor}")
                for row in logs["rows"]:
                    print(row["text"], end="", file=sys.stderr, flush=True)
                previous, cursor = cursor, logs["nextCursor"]
                if cursor == previous or result["status"] not in TERMINAL:
                    break
        if result["status"] in TERMINAL:
            return result
        if time.monotonic() >= deadline:
            return {**result, "waitingExpired": True,
                    "message": "Still running remotely; continue with job wait. Do not resubmit."}
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def transfer_download(client, remote, local, overwrite):
    target = Path(local).expanduser().resolve()
    if target.exists() and not overwrite:
        raise ValueError("Local destination exists; use --overwrite explicitly")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=target.parent)
    digest, count = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as dest, client.open("GET", "/ops/api/files/content?" + urllib.parse.urlencode({"path": remote})) as source:
            while chunk := source.read(1024 * 1024):
                count += len(chunk)
                digest.update(chunk)
                dest.write(chunk)
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
        return {"ok": True, "path": str(target), "bytes": count, "sha256": digest.hexdigest()}
    finally:
        Path(temporary).unlink(missing_ok=True)


def parser():
    p = argparse.ArgumentParser(description="Operate servers through the Agent Server Ops gateway (JSON output)")
    p.add_argument("--config", type=Path, default=CONFIG_HOME / "client.json")
    p.add_argument("--server")
    p.add_argument("--school", dest="select_school")
    p.add_argument("--project", dest="select_project")
    p.add_argument("--node", dest="select_node")
    subs = p.add_subparsers(dest="action", required=True)
    skill = subs.add_parser("install-skill")
    skill.add_argument("--target", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills" / "server-ops")
    server = subs.add_parser("server").add_subparsers(dest="server_action", required=True)
    server.add_parser("list")
    add = server.add_parser("add")
    add.add_argument("name", nargs="?", help="stable legacy alias; omit to name by school/project/node")
    add.add_argument("--url", required=True)
    auth = add.add_mutually_exclusive_group(required=True)
    auth.add_argument("--token-file", type=Path)
    auth.add_argument("--token-env")
    add.add_argument("--ca-file", type=Path)
    add.add_argument("--allow-http", action="store_true")
    label = server.add_parser("label", help="label an existing profile, preserving its alias and credentials")
    label.add_argument("name")
    for sub in (add, label):
        sub.add_argument("--school", required=sub is label)
        sub.add_argument("--project", required=sub is label)
        sub.add_argument("--node", required=sub is label)
    for name in ("health", "status"):
        subs.add_parser(name)
    run = subs.add_parser("run")
    command = run.add_mutually_exclusive_group(required=True)
    command.add_argument("--command")
    command.add_argument("--command-file", type=Path)
    run.add_argument("--cwd")
    run.add_argument("--timeout", type=int, default=600)
    run.add_argument("--resource", action="append")
    run.add_argument("--retry-safe", action="store_true")
    operation = subs.add_parser("operation").add_subparsers(dest="op_action", required=True)
    operation.add_parser("list")
    op_run = operation.add_parser("run")
    op_run.add_argument("name")
    job = subs.add_parser("job").add_subparsers(dest="job_action", required=True)
    listing = job.add_parser("list")
    listing.add_argument("--before", type=float)
    listing.add_argument("--limit", type=int, default=50)
    for name in ("status", "logs", "wait", "cancel", "resume"):
        sub = job.add_parser(name)
        sub.add_argument("job_id")
        if name == "logs":
            sub.add_argument("--cursor", type=int, default=0)
        if name == "wait":
            sub.add_argument("--wait-seconds", type=int, default=60)
            sub.add_argument("--stream", action="store_true")
        if name == "resume":
            resume = sub
    reconcile = job.add_parser("reconcile")
    reconcile.add_argument("receipt_file", type=Path)
    for sub in (run, op_run, resume):
        sub.add_argument("--request-key")
        sub.add_argument("--receipt")
        sub.add_argument("--detach", action="store_true")
        sub.add_argument("--wait-seconds", type=int, default=60)
        sub.add_argument("--stream", action="store_true")
    files = subs.add_parser("files").add_subparsers(dest="file_action", required=True)
    listing = files.add_parser("list")
    listing.add_argument("path", nargs="?", default="")
    upload = files.add_parser("upload")
    upload.add_argument("local", type=Path)
    upload.add_argument("remote")
    upload.add_argument("--overwrite", action="store_true")
    download = files.add_parser("download")
    download.add_argument("remote")
    download.add_argument("local", type=Path)
    download.add_argument("--overwrite", action="store_true")
    return p


def execute(args):
    result = _execute(args)
    if getattr(args, "resolved_target", None):
        result = {**result, "target": args.resolved_target}
    return result


def _execute(args):
    if args.action == "install-skill":
        target = args.target.expanduser().resolve()
        shutil.copytree(Path(__file__).parent / "skills" / "server-ops", target)
        return {"ok": True, "skill": str(target), "message": "Existing skill directories are never overwritten"}
    config = read_json(args.config) if args.config.exists() else {"servers": {}}
    if args.action == "server":
        if args.server_action == "list":
            return {"servers": [profiles.target(k, v) for k, v in config["servers"].items()]}
        metadata = profiles.labels(args.school, args.project, args.node)
        name = args.name or profiles.display_name(metadata)
        if not name or not name.strip() or any(ord(c) < 32 for c in name):
            raise ValueError("Supply a profile name or complete school/project/node labels")
        if args.server_action == "label":
            with profiles.edit(args.config) as config:
                if name not in config["servers"]:
                    raise ValueError("Unknown server profile; run server list")
                profiles.check_unique(config["servers"], metadata, excluding=name)
                profile = config["servers"][name]
                profile.update(metadata)
                profile.setdefault("profileId", profiles.new_id())
            return {"ok": True, **profiles.target(name, profile)}
        profile = {"url": validate_base(args.url, args.allow_http), "allowHttp": args.allow_http}
        if args.token_file:
            token_path = args.token_file.expanduser().resolve()
            if not token_path.is_file():
                raise ValueError("Token file does not exist")
            profile["tokenFile"] = str(token_path)
        else:
            profile["tokenEnv"] = args.token_env
        if args.ca_file:
            profile["caFile"] = str(args.ca_file.expanduser().resolve())
        profile.update(metadata)
        profile["profileId"] = profiles.new_id()
        with profiles.edit(args.config) as config:
            if name in config["servers"]:
                raise ValueError("Server profile already exists; label it without replacing its credentials")
            profiles.check_unique(config["servers"], metadata)
            config["servers"][name] = profile
        return {"ok": True, **profiles.target(name, profile)}
    args.server, profile = profiles.select(config["servers"], name=args.server,
        school=args.select_school, project=args.select_project, node=args.select_node)
    args.resolved_target = profiles.target(args.server, profile)
    client = Client(profile)
    if args.action == "health":
        return client.request("GET", "/ops/healthz")
    if args.action == "status":
        return client.request("GET", "/ops/api/status")
    if args.action == "run":
        command = args.command_file.read_text(encoding="utf-8") if args.command_file else args.command
        payload = {"command": command, "cwd": args.cwd, "timeout": args.timeout,
                   "resources": args.resource or ["host"], "retrySafe": args.retry_safe}
        result = submit(client, "/ops/api/jobs", payload, args)
    elif args.action == "operation":
        if args.op_action == "list":
            return client.request("GET", "/ops/api/operations")
        result = submit(client, "/ops/api/operations/" + urllib.parse.quote(args.name, safe=""), {}, args)
    elif args.action == "job":
        if args.job_action == "list":
            query = {"limit": args.limit}
            if args.before is not None:
                query["before"] = args.before
            return client.request("GET", "/ops/api/jobs?" + urllib.parse.urlencode(query))
        if args.job_action == "reconcile":
            old = read_json(args.receipt_file)
            if old["url"] != client.base:
                raise ValueError("Receipt belongs to a different server URL")
            old_id = old.get("target", {}).get("profileId")
            if old_id and old_id != profile.get("profileId"):
                raise ValueError("Receipt belongs to a different server profile identity")
            if not old_id and old.get("server") not in (None, args.server):
                raise ValueError("Legacy receipt belongs to a different server alias")
            if not re_valid_receipt_route(old["route"]):
                raise ValueError("Invalid receipt route")
            return client.request("POST", old["route"], old["payload"], key=old["requestKey"])
        route = "/ops/api/jobs/" + urllib.parse.quote(args.job_id, safe="")
        if args.job_action == "status":
            return client.request("GET", route)
        if args.job_action == "logs":
            return client.request("GET", route + "/logs?cursor=" + str(args.cursor))
        if args.job_action == "wait":
            return wait_job(client, args.job_id, args.wait_seconds, args.stream)
        if args.job_action == "cancel":
            return client.request("POST", route + "/cancel", {})
        result = submit(client, route + "/resume", {}, args)
    elif args.action == "files":
        if args.file_action == "list":
            return client.request("GET", "/ops/api/files?" + urllib.parse.urlencode({"path": args.path}))
        if args.file_action == "download":
            return transfer_download(client, args.remote, args.local, args.overwrite)
        with args.local.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
            f.seek(0)
            query = urllib.parse.urlencode({"path": args.remote, "overwrite": str(args.overwrite).lower()})
            return client.request("PUT", "/ops/api/files/content?" + query, data=f,
                                  headers={"X-Content-SHA256": digest, "Content-Type": "application/octet-stream",
                                           "Content-Length": str(os.fstat(f.fileno()).st_size)})
    else:
        raise ValueError("Unknown action")
    if not args.detach:
        result = {**wait_job(client, result["jobId"], args.wait_seconds, args.stream), "receipt": result["receipt"]}
    return result


def re_valid_receipt_route(route):
    import re
    return bool(re.fullmatch(r"/ops/api/(jobs|jobs/[a-f0-9]{32}/resume|operations/[A-Za-z0-9_.-]+)", route))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        result = execute(args)
        emit(result)
        if result.get("status") in TERMINAL and not result.get("ok"):
            raise SystemExit(1)
        if result.get("waitingExpired"):
            raise SystemExit(2)
    except urllib.error.HTTPError as exc:
        # API errors never include the authorization header or token.
        emit({"ok": False, "httpStatus": exc.code, "error": exc.read(4096).decode("utf-8", "replace")})
        raise SystemExit(1)
    except (OSError, ValueError, KeyError) as exc:
        emit({"ok": False, "error": str(exc),
              "recovery": "If a submission receipt was printed, use job reconcile RECEIPT with the same server"})
        raise SystemExit(1)


if __name__ == "__main__":
    main()

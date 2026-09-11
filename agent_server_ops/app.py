from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import platform
import re
import socket
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .jobs import JobQueue
from .shell import command_argv


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1, max_length=65536)
    cwd: str | None = None
    timeout: int = Field(default=600, ge=1, le=14400)
    resources: list[str] = Field(default_factory=lambda: ["host"], min_length=1, max_length=16)
    retrySafe: bool = False


async def body(request: Request) -> dict:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 128 * 1024:
            raise HTTPException(413, "JSON request exceeds 128 KiB")
    try:
        value = json.loads(data or b"{}")
    except ValueError:
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "Expected a JSON object")
    return value


def request_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key", "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
        raise HTTPException(400, "Idempotency-Key must contain 8..128 letters, digits, _ . : or -")
    return key


def file_path(root: Path, relative: str) -> Path:
    if "\\" in relative or "\x00" in relative or ":" in relative or relative.startswith("/"):
        raise HTTPException(400, "Expected a relative path using forward slashes")
    result = (root / relative).resolve()
    if result != root and root not in result.parents:
        raise HTTPException(403, "Path escapes workspace")
    return result


def create_app(cfg: dict) -> FastAPI:
    workspace = Path(cfg["workspace"])

    @asynccontextmanager
    async def lifespan(app):
        workspace.mkdir(parents=True, exist_ok=True)
        app.state.jobs = JobQueue(Path(cfg["state_dir"]), command_argv, str(workspace))
        await app.state.jobs.start()
        try:
            yield
        finally:
            await app.state.jobs.close()

    app = FastAPI(title="Agent Server Ops", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path != "/ops/healthz":
            token = request.headers.get("authorization", "")
            digest = hashlib.sha256(token[7:].encode()).hexdigest() if token.startswith("Bearer ") else ""
            if not hmac.compare_digest(digest, cfg["token_sha256"]):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Job or operation not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/ops/healthz")
    async def health():
        return {"ok": True, "gateway": "agent-server-ops", "version": __version__}

    @app.get("/ops/api/status")
    async def status():
        def inspect():
            return {"ok": True, "version": __version__, "hostname": socket.gethostname(),
                    "platform": platform.platform(), "bootTime": psutil.boot_time(),
                    "time": time.time(), "cpuPercent": psutil.cpu_percent(interval=0.1),
                    "memory": dict(psutil.virtual_memory()._asdict()),
                    "disk": dict(psutil.disk_usage(workspace)._asdict()),
                    "workspace": str(workspace), "allowShell": cfg["allow_shell"],
                    "queues": app.state.jobs.limits}
        return await asyncio.to_thread(inspect)

    @app.get("/ops/api/operations")
    async def catalog():
        return {"operations": [{"name": name, "description": spec.get("description", ""),
                                "stages": [s["name"] for s in spec["stages"]],
                                "retrySafe": spec.get("retrySafe", False)}
                               for name, spec in cfg["operations"].items()]}

    @app.post("/ops/api/operations/{name}", status_code=202)
    async def operation(name: str, request: Request):
        key = request_key(request)
        if await body(request):
            raise HTTPException(400, "Operations take no client-side parameters")
        spec = cfg["operations"][name]
        return app.state.jobs.submit({"kind": spec.get("kind", "deploy"),
                                      "cwd": spec.get("cwd", str(workspace)),
                                      "timeout": spec.get("timeout", 1800),
                                      "resources": spec.get("resources", ["host"]),
                                      "retrySafe": spec.get("retrySafe", False),
                                      "stages": spec["stages"], "operation": name}, key)

    @app.post("/ops/api/jobs", status_code=202)
    async def run(request: Request):
        key = request_key(request)
        if not cfg["allow_shell"]:
            raise HTTPException(403, "Arbitrary shell disabled; use a configured operation")
        try:
            data = RunRequest.model_validate(await body(request))
        except ValidationError:
            raise HTTPException(422, "Invalid run request; check command, timeout, resources and field names") from None
        if any(not re.fullmatch(r"[\w:./-]{1,128}", r) for r in data.resources):
            raise HTTPException(422, "Invalid resource name")
        cwd = data.cwd or str(workspace)
        if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise HTTPException(400, "cwd must be an existing absolute server directory")
        return app.state.jobs.submit({"kind": "command", "cwd": str(Path(cwd).resolve()),
                                      "timeout": data.timeout, "resources": data.resources,
                                      "retrySafe": data.retrySafe,
                                      "stages": [{"name": "run", "command": data.command}]}, key)

    @app.get("/ops/api/jobs")
    async def jobs(limit: int = 50, before: float | None = None):
        return {"jobs": app.state.jobs.list(limit, before)}

    @app.get("/ops/api/jobs/{job_id}")
    async def job(job_id: str):
        return app.state.jobs.get(job_id)

    @app.get("/ops/api/jobs/{job_id}/logs")
    async def logs(job_id: str, cursor: int = 0, limit: int = 65536):
        return app.state.jobs.logs(job_id, cursor, limit)

    @app.post("/ops/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        return await app.state.jobs.cancel(job_id)

    @app.post("/ops/api/jobs/{job_id}/resume", status_code=202)
    async def resume(job_id: str, request: Request):
        return app.state.jobs.resume(job_id, request_key(request))

    @app.get("/ops/api/files")
    async def files(path: str = ""):
        target = file_path(workspace, path)
        if not target.is_dir():
            raise HTTPException(404, "Directory not found")
        items = []
        for p in sorted(target.iterdir(), key=lambda p: p.name):
            if len(items) >= 1000:
                raise HTTPException(413, "Directory has more than 1000 entries; use run to inspect")
            items.append({"name": p.name, "directory": p.is_dir(), "symlink": p.is_symlink(),
                          "size": p.lstat().st_size})
        return {"path": path, "entries": items}

    @app.get("/ops/api/files/content")
    async def download(path: str):
        target = file_path(workspace, path)
        if not target.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    @app.put("/ops/api/files/content")
    async def upload(request: Request, path: str, overwrite: bool = False):
        target = file_path(workspace, path)
        if target == workspace:
            raise HTTPException(400, "A file path is required")
        expected = request.headers.get("X-Content-SHA256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise HTTPException(400, "X-Content-SHA256 is required")
        if target.exists() and not overwrite:
            raise HTTPException(409, "File exists; explicitly request overwrite")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
        tmp = Path(temporary)
        total, digest = 0, hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > cfg["max_upload_bytes"]:
                        raise HTTPException(413, "Upload exceeds configured limit")
                    digest.update(chunk)
                    await asyncio.to_thread(f.write, chunk)
                f.flush()
                os.fsync(f.fileno())
            if not hmac.compare_digest(digest.hexdigest(), expected):
                raise HTTPException(422, "SHA256 mismatch")
            if overwrite:
                os.replace(tmp, target)
            else:
                try:
                    os.link(tmp, target)
                except FileExistsError:
                    raise HTTPException(409, "File already exists") from None
            return {"ok": True, "path": path, "bytes": total, "sha256": digest.hexdigest()}
        finally:
            tmp.unlink(missing_ok=True)

    return app

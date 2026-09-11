"""Durable, single-owner job queue. Business workers and databases are unrelated.

SQLite contains receipts; NDJSON log files use byte cursors. A client leaving
never cancels a job. On gateway restart orphaned process trees are terminated
before interrupted jobs can be retried, using PID creation times against reuse.
"""
from __future__ import annotations

import asyncio
import codecs
import json
import os
import sqlite3
import time
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import psutil

TERMINAL = {"succeeded", "failed", "interrupted", "cancelled", "timed_out"}
LIMITS = {"query": 16, "command": 4, "build": 1, "deploy": 1, "service": 1, "transfer": 2}


def system_shutting_down():
    if os.name != "nt":
        return False
    import ctypes
    return bool(ctypes.windll.user32.GetSystemMetrics(0x2000))


def kill_tree(pid: int, created: float) -> None:
    try:
        parent = psutil.Process(pid)
        if abs(parent.create_time() - created) > 0.01:
            return
        children = parent.children(recursive=True)
        for process in reversed(children):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
        psutil.wait_procs([parent, *children], timeout=5)
    except psutil.NoSuchProcess:
        pass


class JobQueue:
    def __init__(self, root: Path, argv, cwd: str, *, limits=None, max_pending=200):
        self.root, self.argv, self.cwd = Path(root), argv, cwd
        self.limits = {**LIMITS, **(limits or {})}
        if any(not isinstance(v, int) or not 1 <= v <= 32 for v in self.limits.values()):
            raise ValueError("Queue limits must be integers between 1 and 32")
        self.max_pending = max_pending
        self.active = {}
        self.stopping = False
        self.dispatch_paused = False
        self.interrupting = False
        self.wakeup = asyncio.Event()
        self.runner = None
        self.log_lock = threading.Lock()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "jobs.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def update(self, job_id, **fields):
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                       [*fields.values(), job_id])

    def get(self, job_id, *, internal=False):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["jobId"] = result.pop("id")
        spec = json.loads(result.pop("spec"))
        result["resources"] = spec["resources"]
        result["retrySafe"] = spec.get("retrySafe", False)
        result["ref"] = spec.get("ref")
        result["ok"] = result["status"] == "succeeded"
        result["accepted"] = True
        if internal:
            result["spec"] = spec
        return result

    def list(self, limit=50, before=None):
        with self.db() as db:
            rows = db.execute("SELECT id FROM jobs WHERE created < ? ORDER BY created DESC LIMIT ?",
                              (before or time.time() + 1, max(1, min(limit, 200)))).fetchall()
        return [self.get(r[0]) for r in rows]

    def submit(self, spec, key=None, parent=None):
        spec = {**spec, "resources": sorted(set(spec.get("resources", [])))}
        if spec.get("kind") not in self.limits:
            raise ValueError("Unknown queue")
        if not spec.get("stages") or not 1 <= float(spec.get("timeout", 600)) <= 14400:
            raise ValueError("Jobs require stages and a timeout of 1..14400 seconds")
        encoded = json.dumps(spec, sort_keys=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                row = db.execute("SELECT id,spec FROM jobs WHERE requestKey=?", (key,)).fetchone()
                if row:
                    if row["spec"] != encoded:
                        raise ValueError("Idempotency key already belongs to a different request")
                    return self.get(row["id"])
            count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= self.max_pending:
                raise ValueError("Job queue is full; retry submission later with the same key")
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,kind,status,stage,created,spec,requestKey,parent) VALUES(?,?,?,?,?,?,?,?)",
                       (job_id, spec["kind"], "queued", "accepted", time.time(), encoded, key, parent))
        self.wakeup.set()
        return self.get(job_id)

    def resume(self, job_id, key=None):
        old = self.get(job_id, internal=True)
        if old["status"] not in {"failed", "interrupted", "cancelled", "timed_out"}:
            raise ValueError("Only a stopped unsuccessful job can be resumed")
        if not old["retrySafe"]:
            raise ValueError("This command is not declared retry-safe; inspect its effects before a new submission")
        # All stages run again; retry-safe is an explicit operator declaration.
        return self.submit(old["spec"], key, parent=job_id)

    async def cancel(self, job_id):
        job = self.get(job_id)
        if job["status"] == "queued":
            self.update(job_id, status="cancelled", finished=time.time())
        elif job_id in self.active:
            self.active[job_id]["task"].cancel()
            await asyncio.gather(self.active[job_id]["task"], return_exceptions=True)
        return self.get(job_id)

    def log(self, job_id, stream, text):
        path = self.root / f"{job_id}.ndjson"
        # Bound disk use while continuing to drain process pipes. One explicit
        # marker is preserved; slow readers never control the output producer.
        size = path.stat().st_size if path.exists() else 0
        cap = 16 * 1024 * 1024
        if size > cap + 1024:
            return
        if size >= cap:
            text, stream = "Log size limit reached; remaining output discarded. " + " " * 1024, "warning"
        with self.log_lock, path.open("ab") as f:
            f.write((json.dumps({"time": time.time(), "stream": stream, "text": text}, ensure_ascii=False) + "\n").encode())

    def logs(self, job_id, cursor=0, limit=65536, tail=0):
        job = self.get(job_id)
        path = self.root / f"{job_id}.ndjson"
        rows, position = [], max(0, cursor)
        if path.exists():
            with path.open("rb") as f:
                if tail > 0:
                    f.seek(max(0, path.stat().st_size - min(tail, 1048576)))
                    if f.tell():
                        f.readline()
                    position = f.tell()
                if position > path.stat().st_size:
                    raise ValueError("Cursor exceeds log size")
                f.seek(position)
                while f.tell() - position < max(1024, min(limit, 1048576)):
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        f.seek(-len(line), 1)
                        break
                    try:
                        rows.append(json.loads(line))
                    except ValueError as exc:
                        raise ValueError("Cursor must be a returned nextCursor") from exc
                position = f.tell()
        return {"jobId": job_id, "rows": rows, "nextCursor": position,
                "status": job["status"], "done": job["status"] in TERMINAL and (not path.exists() or position >= path.stat().st_size)}

    async def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner = (self.root / "scheduler.lock").open("a+b")
        self.owner.seek(0)
        self.owner.write(b"0")
        self.owner.flush()
        self.owner.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                stage TEXT, created REAL, started REAL, finished REAL, exitCode INTEGER,
                error TEXT, spec TEXT NOT NULL, requestKey TEXT UNIQUE, parent TEXT,
                pid INTEGER, processCreated REAL)""")
            orphaned = db.execute("SELECT id,pid,processCreated FROM jobs WHERE status='running'").fetchall()
        for row in orphaned:
            if row["pid"] and row["processCreated"]:
                await asyncio.to_thread(kill_tree, row["pid"], row["processCreated"])
            self.update(row["id"], status="interrupted", finished=time.time(), error="Gateway or server restarted")
            self.log(row["id"], "system", "Interrupted during gateway/server restart; inspect or resume explicitly.")
        self.runner = asyncio.create_task(self.schedule())

    async def close(self):
        self.stopping = True
        self.wakeup.set()
        if self.runner:
            await self.runner
        tasks = [v["task"] for v in self.active.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.owner.close()

    async def quiesce(self):
        """Checkpoint running jobs and retain the backlog before host shutdown."""
        self.dispatch_paused = True
        self.interrupting = True
        tasks = [v["task"] for v in self.active.values() if not v["task"].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def schedule(self):
        while not self.stopping:
            if system_shutting_down() and not self.dispatch_paused:
                await self.quiesce()
            if self.dispatch_paused:
                await asyncio.sleep(0.2)
                continue
            self.wakeup.clear()
            self.active = {k: v for k, v in self.active.items() if not v["task"].done()}
            used = Counter(v["kind"] for v in self.active.values())
            locked = {r for v in self.active.values() for r in v["resources"]}
            with self.db() as db:
                pending = db.execute("SELECT id,spec,created FROM jobs WHERE status='queued' ORDER BY created").fetchall()
            pending.sort(key=lambda row: (-json.loads(row["spec"]).get("priority", 0), row["created"]))
            waiting_resources = set()
            for row in pending:
                spec = json.loads(row["spec"])
                resources, kind = set(spec["resources"]), spec["kind"]
                if resources & (locked | waiting_resources) or used[kind] >= self.limits[kind]:
                    waiting_resources.update(resources)
                    continue
                self.update(row["id"], status="running", started=time.time())
                self.active[row["id"]] = {"kind": kind, "resources": resources,
                                          "task": asyncio.create_task(self.execute(row["id"], spec))}
                locked.update(resources)
                used[kind] += 1
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def execute(self, job_id, spec):
        process = None
        created = None
        readers = []
        try:
            async with asyncio.timeout(spec.get("timeout", 600)):
                for stage in spec["stages"]:
                    self.update(job_id, stage=stage["name"])
                    self.log(job_id, "stage", f"Starting {stage['name']}")
                    options = {"start_new_session": True} if os.name != "nt" else {}
                    process = await asyncio.create_subprocess_exec(
                        *self.argv(stage["command"]), cwd=spec.get("cwd", self.cwd),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "SERVER_OPS_JOB_ID": job_id}, **options)
                    try:
                        created = psutil.Process(process.pid).create_time()
                    except psutil.NoSuchProcess:
                        created = None
                    self.update(job_id, pid=process.pid, processCreated=created)

                    async def drain(pipe, stream):
                        decoder = codecs.getincrementaldecoder("utf-8")("replace")
                        while data := await pipe.read(16384):
                            text = decoder.decode(data)
                            await asyncio.to_thread(self.log, job_id, stream, text)
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            await asyncio.to_thread(self.log, job_id, stream, tail)

                    readers = [asyncio.create_task(drain(process.stdout, "stdout")),
                               asyncio.create_task(drain(process.stderr, "stderr"))]
                    # asyncio Process.wait() can wait for pipe EOF even after the
                    # command exits. Detached services may inherit those handles.
                    # Reap the command independently, then allow buffered output
                    # to drain without waiting for the lifetime of its services.
                    while process.returncode is None:
                        await asyncio.sleep(0.05)
                    _, pending_readers = await asyncio.wait(readers, timeout=2)
                    if pending_readers:
                        self.log(job_id, "system", "Command exited; detached processes still hold log pipes. Closing job log handles.")
                    for reader in pending_readers:
                        reader.cancel()
                    outcomes = await asyncio.gather(*readers, return_exceptions=True)
                    # StreamReader has no public close API. Closing the exited
                    # subprocess transport releases our read handles, not services.
                    process._transport.close()
                    readers = []
                    for outcome in outcomes:
                        if isinstance(outcome, Exception):
                            raise outcome
                    code = process.returncode
                    self.update(job_id, exitCode=code, pid=None, processCreated=None)
                    if code:
                        raise RuntimeError(f"Stage {stage['name']} exited with code {code}")
                    self.log(job_id, "stage", f"Passed {stage['name']}")
            self.update(job_id, status="succeeded", stage="verified", finished=time.time(), exitCode=0)
        except (Exception, asyncio.CancelledError) as exc:
            if process and process.returncode is None and created:
                await asyncio.to_thread(kill_tree, process.pid, created)
            status = "timed_out" if isinstance(exc, TimeoutError) else (
                ("interrupted" if self.stopping or self.interrupting else "cancelled") if isinstance(exc, asyncio.CancelledError) else "failed")
            error = str(exc) or status
            self.update(job_id, status=status, finished=time.time(), error=error, pid=None, processCreated=None)
            self.log(job_id, "system", error)
        finally:
            for reader in readers:
                reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            if process and process.returncode is not None:
                process._transport.close()
            self.wakeup.set()

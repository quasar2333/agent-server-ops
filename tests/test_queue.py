import asyncio
import sys
from pathlib import Path

import pytest

from agent_server_ops.jobs import JobQueue
from agent_server_ops.shell import command_argv


def test_second_scheduler_cannot_own_same_state(tmp_path):
    async def run():
        first = JobQueue(tmp_path, command_argv, str(tmp_path))
        second = JobQueue(tmp_path, command_argv, str(tmp_path))
        await first.start()
        try:
            with pytest.raises(OSError):
                await second.start()
        finally:
            if hasattr(second, "owner"):
                second.owner.close()
            await first.close()
    asyncio.run(run())


def test_persisted_backlog_survives_restart(tmp_path):
    async def run():
        queue = JobQueue(tmp_path, command_argv, str(tmp_path))
        await queue.start()
        queue.dispatch_paused = True
        job = queue.submit({"kind": "command", "resources": ["host"],
                            "stages": [{"name": "execute", "command": [sys.executable, "-c", "print('backlog')"]}]})
        await queue.close()
        queue = JobQueue(tmp_path, command_argv, str(tmp_path))
        await queue.start()
        try:
            for _ in range(200):
                result = queue.get(job["jobId"])
                if result["status"] == "succeeded":
                    break
                await asyncio.sleep(.025)
            assert result["ok"]
        finally:
            await queue.close()
    asyncio.run(run())

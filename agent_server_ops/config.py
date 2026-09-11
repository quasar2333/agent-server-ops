from __future__ import annotations

import json
import os
import re
from pathlib import Path


def private_write(path: Path, text: str, *, exclusive: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def load_config(path: Path) -> dict:
    path = path.expanduser().resolve()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    digest = cfg.get("token_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("gateway config requires token_sha256; run init first")
    for key in ("workspace", "state_dir"):
        value = Path(cfg[key]).expanduser()
        cfg[key] = str((path.parent / value).resolve() if not value.is_absolute() else value.resolve())
    workspace, state = Path(cfg["workspace"]), Path(cfg["state_dir"])
    if workspace == state or workspace in state.parents or state in workspace.parents:
        raise ValueError("workspace and state_dir must be separate, non-nested directories")
    if path == workspace or workspace in path.parents:
        raise ValueError("gateway config must be outside the file-transfer workspace")
    if not 1 <= int(cfg.get("port", 9876)) <= 65535:
        raise ValueError("port must be 1..65535")
    cfg["config_path"] = str(path)
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 9876)
    cfg.setdefault("operations", {})
    cfg.setdefault("max_upload_bytes", 64 * 1024 * 1024)
    cfg.setdefault("allow_shell", True)
    if not isinstance(cfg["allow_shell"], bool) or not 1 <= cfg["max_upload_bytes"] <= 1024**3:
        raise ValueError("allow_shell must be boolean; max_upload_bytes must be 1..1073741824")
    if not isinstance(cfg["operations"], dict):
        raise ValueError("operations must be an object")
    for name, spec in cfg["operations"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or not isinstance(spec, dict):
            raise ValueError("Invalid operation name or definition")
        if spec.get("kind", "deploy") not in {"query", "command", "build", "deploy", "service", "transfer"}:
            raise ValueError(f"Invalid queue for {name}")
        if not isinstance(spec.get("timeout", 1800), int) or not 1 <= spec.get("timeout", 1800) <= 14400:
            raise ValueError(f"Invalid timeout for {name}")
        resources = spec.get("resources", ["host"])
        if not isinstance(resources, list) or not resources or not all(isinstance(r, str) and re.fullmatch(r"[\w:./-]{1,128}", r) for r in resources):
            raise ValueError(f"Invalid resources for {name}")
        if "cwd" in spec and (not Path(spec["cwd"]).is_absolute() or not Path(spec["cwd"]).is_dir()):
            raise ValueError(f"Operation {name} cwd must be an existing absolute directory")
        stages = spec.get("stages")
        if not isinstance(stages, list) or not 1 <= len(stages) <= 32:
            raise ValueError(f"Operation {name} needs 1..32 stages")
        for stage in stages:
            if not isinstance(stage, dict) or not isinstance(stage.get("name"), str) or not stage["name"]:
                raise ValueError(f"Invalid stage for {name}")
            command = stage.get("command")
            if not ((isinstance(command, str) and command.strip()) or
                    (isinstance(command, list) and command and all(isinstance(v, str) and v for v in command))):
                raise ValueError(f"Invalid stage command for {name}")
    return cfg

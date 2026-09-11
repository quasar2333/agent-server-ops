from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path

from .config import load_config, private_write


def initialize(root: Path, host="127.0.0.1", port=9876):
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = root / "gateway.json"
    token_path = root / "gateway.token"
    if config.exists() or token_path.exists():
        raise ValueError("Already initialized; existing credentials and config were preserved")
    token = secrets.token_urlsafe(48)
    private_write(token_path, token + "\n")
    value = {"host": host, "port": port, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
             "workspace": str(root / "workspace"), "state_dir": str(root / "state"),
             "allow_shell": True, "max_upload_bytes": 67108864,
             "operations": {"python-check": {"description": "Check the gateway Python runtime",
                              "kind": "query", "retrySafe": True, "timeout": 30,
                              "resources": ["diagnostics"],
                              "stages": [{"name": "verify", "command": [os.sys.executable, "--version"]}]}}}
    private_write(config, json.dumps(value, indent=2) + "\n")
    (root / "workspace").mkdir(exist_ok=True)
    (root / "state").mkdir(exist_ok=True, mode=0o700)
    return {"ok": True, "config": str(config), "tokenFile": str(token_path),
            "message": "Token saved to private file; it is never printed. Copy it securely to the Agent machine."}


def main():
    for stream in (os.sys.stdout, os.sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Initialize or run the independent operations gateway")
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--root", type=Path, required=True)
    init.add_argument("--host", default="127.0.0.1")
    init.add_argument("--port", type=int, default=9876)
    check = sub.add_parser("check-config")
    check.add_argument("--config", type=Path, required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--certfile")
    serve.add_argument("--keyfile")
    args = parser.parse_args()
    try:
        if args.action == "init":
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be 1..65535")
            result = initialize(args.root, args.host, args.port)
        else:
            cfg = load_config(args.config)
            if args.action == "check-config":
                result = {"ok": True, "operations": list(cfg["operations"])}
            else:
                import uvicorn
                from .app import create_app
                if bool(args.certfile) != bool(args.keyfile):
                    raise ValueError("certfile and keyfile must be supplied together")
                uvicorn.run(create_app(cfg), host=args.host or cfg["host"], port=args.port or cfg["port"],
                            workers=1, access_log=False, proxy_headers=False, server_header=False,
                            ssl_certfile=args.certfile, ssl_keyfile=args.keyfile,
                            timeout_graceful_shutdown=15)
                return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

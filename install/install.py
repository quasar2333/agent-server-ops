#!/usr/bin/env python3
"""Install from a checked-out release into an independent virtualenv."""
from __future__ import annotations

import argparse
import base64
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def call(argv):
    subprocess.run([str(v) for v in argv], check=True)


def systemd_unit(gateway: Path, config: Path, user: str):
    def quoted(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    return ("[Unit]\nDescription=Agent Server Ops gateway\nAfter=network-online.target\nWants=network-online.target\n\n"
            "[Service]\nType=simple\nUser=" + user + "\nExecStart=" + quoted(gateway) +
            " serve --config " + quoted(config) + "\nRestart=on-failure\nRestartSec=3\n"
            "KillMode=control-group\nTimeoutStopSec=30\nUMask=0077\n\n[Install]\nWantedBy=multi-user.target\n")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9876)
    p.add_argument("--service", action="store_true", help="Register and start the native startup service")
    args = p.parse_args()
    root = args.root.expanduser().resolve()
    if any(c in str(root) for c in "\n\r\x00"):
        p.error("Invalid root path")
    if (root / "gateway.json").exists() or (root / "gateway.token").exists():
        p.error("Already installed; follow the README upgrade procedure to preserve state and credentials")
    if args.service and sys.platform.startswith("linux") and os.geteuid() != 0:
        p.error("System-wide Linux service installation requires sudo; omit --service for foreground use")
    if args.service and sys.platform == "darwin" and os.geteuid() == 0:
        p.error("macOS uses a per-user LaunchAgent; rerun without sudo")
    if args.service and not (sys.platform.startswith("linux") or sys.platform == "darwin" or os.name == "nt"):
        p.error("Native service registration supports Linux, Windows and macOS")
    if args.service and os.name == "nt":
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            p.error("Windows startup task installation requires an Administrator terminal")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    call([sys.executable, "-m", "venv", root / ".venv"])
    bin_dir = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    gateway = bin_dir / ("server-ops-gateway.exe" if os.name == "nt" else "server-ops-gateway")
    source = Path(__file__).resolve().parents[1]
    call([python, "-m", "pip", "install", str(source)])
    call([gateway, "init", "--root", root, "--host", args.bind, "--port", args.port])
    config = root / "gateway.json"
    call([gateway, "check-config", "--config", config])
    argv = [str(gateway), "serve", "--config", str(config)]
    if sys.platform.startswith("linux"):
        import pwd
        unit = root / "agent-server-ops.service"
        unit.write_text(systemd_unit(gateway, config, pwd.getpwuid(os.geteuid()).pw_name), encoding="utf-8")
        if args.service:
            target = Path("/etc/systemd/system/agent-server-ops.service")
            with target.open("x", encoding="utf-8") as f:
                f.write(unit.read_text())
            call(["systemctl", "daemon-reload"])
            call(["systemctl", "enable", "--now", "agent-server-ops.service"])
            call(["systemctl", "is-active", "agent-server-ops.service"])
    elif sys.platform == "darwin":
        value = {"Label": "io.github.quasar2333.agent-server-ops", "ProgramArguments": argv,
                 "RunAtLoad": True, "KeepAlive": True,
                 "StandardOutPath": str(root / "gateway.stdout.log"),
                 "StandardErrorPath": str(root / "gateway.stderr.log")}
        generated = root / "io.github.quasar2333.agent-server-ops.plist"
        generated.write_bytes(plistlib.dumps(value))
        if args.service:
            dest = Path.home() / "Library/LaunchAgents" / generated.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("xb") as f:
                f.write(generated.read_bytes())
            call(["launchctl", "bootstrap", f"gui/{os.getuid()}", dest])
    elif os.name == "nt":
        def ps(value):
            return "'" + str(value).replace("'", "''") + "'"
        script = (
            "$ErrorActionPreference='Stop'\n"
            "if (Get-ScheduledTask -TaskName AgentServerOps -ErrorAction SilentlyContinue) { throw 'Task already exists' }\n"
            "$a=New-ScheduledTaskAction -Execute " + ps(gateway) + " -Argument " + ps(subprocess.list2cmdline(argv[1:])) + "\n"
            "$t=New-ScheduledTaskTrigger -AtStartup\n"
            "$p=New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest\n"
            "$s=New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew\n"
            "Register-ScheduledTask -TaskName AgentServerOps -Action $a -Trigger $t -Principal $p -Settings $s | Out-Null\n"
            "Start-ScheduledTask -TaskName AgentServerOps\n"
        )
        (root / "register-task.ps1").write_text(script, encoding="utf-8-sig")
        # POSIX chmod does not establish Windows ACLs. Restrict the installation explicitly.
        identity = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
        import csv
        sid = next(csv.reader([identity.strip()]))[1]
        call(["icacls", root, "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F",
              "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"])
        if args.service:
            encoded = base64.b64encode(script.encode("utf-16-le")).decode()
            call(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])
    if args.service:
        host = "127.0.0.1" if args.bind == "0.0.0.0" else ("::1" if args.bind == "::" else args.bind)
        host = f"[{host}]" if ":" in host else host
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(20):
            try:
                with opener.open(f"http://{host}:{args.port}/ops/healthz", timeout=2) as response:
                    if json.load(response).get("ok"):
                        break
            except OSError:
                pass
            time.sleep(.5)
        else:
            raise RuntimeError("Service registered but gateway health check failed; inspect the native service logs")
    print(json.dumps({"ok": True, "root": str(root), "serviceRegistered": args.service,
                      "foreground": argv, "tokenFile": str(root / "gateway.token")}, indent=2))


if __name__ == "__main__":
    main()

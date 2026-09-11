from __future__ import annotations

import base64
import os
import shutil


def command_argv(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        return command
    if os.name != "nt":
        return ["/bin/sh", "-c", "set -eu\n" + command]
    script = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
        "$OutputEncoding=[Console]::OutputEncoding;"
        "$ProgressPreference='SilentlyContinue';"
        "try{&{" + command + "};if($null -ne $LASTEXITCODE){exit $LASTEXITCODE}}"
        "catch{[Console]::Error.WriteLine(($_|Out-String));exit 1}"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [shutil.which("pwsh") or "powershell.exe", "-NoLogo", "-NoProfile",
            "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", encoded]

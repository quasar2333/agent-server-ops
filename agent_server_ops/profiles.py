"""Local server identity and selection; no credentials or remote writes here."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unicodedata
import uuid


def labels(school=None, project=None, node=None):
    values = (school, project, node)
    if not any(value is not None for value in values):
        return {}
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("School, project and node must be supplied together")
    result = {}
    for key, value in zip(("school", "project", "node"), values):
        value = unicodedata.normalize("NFC", value.strip())
        if len(value) > 96 or any(unicodedata.category(c).startswith("C") or c in "/\\" for c in value):
            raise ValueError("Labels must be 1..96 characters without controls or path separators")
        result[key] = value
    return result


def display_name(profile):
    return "/".join(profile[key] for key in ("school", "project", "node")) if all(
        profile.get(key) for key in ("school", "project", "node")) else None


def target(name, profile):
    return {"name": name, "displayName": display_name(profile) or name, "url": profile["url"],
            **{key: profile[key] for key in ("school", "project", "node", "profileId") if key in profile}}


def check_unique(servers, metadata, *, excluding=None):
    if metadata and any(name != excluding and all(value.get(k) == v for k, v in metadata.items())
                        for name, value in servers.items()):
        raise ValueError("This school/project/node is already registered")


def select(servers, *, name=None, school=None, project=None, node=None):
    filters = {k: unicodedata.normalize("NFC", v.strip())
               for k, v in (("school", school), ("project", project), ("node", node)) if v is not None}
    if name is not None:
        if filters:
            raise ValueError("Use --server or school/project/node selectors, not both")
        if name not in servers:
            raise ValueError("Unknown server profile; run server list")
        return name, servers[name]
    if filters:
        matches = [(k, v) for k, v in servers.items() if all(v.get(f) == value for f, value in filters.items())]
        if len(matches) != 1:
            raise ValueError(f"Server selector matched {len(matches)} targets; choose an exact --server or --node")
        return matches[0]
    # Preserve the original single-default setup. Once multiple servers exist,
    # every operation requires an explicit selection, including read-only work.
    if len(servers) == 1 and "default" in servers:
        return "default", servers["default"]
    raise ValueError("Select an explicit --server or school/project/node; no implicit multi-server default")


@contextmanager
def edit(path):
    """Serialize local edits and replace the whole JSON atomically.

    A stale lock after a killed editor fails closed for manual inspection.
    It is never guessed safe to erase a concurrent editor's lock.
    """
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Edit the actual config file, not a symbolic link")
    lock = path.with_name(path.name + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("Client config is locked by another editor; inspect before removing a stale lock") from exc
    temporary = None
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"servers": {}}
        if not isinstance(config.get("servers"), dict):
            raise ValueError("Client config requires a servers object")
        yield config
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def new_id():
    return uuid.uuid4().hex

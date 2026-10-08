"""Storage and expiry for Codex-owned recovery data."""

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
import uuid

OWNER = "codex-setup"
RETENTION = {"trash": 7 * 86400, "git": 30 * 86400}


def state_root():
    configured = os.environ.get("CODEX_SETUP_STATE")
    default = Path.home() / "AppData/Local/CodexSetup" if os.name == "nt" else Path.home() / ".local/share/codex-setup"
    return Path(configured or default).expanduser().absolute()


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def checked_root(root=None):
    root = Path(root or state_root()).absolute()
    if root == Path(root.anchor) or root == Path.home():
        raise ValueError("Recovery storage must be a dedicated directory")
    for parent in (root, *root.parents):
        if parent.exists() and is_link(parent):
            raise ValueError(f"Recovery storage crosses a link: {parent}")
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def locked(root=None):
    root = checked_root(root)
    lock_path = root / ".lock"
    if lock_path.exists() and is_link(lock_path):
        raise ValueError("Linked guard lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield root
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def new_entry(root, category, **fields):
    directory = root / category
    if directory.exists() and is_link(directory):
        raise ValueError(f"Linked recovery category: {directory}")
    entry = directory / uuid.uuid4().hex
    entry.mkdir(parents=True)
    record = {"owner": OWNER, "category": category, "id": entry.name, "created": time.time(), "complete": False, **fields}
    write_record(entry, record)
    return entry, record


def write_record(entry, record):
    pending = entry / "manifest.pending"
    pending.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(entry / "manifest.json")


def remove_tree(path):
    # Junctions and symlinks are removed as entries, never traversed.
    if is_link(path):
        if path.is_dir() and os.name == "nt":
            path.rmdir()
        else:
            path.unlink()
    elif path.is_dir():
        for child in path.iterdir():
            remove_tree(child)
        path.rmdir()
    else:
        path.unlink()


def cleanup(root=None, now=None):
    now = time.time() if now is None else now
    removed, errors = [], []
    with locked(root) as root:
        for category, lifetime in RETENTION.items():
            directory = root / category
            if not directory.exists() or is_link(directory):
                continue
            for entry in directory.iterdir():
                if not entry.is_dir() or is_link(entry) or len(entry.name) != 32:
                    continue
                try:
                    int(entry.name, 16)
                    manifest = entry / "manifest.json"
                    if not os.path.lexists(manifest):
                        continue
                    if is_link(manifest):
                        continue
                    record = json.loads(manifest.read_text(encoding="utf-8"))
                    if (record.get("owner"), record.get("category"), record.get("id")) != (OWNER, category, entry.name):
                        continue
                    created = record["created"]
                    if not isinstance(created, (int, float)) or not math.isfinite(created):
                        raise ValueError("Invalid recovery timestamp")
                    if now - created < lifetime:
                        continue
                    for ref in record.get("refs", []):
                        expected = f"refs/codex-setup/{entry.name}/"
                        if not ref["name"].startswith(expected):
                            raise ValueError("Unexpected backup ref")
                        repo = Path(record["repo"])
                        if repo.is_dir():
                            existing = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", ref["name"]], capture_output=True)
                            if existing.returncode:
                                continue
                            if existing.stdout.decode().strip() != ref["sha"]:
                                raise RuntimeError("Backup ref changed")
                            result = subprocess.run(["git", "-C", str(repo), "update-ref", "-d", ref["name"], ref["sha"]], capture_output=True)
                            if result.returncode:
                                raise RuntimeError("Backup ref changed or could not be removed")
                    # Recheck the absolute path immediately before recursive deletion.
                    if entry.resolve() != root.resolve() / category / entry.name:
                        raise ValueError("Recovery entry escaped its storage directory")
                    remove_tree(entry)
                    removed.append(f"{category}/{entry.name}")
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                    errors.append(f"{entry}: {exc}")
    return removed, errors


def recycle(paths, root=None, missing_ok=False, copy_only=False):
    results = []
    with locked(root) as root:
        targets = [Path(os.path.abspath(Path(p).expanduser())) for p in paths]
        for path in targets:
            if path == Path(path.anchor) or path == Path.home() or path == root or path in root.parents or root in path.parents:
                raise ValueError(f"Protected recycle target: {path}")
            if ".git" in path.parts or ".codex" in path.parts or ".agents" in path.parts:
                raise ValueError(f"Protected configuration target: {path}")
            if not os.path.lexists(path) and not missing_ok:
                raise FileNotFoundError(path)
            if copy_only and path.is_dir() and not is_link(path):
                raise ValueError("Patch recovery accepts files only")
        for path in targets:
            if not os.path.lexists(path):
                continue
            entry, record = new_entry(root, "trash", original=str(path), method="copy-before-patch" if copy_only else "move")
            payload = entry / "content"
            # The source link itself is moved. Its destination is never resolved.
            if copy_only:
                shutil.copy2(path, payload, follow_symlinks=False)
            elif is_link(path) and path.is_dir():
                os.rename(path, payload)
            else:
                shutil.move(str(path), str(payload))
            record["complete"] = True
            write_record(entry, record)
            results.append(str(entry))
    return results


def restore_trash(entry, root=None):
    with locked(root) as root:
        entry = Path(entry).absolute()
        if entry.parent != root / "trash" or is_link(entry):
            raise ValueError("Restore expects an entry in this guard's trash directory")
        manifest = entry / "manifest.json"
        if is_link(manifest):
            raise ValueError("Linked recycle manifest")
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if record.get("owner") != OWNER or record.get("category") != "trash" or record.get("id") != entry.name:
            raise ValueError("Unrecognized recycle entry")
        original = Path(record["original"])
        if os.path.lexists(original):
            raise FileExistsError(f"Restore would overwrite: {original}")
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry / "content"), str(original))
        record["restored"] = time.time()
        write_record(entry, record)
        return original

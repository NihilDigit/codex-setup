"""Install shared Codex settings while preserving application runtime entries."""

import argparse
import difflib
import json
import os
from pathlib import Path
import shutil
import shlex
import stat
import subprocess
import sys
import tomllib
from storage import checked_root, locked, new_entry, state_root, write_record

RUNTIME_KEYS = ("notify", "marketplaces", "plugins", "shell_environment_policy")


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(key, ensure_ascii=False)} = {toml_value(item)}" for key, item in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def toml_tables(data, prefix=()):
    lines = []
    if prefix:
        lines.append("[" + ".".join(json.dumps(key, ensure_ascii=False) for key in prefix) + "]")
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{json.dumps(key, ensure_ascii=False)} = {toml_value(value)}")
    lines.append("")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.extend(toml_tables(value, (*prefix, key)))
    return lines


def runtime_config(target):
    path = target / "config.toml"
    if linked(path):
        raise ValueError(f"Configuration crosses a link: {path}")
    existing = tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    retained = {key: existing[key] for key in RUNTIME_KEYS if key in existing}
    if "node_repl" in existing.get("mcp_servers", {}):
        retained["mcp_servers"] = {"node_repl": existing["mcp_servers"]["node_repl"]}
    return retained


def linked(path):
    for parent in (path, *path.parents):
        if os.path.lexists(parent):
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                return parent
    return None


def cache_directories():
    home = Path.home()
    candidates = [home / name for name in (
        ".gradle/caches", ".gradle/wrapper/dists", ".gradle/daemon", ".m2/repository",
        ".cache/uv", ".cache/pip", ".npm", "Library/Caches/pip", "Library/Caches/uv",
        "AppData/Local/uv/cache", "AppData/Local/pip/Cache", "AppData/Local/npm-cache",
    )]
    for name in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "npm_config_cache"):
        if os.environ.get(name):
            candidates.append(Path(os.environ[name]).expanduser())
    gradle = os.environ.get("GRADLE_USER_HOME")
    if gradle:
        candidates.extend(Path(gradle) / name for name in ("caches", "wrapper/dists", "daemon"))
    cargo = Path(os.environ.get("CARGO_HOME") or home / ".cargo")
    candidates.extend(cargo / name for name in ("registry", "git"))
    return sorted({path.resolve() for path in candidates if path.is_dir()}, key=str)


def installation_files(target):
    source = Path(__file__).resolve().parent.parent
    hooks_dir = target / "hooks/codex-setup"
    profile = (source / "config.toml").read_text(encoding="utf-8")
    runtime = runtime_config(target)
    if "notify" in runtime:
        profile = 'notify = ' + json.dumps(runtime.pop("notify"), ensure_ascii=False) + '\n' + profile
    paths = [*cache_directories(), state_root().resolve()]
    profile += '\n[permissions.codex-setup.filesystem]\n'
    for path in dict.fromkeys(paths):
        profile += f'{json.dumps(str(path), ensure_ascii=False)} = "write"\n'
    if os.name == "nt":
        profile += '\n[windows]\nsandbox = "elevated"\n'
    if runtime:
        profile += "\n" + "\n".join(toml_tables(runtime))
    tomllib.loads(profile)
    files = {"AGENTS.md": (source / "AGENTS.md").read_bytes(), "config.toml": profile.encode("utf-8")}
    for path in (source / "Hooks").rglob("*"):
        if path.is_file() and path.suffix in (".py", ".ps1") and path.name not in ("setup.py", "scheduled-cleanup.ps1"):
            files["hooks/codex-setup/" + path.relative_to(source / "Hooks").as_posix()] = path.read_bytes()
    data = {"hooks": {"PreToolUse": []}}
    groups = data["hooks"]["PreToolUse"]
    for matcher, name, status in (("Bash|apply_patch", "recycle_delete.py", "codex-setup: recycle"),
                                  ("Bash", "git_backup.py", "codex-setup: git backup")):
        argv = [sys.executable, "-X", "utf8", str(hooks_dir / "runner.py"), name]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        groups.append({"matcher": matcher, "hooks": [{"type": "command", "command": command,
                       "timeout": 180, "statusMessage": status}]})
    files["hooks.json"] = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, help="Codex home; default CODEX_HOME or ~/.codex")
    parser.add_argument("--apply", action="store_true", help="Install; default previews changes")
    parser.add_argument("--replace", action="store_true", help="Replace differing files after saving previous contents")
    parser.add_argument("--schedule-cleanup", action="store_true", help="Register daily Windows cleanup")
    args = parser.parse_args()
    if args.schedule_cleanup and (not args.apply or os.name != "nt"):
        parser.error("--schedule-cleanup requires Windows and --apply")
    target = Path(args.target or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()
    if (target / "AGENTS.override.md").exists():
        parser.error("AGENTS.override.md takes precedence; resolve it before installing")
    obsolete = [path for path in (target / "rules/default.rules", target / "codex-setup.config.toml") if path.exists()]
    for path in obsolete:
        if linked(path):
            parser.error(f"Refusing to retire a linked path: {path}")
        print(f"Retire after saving in recovery directory: {path}")
    changes = []
    for name, new in installation_files(target).items():
        destination = target / name
        if linked(destination):
            parser.error(f"Refusing to write through a link: {destination}")
        old = destination.read_bytes() if destination.exists() else None
        if old == new:
            continue
        if old and not args.replace:
            parser.error(f"Existing file differs: {destination}; preview with --replace")
        if name == "config.toml":
            print(f"Replace: {destination}; preserve runtime settings without displaying their values.")
            print((Path(__file__).parent.parent / "config.toml").read_text(encoding="utf-8"))
            print("Writable directories:", *tomllib.loads(new.decode("utf-8"))["permissions"]["codex-setup"]["filesystem"], sep="\n")
        else:
            print("".join(difflib.unified_diff((old or b"").decode("utf-8-sig").splitlines(True),
                        new.decode("utf-8").splitlines(True), fromfile=str(destination), tofile=name)), end="")
        changes.append((destination, new, old))
    print("Replace shared settings; preserve application runtime entries, authentication, plugins and session data.")
    print(f"Recovery data: {state_root()}")
    if not args.apply:
        print("Preview only; add --apply to install.")
        return
    checked_root()
    target.mkdir(parents=True, exist_ok=True)
    for path in obsolete:
        with locked() as root:
            entry, record = new_entry(root, "trash", original=str(path), method="retire-previous-config")
            shutil.move(str(path), str(entry / "content"))
            record["complete"] = True
            write_record(entry, record)
    for destination, new, old in changes:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if old is not None:
            with locked() as root:
                entry, record = new_entry(root, "trash", original=str(destination), method="copy-before-install")
                shutil.copy2(destination, entry / "content", follow_symlinks=False)
                record["complete"] = True
                write_record(entry, record)
        destination.write_bytes(new)
    if args.schedule_cleanup:
        subprocess.run(["pwsh", "-NoProfile", "-File", str(target / "hooks/codex-setup/register-cleanup.ps1"),
                        "-Python", sys.executable, "-StateDirectory", str(state_root())], check=True)
    print("Installed as the default configuration. Review hooks with /hooks, then start a new session.")


if __name__ == "__main__":
    main()

"""Snapshot repository state before destructive Git operations."""

import os
from pathlib import Path
import re
import subprocess
import tarfile

from common import context, deny, parse_commands, run_hook
from storage import cleanup, locked, new_entry, write_record

RISKY = {"restore", "reset", "checkout", "switch", "clean", "rebase", "merge", "cherry-pick", "revert", "am"}
GLOBAL_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
CHDIR = {"cd", "chdir", "set-location", "sl"}


def git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return result


def git_request(args, cwd):
    index, repo = 0, cwd
    while index < len(args):
        arg = args[index]
        if arg == "-C":
            index += 1
            if index >= len(args) or any(c in args[index] for c in ("$", "`")):
                raise ValueError("git -C requires a literal path")
            repo = Path(os.path.abspath(repo / args[index]))
        elif arg.startswith("-"):
            if arg == "--git-dir" or arg == "--work-tree" or arg.startswith(("--git-dir=", "--work-tree=")):
                raise ValueError("Use git -C for operations requiring a snapshot")
            if arg in GLOBAL_VALUE:
                index += 1
        else:
            return arg, args[index + 1:], repo
        index += 1
    return None, [], repo


def dangerous(subcommand, args):
    if subcommand == "stash":
        return any(a in {"drop", "clear", "pop"} for a in args)
    if subcommand == "branch":
        return any(a in {"-d", "-D", "--delete", "-f", "--force"} for a in args)
    if subcommand == "tag":
        return any(a in {"-d", "--delete", "-f", "--force"} for a in args)
    if subcommand in {"checkout", "switch"} and any(a in {"--help", "-h"} for a in args):
        return False
    if subcommand == "clean" and any(a in {"-n", "--dry-run"} or re.fullmatch(r"-[a-z]*n[a-z]*", a) for a in args):
        return False
    return subcommand in RISKY


def backup(repo, operation, args, session_id):
    repo = Path(git(repo, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()
    head = git(repo, "rev-parse", "--verify", "HEAD", check=False)
    with locked() as root:
        entry, record = new_entry(root, "git", repo=str(repo), operation=operation, arguments=args,
                                  session_id=session_id, refs=[])
        if head.returncode == 0:
            record["head"] = head.stdout.decode().strip()
        index = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip()
        if Path(index).is_file():
            (entry / "index").write_bytes(Path(index).read_bytes())
        (entry / "staged.patch").write_bytes(git(repo, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv").stdout)
        (entry / "worktree.patch").write_bytes(git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv").stdout)
        files = git(repo, "ls-files", "-o", "--exclude-standard", "-z").stdout.split(b"\0")
        include_ignored = operation == "clean" and any(a in {"-x", "-X"} or re.fullmatch(r"-[a-zA-Z]*[xX][a-zA-Z]*", a) for a in args)
        if include_ignored:
            files += git(repo, "ls-files", "-o", "-i", "--exclude-standard", "-z").stdout.split(b"\0")
        with tarfile.open(entry / "untracked.tar", "w", dereference=False) as archive:
            for raw in sorted(set(files) - {b""}):
                name = os.fsdecode(raw)
                source = repo / name
                if source.parent.resolve().is_relative_to(root.resolve()):
                    continue
                if not source.parent.resolve().is_relative_to(repo):
                    raise ValueError(f"Backup path crosses a link: {source}")
                archive.add(source, arcname=name, recursive=False)
        refs = []
        if head.returncode == 0:
            refs.append(("head", record["head"]))
        if operation == "stash":
            listing = git(repo, "stash", "list", "--format=%H").stdout.decode().splitlines()
            refs.extend((f"stash-{i}", sha) for i, sha in enumerate(listing))
        if operation in {"branch", "tag"}:
            namespace = "refs/heads" if operation == "branch" else "refs/tags"
            listing = git(repo, "for-each-ref", "--format=%(objectname)", namespace).stdout.decode().splitlines()
            refs.extend((f"{operation}-{i}", sha) for i, sha in enumerate(listing))
        for label, sha in refs:
            ref = {"name": f"refs/codex-setup/{entry.name}/{label}", "sha": sha}
            record["refs"].append(ref)
            write_record(entry, record)
            git(repo, "update-ref", ref["name"], sha)
        if (entry / "staged.patch").read_bytes() != git(repo, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv").stdout:
            raise RuntimeError("Index changed during backup; retry the Git operation separately")
        if (entry / "worktree.patch").read_bytes() != git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv").stdout:
            raise RuntimeError("Working tree changed during backup; retry the Git operation separately")
        record["complete"] = True
        write_record(entry, record)
        return entry


def check(payload):
    if payload.get("tool_name") not in {"Bash", "PowerShell", "exec_command", "shell"}:
        return None
    raw = payload.get("tool_input", {}).get("command", "")
    commands = parse_commands(raw)
    current = Path(payload.get("cwd", os.getcwd())).absolute()
    requests, preceding = [], []
    directory_error = None
    for command in commands:
        name = command.name.lower()
        if name in CHDIR:
            if len(command.args) != 1 or any(c in command.args[0] for c in ("$", "`")):
                directory_error = "危险 Git 操作的工作目录须明确；请先切换目录，再单独执行命令。"
            else:
                current = Path(os.path.abspath(current / command.args[0]))
        elif name == "git":
            operation, args, repo = git_request(command.args, current)
            if dangerous(operation, args):
                if directory_error:
                    return deny(directory_error)
                if preceding or command.prefixes:
                    return deny("请将危险 Git 操作单独执行，以便备份其执行前的实际状态。")
                requests.append((repo, operation, args))
        else:
            preceding.append(command)
    if not requests:
        return None
    if preceding or len(requests) > 1:
        return deny("请将危险 Git 操作单独执行，以便备份其执行前的实际状态。")
    backups = [backup(repo, operation, args, payload.get("session_id")) for repo, operation, args in requests]
    _, errors = cleanup()
    message = "Git 操作前已备份，保留 30 天：" + ", ".join(map(str, backups))
    if errors:
        message += "。过期清理未完成：" + "; ".join(errors)
    return context(message)


if __name__ == "__main__":
    run_hook(check)

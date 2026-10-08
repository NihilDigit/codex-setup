"""Route supported deletion requests to the Agent recycle directory."""

import argparse
import base64
import glob
import json
import os
from pathlib import Path
import re
import sys

from common import context, deny, parse_commands, run_hook
from storage import cleanup, recycle, restore_trash

DELETERS = {"rm", "rmdir", "unlink", "shred", "srm", "wipe", "remove-item", "del", "erase", "rd"}
INDIRECT = re.compile(r"\b(?:unlink|rmtree|remove|rmdir)\s*\(|\.Delete\s*\(|::\s*Delete\w*\s*\(|\bfind\b[^\n]*-delete", re.I)


def expanded_paths(args, cwd, shell):
    paths, missing_ok, literal = [], False, False
    options_done = False
    for arg in args:
        lower = arg.lower()
        if not options_done and arg == "--":
            options_done = True
            continue
        if not options_done and arg.startswith("-"):
            if shell == "powershell":
                if lower in ("-recurse", "-force", "-path", "-literalpath"):
                    literal |= lower == "-literalpath"
                    continue
            elif lower in ("--force", "--recursive", "--dir") or re.fullmatch(r"-[rRfdiIv]+", arg):
                missing_ok |= lower == "--force" or "f" in arg[1:]
                continue
            raise ValueError(f"Unsupported deletion option: {arg}")
        if any(char in arg for char in ("$", "`", "\n", "\r")) or re.match(r"^(?:alias|env|function|variable|hk[a-z]{2}):", arg, re.I):
            raise ValueError("Deletion requires literal filesystem paths")
        if os.name == "nt" and shell == "posix" and re.match(r"^/[a-zA-Z]/", arg):
            arg = arg[1] + ":" + arg[2:]
        path = os.path.abspath(os.path.join(cwd, os.path.expanduser(arg)))
        matches = [path] if literal or not glob.has_magic(path) else glob.glob(path)
        if not matches and not missing_ok:
            raise FileNotFoundError(path)
        paths.extend(matches)
    if not paths and not missing_ok:
        raise ValueError("Deletion requires explicit paths")
    return list(dict.fromkeys(paths)), missing_ok


def check(payload):
    tool = payload.get("tool_name", "")
    raw = payload.get("tool_input", {}).get("command", "")
    cwd = Path(payload.get("cwd", os.getcwd())).absolute()
    if tool in ("apply_patch", "Edit", "Write"):
        deletions = re.findall(r"^\*\*\* Delete File: (.+)$", raw, re.M)
        if not deletions:
            return None
        targets = [Path(os.path.abspath(cwd / name)) for name in deletions]
        if any(not path.parent.resolve().is_relative_to(cwd.resolve()) for path in targets):
            return deny("补丁删除仅处理当前工作目录中的明确路径；其他路径请单独提交回收命令。")
        entries = recycle(targets, copy_only=True)
        return context("补丁删除前的原内容已保存到 Agent 回收目录，保留 7 天：" + ", ".join(entries))
    if tool not in ("Bash", "PowerShell", "exec_command", "shell"):
        return None
    commands = parse_commands(raw)
    deletes = [cmd for cmd in commands if cmd.name.lower() in DELETERS]
    for command in commands:
        if command.name.lower() == "cmd" and re.search(r"(?:^|\s)(?:del|erase|rd|rmdir)(?:\s|$)", " ".join(command.args), re.I):
            return deny("cmd 内的删除已拦截。请单独提交明确路径的回收请求。")
    if INDIRECT.search(raw):
        if commands and all(cmd.name.lower() in {"echo", "write-output", "printf", "adb", "ssh", "docker"} for cmd in commands):
            return None
        return deny("脚本内直接删除已拦截。请使用明确路径的 rm 或 Remove-Item，hook 会转为回收操作。")
    if not deletes:
        return None
    if len(commands) != 1 or deletes[0].prefixes or re.search(r"[<>]", raw) or re.search(r"\$\(|@\(|@\{", raw):
        return deny("请将删除单独执行，并提供明确路径；hook 将自动转为回收操作。")
    paths, missing_ok = expanded_paths(deletes[0].args, str(cwd), deletes[0].shell)
    if deletes[0].shell == "posix":
        recursive = any(arg in {"--recursive"} or re.fullmatch(r"-[rRfdiIv]*[rR][rRfdiIv]*", arg) for arg in deletes[0].args)
        for path in paths:
            target = Path(path)
            if target.is_dir() and not target.is_symlink() and not recursive:
                if deletes[0].name.lower() != "rmdir" or any(target.iterdir()):
                    return deny("目录回收需要明确的递归删除请求。")
    if deletes[0].name.lower() in {"shred", "srm", "wipe"}:
        return deny("文件擦除已拦截。请提交明确路径的回收请求。")
    request = base64.b64encode(json.dumps({"paths": paths, "missing_ok": missing_ok}).encode()).decode()
    script = base64.b64encode(str(Path(__file__).resolve()).encode()).decode()
    # Base64 keeps shell interpolation out of both the script path and arguments.
    command = f'python -X utf8 -c "import base64,pathlib,runpy,sys;p=base64.b64decode(\'{script}\').decode();sys.path.insert(0,str(pathlib.Path(p).parent));runpy.run_path(p,run_name=\'__main__\')" recycle --encoded {request}'
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"command": command}, "additionalContext": "本次删除已改为回收操作，内容保留 7 天。"}}


def main():
    if len(sys.argv) == 1:
        run_hook(check)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    trash = sub.add_parser("recycle")
    trash.add_argument("paths", nargs="*")
    trash.add_argument("--encoded")
    restore = sub.add_parser("restore")
    restore.add_argument("entry", type=Path)
    args = parser.parse_args()
    if args.action == "restore":
        print(restore_trash(args.entry))
    else:
        request = json.loads(base64.b64decode(args.encoded)) if args.encoded else {"paths": args.paths}
        for entry in recycle(request["paths"], missing_ok=request.get("missing_ok", False)):
            print(f"Recycled: {entry}")
    removed, errors = cleanup()
    for error in errors:
        print(error, file=sys.stderr)


if __name__ == "__main__":
    main()

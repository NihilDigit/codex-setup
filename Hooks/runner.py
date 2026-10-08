"""Convert recoverable hook process failures into explicit denials."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


def execute(name, payload, timeout):
    path = Path(__file__).parent / name
    try:
        result = subprocess.run([sys.executable, "-X", "utf8", str(path)], input=payload, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip() or f"exit {result.returncode}")
        if not result.stdout.strip():
            return None
        data = json.loads(result.stdout)
        hook = data.get("hookSpecificOutput", {})
        if hook.get("hookEventName") != "PreToolUse":
            raise ValueError("Unexpected hook response")
        if hook.get("permissionDecision") not in (None, "allow", "deny"):
            raise ValueError("Unsupported hook decision")
        if "updatedInput" in hook and (hook.get("permissionDecision") != "allow" or not isinstance(hook["updatedInput"].get("command"), str)):
            raise ValueError("Invalid rewritten command")
        return data
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": f"Hook 检查未完成，已拦截本次操作：{str(exc)[:800]}"}}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=("recycle_delete.py", "git_backup.py"))
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    response = execute(args.check, sys.stdin.buffer.read(), args.timeout)
    if response:
        json.dump(response, sys.stdout, ensure_ascii=False)

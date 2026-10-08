"""Expire only this guard's recycle entries and Git snapshots."""

import json
from storage import cleanup

if __name__ == "__main__":
    removed, errors = cleanup()
    print(json.dumps({"removed": removed, "errors": errors}, ensure_ascii=False))
    raise SystemExit(bool(errors))

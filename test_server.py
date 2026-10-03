from server import pick_dirs

sizes = {"/h": 100, "/h/a": 90, "/h/a/game1": 60, "/h/a/game2": 30, "/h/b": 10}
got = [p for _, p in pick_dirs(sizes)]
# /h and /h/a are skipped (one child holds most of them), games show individually
assert got == ["/h/a/game1", "/h/a/game2", "/h/b"], got
print("ok")

from datetime import datetime, timedelta, timezone
from server import why_delete

now = datetime(2026, 10, 3, tzinfo=timezone.utc)
snap = lambda type, cleanup, days: {"type": type, "cleanup": cleanup, "date": now - timedelta(days=days)}
assert why_delete(snap("single", "", 1), now).startswith("Manual")
assert why_delete(snap("single", "timeline", 45), now) == "45 days old"
assert why_delete(snap("pre", "number", 2), now) is None
print("ok")

import os, tempfile
from server import walk

with tempfile.TemporaryDirectory() as d:
    os.makedirs(f"{d}/a/b")
    open(f"{d}/a/b/f", "wb").write(b"x" * 300)
    open(f"{d}/top", "wb").write(b"x" * 50)
    os.link(f"{d}/a/b/f", f"{d}/a/hard")  # counted once
    os.symlink(f"{d}/a", f"{d}/link")  # skipped
    total, sizes, files = walk(d, depth=1, big=100)
    assert total == 350, total
    assert sizes == {d: 350, f"{d}/a": 300}, sizes  # a/b rolls up into a
    assert len(files) == 1 and files[0][0] == 300, files
print("ok")

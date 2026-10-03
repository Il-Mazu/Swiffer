#!/usr/bin/env python3
"""Swiffer: local disk-space dashboard. Run: python3 server.py"""
import base64, json, os, shutil, subprocess, threading, time, webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WIN = os.name == "nt"
HOME = Path.home()
HERE = Path(__file__).parent
HISTORY = HERE / "history.json"
PORT = int(os.environ.get("SWIFFER_PORT", 8765))
SYSTEM = os.environ.get("SystemDrive", "C:") + "\\" if WIN else "/"

# id -> (label, what it is, paths measured, admin?, how to clean)
# user actions: list of dirs whose *contents* get deleted, or a command
# admin actions: a shell snippet (PowerShell on Windows), all selected ones run under a single prompt
if WIN:
    LOCAL = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData/Local"))
    app_caches = [*LOCAL.glob("Google/Chrome/User Data/*/Cache"), *LOCAL.glob("Microsoft/Edge/User Data/*/Cache"),
                  *LOCAL.glob("Mozilla/Firefox/Profiles/*/cache2"), LOCAL / "NVIDIA/DXCache", LOCAL / "NVIDIA/GLCache",
                  LOCAL / "D3DSCache", LOCAL / "Microsoft/Windows/INetCache"]
    JUNK = {
        "winupdate": ("Windows Update downloads", "Update files that are already installed. Windows downloads them again if it needs them.",
                      [Path(os.environ.get("SystemRoot", r"C:\Windows"), "SoftwareDistribution/Download")], True,
                      r'Stop-Service wuauserv -Force; Remove-Item "$env:SystemRoot\SoftwareDistribution\Download\*" '
                      r'-Recurse -Force -ErrorAction SilentlyContinue; Start-Service wuauserv'),
        "temp": ("Temporary files", "Files apps left in your temp folder. Ones still in use are skipped.",
                 [LOCAL / "Temp"], False, [LOCAL / "Temp"]),
        "apps": ("App caches", "Chrome, Edge, Firefox, Windows web, NVIDIA and DirectX shader caches. Apps rebuild them as needed.",
                 app_caches, False, app_caches),
        "crashdumps": ("Crash dumps", "Memory dumps from crashed programs.", [LOCAL / "CrashDumps"], False, [LOCAL / "CrashDumps"]),
        "playwright": ("Playwright browsers", "Test browsers. Run `npx playwright install` to get them back.",
                       [LOCAL / "ms-playwright"], False, [LOCAL / "ms-playwright"]),
        "npm": ("npm cache", "Downloaded package tarballs.", [LOCAL / "npm-cache/_cacache"], False, ["npm", "cache", "clean", "--force"]),
        # you can only read your own folder in there, so this measures just your bin
        "trash": ("Recycle Bin", "Files you already deleted.", [Path(SYSTEM, "$Recycle.Bin")], False,
                  ["powershell", "-NoProfile", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"]),
    }
else:
    PM = next((p for p in ("pacman", "apt-get", "dnf") if shutil.which(p)), None)
    JUNK = {
        "pacman": {
            "pkgcache": ("Pacman package cache", "Every downloaded package file. Pacman downloads them again if it ever needs them.",
                         ["/var/cache/pacman/pkg"], True, "rm -rf /var/cache/pacman/pkg/*"),
            "orphans": ("Orphan packages", "Dependencies nothing needs anymore.", [], True,
                        'o=$(pacman -Qdtq) && [ -n "$o" ] && pacman -Rns --noconfirm $o || true'),
        },
        "apt-get": {
            "pkgcache": ("APT package cache", "Downloaded package files. APT downloads them again if it ever needs them.",
                         ["/var/cache/apt/archives"], True, "apt-get clean"),
            "orphans": ("Unused packages", "Dependencies nothing needs anymore.", [], True,
                        "DEBIAN_FRONTEND=noninteractive apt-get -y autoremove"),
        },
        "dnf": {
            "pkgcache": ("DNF package cache", "Downloaded packages and repository data. DNF downloads them again when needed.",
                         ["/var/cache/dnf", "/var/cache/libdnf5"], True, "dnf clean all"),
        },
    }.get(PM, {})
    JUNK.update({
        "aur": ("AUR build caches", "Paru and yay build folders. Rebuilt the next time you install from the AUR.",
                [HOME / ".cache/paru", HOME / ".cache/yay"], False, [HOME / ".cache/paru", HOME / ".cache/yay"]),
        "apps": ("App caches", "Firefox, Electron, NVIDIA shader, Bazel and ProtonPlus caches. Apps rebuild them as needed.",
                 [HOME / ".cache" / d for d in ("mozilla", "electron", "nvidia", "bazel", "ProtonPlus")], False,
                 [HOME / ".cache" / d for d in ("mozilla", "electron", "nvidia", "bazel", "ProtonPlus")]),
        "playwright": ("Playwright browsers", "Test browsers. Run `npx playwright install` to get them back.",
                       [HOME / ".cache/ms-playwright"], False, [HOME / ".cache/ms-playwright"]),
        "npm": ("npm cache", "Downloaded package tarballs.", [HOME / ".npm/_cacache"], False, ["npm", "cache", "clean", "--force"]),
        "trash": ("Trash", "Files you already deleted.", [HOME / ".local/share/Trash"], False,
                  [HOME / ".local/share/Trash" / d for d in ("files", "info", "expunged")]),
        "coredumps": ("Crash dumps", "Memory dumps from crashed programs.", ["/var/lib/systemd/coredump"], True,
                      "rm -f /var/lib/systemd/coredump/*"),
        "journal": ("System logs", "Trims the journal to 50 MB.", ["/var/log/journal"], True,
                    "journalctl --vacuum-size=50M"),
        "flatpak": ("Unused Flatpak runtimes", "Runtimes no installed app uses.", [], False,
                    ["flatpak", "uninstall", "--unused", "-y", "--noninteractive"]),
    })

ONLINE_ONLY = 0x400000  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS: a OneDrive file that isn't downloaded


def walk(top, depth=6, big=200e6):
    """du -x in Python, so it runs on Windows too. Returns total bytes under `top`,
    {folder: bytes} down to `depth` levels (deeper folders roll up into their parent)
    and [(bytes, path)] for files over `big`. Paths use / on every OS.
    Skips symlinks, junctions and other filesystems."""
    sizes, files, seen = {}, [], set()
    try:
        dev = os.stat(top).st_dev
    except OSError:
        return 0, sizes, files

    def rec(d, lvl):
        total = 0
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink() or getattr(e, "is_junction", lambda: False)():
                            continue
                        st = e.stat()
                        if e.is_dir():
                            if WIN or st.st_dev == dev:  # DirEntry has no st_dev on Windows
                                total += rec(e.path, lvl + 1)
                        elif st.st_nlink > 1 and (st.st_dev, st.st_ino) in seen:  # hard link, counted once like du
                            continue
                        elif not getattr(st, "st_file_attributes", 0) & ONLINE_ONLY:
                            if st.st_nlink > 1:
                                seen.add((st.st_dev, st.st_ino))
                            total += st.st_size
                            if st.st_size > big:
                                files.append((st.st_size, Path(e.path).as_posix()))
                    except OSError:
                        pass
        except OSError:
            pass
        if lvl <= depth:
            sizes[Path(d).as_posix()] = total
        return total

    return rec(str(top), 0), sizes, files


def du(paths):
    return sum(walk(p, depth=-1)[0] for p in paths)


def proc(cmd):
    # which() finds npm.cmd and friends on Windows
    return subprocess.run([shutil.which(cmd[0]) or cmd[0], *cmd[1:]], capture_output=True, text=True)


def run(cmd):
    r = proc(cmd)
    return r.returncode, (r.stdout + r.stderr).strip()


def stdout(cmd):
    return proc(cmd).stdout


def orphans():
    if WIN:
        return []
    if PM == "pacman":
        return stdout(["pacman", "-Qdtq"]).split()
    if PM == "apt-get":
        return [l.split()[1] for l in stdout(["apt-get", "-s", "autoremove"]).splitlines() if l.startswith("Remv ")]
    return []


def disk():
    u = shutil.disk_usage(SYSTEM)
    point = {"t": int(time.time()), "free": u.free}
    hist = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    # ponytail: one point per 5 min, capped at 2000 (~a week of constant use)
    if not hist or point["t"] - hist[-1]["t"] > 300 or abs(point["free"] - hist[-1]["free"]) > 100e6:
        hist = (hist + [point])[-2000:]
        HISTORY.write_text(json.dumps(hist))
    return {"total": u.total, "used": u.used, "free": u.free, "history": hist}


def junk():
    items = []
    for k, (label, desc, paths, root, _) in JUNK.items():
        if k == "orphans":
            names = orphans()
            if not names:
                continue
            size = sum(map(int, stdout(["expac", "%m", *names]).split())) if shutil.which("expac") else None
            desc = f"{desc} {', '.join(names)}"
        elif k == "flatpak":
            if not shutil.which("flatpak"):
                continue
            size = None
        else:
            size = du(paths)
            # journalctl can't trim the active log files, so small journals have nothing to free
            if size < (200e6 if k == "journal" else 1e6):
                continue
        items.append({"id": k, "label": label, "desc": desc, "size": size, "root": root})
    return items


def pick_dirs(sizes, top=15):
    """From {path: bytes}, pick the folders that actually hold the data:
    skip a folder when one child has over a quarter of it (show the child instead),
    and skip anything inside a folder already picked."""
    biggest_child = {}
    for p, s in sizes.items():
        parent = os.path.dirname(p)
        biggest_child[parent] = max(biggest_child.get(parent, 0), s)
    picked = []
    for p, s in sorted(sizes.items(), key=lambda x: -x[1]):
        if biggest_child.get(p, 0) > s / 4 or any(p.startswith(q + "/") for q, _ in picked):
            continue
        picked.append((p, s))
        if len(picked) == top:
            break
    return [(s, p) for p, s in picked]


def heavy():
    # ponytail: depth 6 reaches ~/.local/share/Steam/steamapps/common/<game>; deeper folders roll up into their parent
    _, sizes, files = walk(HOME)
    home = HOME.as_posix()
    fmt = lambda xs: [{"size": s, "path": p.replace(home, "~", 1)} for s, p in xs[:15]]
    return {"dirs": fmt(pick_dirs(sizes)), "files": fmt(sorted(files, reverse=True))}


def run_admin(snippets):
    """Run shell snippets as admin under one prompt. Returns (exit code, output, prompt cancelled?)."""
    if WIN:
        # the elevated window's output can't be captured, so only the exit code comes back
        inner = base64.b64encode("; ".join(snippets).encode("utf-16-le")).decode()
        code, out = run(["powershell", "-NoProfile", "-Command",
                         f"$p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
                         f"-ArgumentList '-NoProfile','-EncodedCommand','{inner}'; exit $p.ExitCode"])
        return code, out, "cancel" in out.lower()
    code, out = run(["pkexec", "sh", "-c", "; ".join(snippets)])
    return code, out, code in (126, 127)  # password prompt dismissed or denied


def clean(ids):
    log, admin = [], []
    for k in ids:
        if k not in JUNK:
            continue
        label, _, _, root, how = JUNK[k]
        if root:
            admin.append(how if WIN else f'echo "== {label}"; {how}')
        elif isinstance(how[0], Path):
            for d in how:
                for c in (d.iterdir() if d.is_dir() else []):
                    if c.is_dir() and not c.is_symlink():
                        shutil.rmtree(c, ignore_errors=True)
                    else:
                        try:
                            c.unlink()
                        except OSError:  # still in use (Windows) or already gone
                            pass
            log.append(f"== {label}: emptied")
        else:
            code, out = run(how)
            log.append(f"== {label}: {'done' if code == 0 else 'failed'}\n{out}".strip())
    if admin:
        code, out, cancelled = run_admin(admin)
        log.append((out or "== Admin actions: done") if code == 0 else
                   f"Admin actions stopped (exit {code}{', prompt cancelled' if cancelled else ''}).\n{out}")
    return {"log": "\n".join(log)}


def why_delete(snap, now):
    """Reason a snapshot is worth deleting, or None."""
    if snap["type"] == "single" and not snap["cleanup"]:
        return "Manual snapshot, never deleted automatically"
    if now - snap["date"] > timedelta(days=30):
        return f"{(now - snap['date']).days} days old"
    return None


def snapshots():
    # info.xml is readable by the wheel group, so listing needs no password
    try:
        dirs = sorted(Path("/.snapshots").iterdir(), key=lambda d: int(d.name) if d.name.isdigit() else 0)
    except FileNotFoundError:  # no Snapper here: the page hides the section
        return {"unavailable": True}
    except OSError as e:
        return {"error": f"Couldn't read /.snapshots ({e.strerror})."}
    now, snaps = datetime.now(timezone.utc), []
    for d in dirs:
        try:
            x = ET.parse(d / "info.xml").getroot()
        except (OSError, ET.ParseError):
            continue
        snap = {"number": int(x.findtext("num")), "type": x.findtext("type"), "cleanup": x.findtext("cleanup") or "",
                "description": x.findtext("description") or "",
                "date": datetime.fromisoformat(x.findtext("date")).replace(tzinfo=timezone.utc)}  # snapper stores UTC
        snap["why"] = why_delete(snap, now)
        snap["date"] = snap["date"].isoformat()
        snaps.append(snap)
    return {"snapshots": snaps}


def delete_snapshots(nums):
    nums = [str(int(n)) for n in nums if int(n) > 0]
    code, out = run(["pkexec", "snapper", "delete", *nums]) if nums else (0, "")
    return {"log": out or (f"Deleted snapshots {', '.join(nums)}." if code == 0 else f"Delete failed (exit {code}).")}


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _local(self):
        # blocks DNS-rebinding: only answer requests addressed to localhost
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local():
            return self._send({"error": "forbidden"}, code=403)
        routes = {"/api/disk": disk, "/api/junk": junk, "/api/heavy": heavy, "/api/snapshots": snapshots}
        if self.path in routes:
            return self._send(routes[self.path]())
        if self.path == "/":
            return self._send((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        self._send({"error": "not found"}, code=404)

    def do_POST(self):
        # custom header forces a CORS preflight, so other websites can't trigger cleanups
        if not self._local() or self.headers.get("X-Swiffer") != "1":
            return self._send({"error": "forbidden"}, code=403)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/clean":
            return self._send(clean(body.get("ids", [])))
        if self.path == "/api/snapshots/delete":
            return self._send(delete_snapshots(body.get("numbers", [])))
        self._send({"error": "not found"}, code=404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:  # already running: just open it
        webbrowser.open(url)
        raise SystemExit
    print(f"Swiffer running at {url}  (Ctrl+C to stop)")
    if not os.environ.get("SWIFFER_NO_BROWSER"):
        threading.Timer(0.5, webbrowser.open, [url]).start()
    server.serve_forever()

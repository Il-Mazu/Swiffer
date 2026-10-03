# Swiffer

![Swiffer screenshot](docs/screenshot.png)

Local dashboard for disk space: free space, what takes the most room, and one-click cleanup.
Runs on Linux and Windows. Only Python 3's standard library is needed.

    python3 server.py      # Linux
    py server.py           # Windows

It opens http://localhost:8765 in your browser. The server only listens on localhost.

## What it can clean

Items that aren't on your system are hidden.

**Linux**
- Package cache: pacman (Arch, CachyOS, Manjaro…), APT (Debian, Ubuntu, Mint…) or DNF (Fedora…)
- Unused packages: pacman and APT
- AUR build caches (paru, yay), app caches, Playwright browsers, npm cache, Trash
- Crash dumps and system logs (systemd distros)
- Unused Flatpak runtimes
- Snapper snapshots on btrfs (the section only appears if `/.snapshots` exists)

Items marked "needs admin" go through `pkexec`, so you get the normal password prompt.

**Windows**
- Windows Update downloads (needs admin, one UAC prompt)
- Temp folder, browser and shader caches, crash dumps, Playwright browsers, npm cache, Recycle Bin

`history.json` (git-ignored) stores one free-space reading per visit for the chart.

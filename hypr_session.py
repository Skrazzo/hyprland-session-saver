#!/usr/bin/env python3
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
import importlib.util

SESSION_FILE = Path.home() / ".config" / "hypr" / "session.json"
CONFIG_FILE = Path.home() / ".config" / "hypr-session" / "config.py"
TERMINALS = {"alacritty", "kitty"}
KNOWN_SHELLS = {"fish", "bash", "zsh", "sh", "dash", "tcsh", "ksh", "csh", "nu", "elvish", "xonsh"}

try:
    # Try to import user-specific config
    spec = importlib.util.spec_from_file_location("config", CONFIG_FILE)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    APP_MAP = config.APP_MAP
except (FileNotFoundError, AttributeError):
    # Fallback to local config
    from config import APP_MAP


def notify(msg: str, urgency: str = "normal"):
    subprocess.run(["notify-send", "-u", urgency, "Hypr Session", msg])


def get_clients():
    result = subprocess.run(
        ["hyprctl", "clients", "-j"], capture_output=True, text=True
    )
    if result.returncode != 0:
        notify("Failed to get clients from Hyprland.", "critical")
        sys.exit(1)
    return json.loads(result.stdout)


def get_shell_children(pid: int):
    """Get cwd and foreground command for each child process of a terminal."""
    try:
        ps = subprocess.run(
            ["ps", "--ppid", str(pid), "-o", "pid=,comm="],
            capture_output=True, text=True,
        ).stdout.strip().splitlines()
        result = []
        for line in ps:
            parts = line.split()
            if not parts:
                continue
            child_pid = int(parts[0])
            comm = parts[1] if len(parts) > 1 else ""
            # Skip kitty's internal shell-integration helper
            if comm == "kitten":
                continue
            cwd = get_cwd(child_pid)
            if comm in KNOWN_SHELLS:
                # It's a shell — look at what command is running inside it
                command = get_foreground_command(child_pid)
            else:
                # Direct program (e.g., nvim via `kitty -e nvim`) — use its own cmdline
                command = read_cmdline(child_pid)
            result.append({"cwd": cwd, "command": command})
        return result
    except Exception:
        return []


def get_cwd(pid: int):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return None


def read_cmdline(pid: int):
    """Read the command line of a process."""
    try:
        with open(f"/proc/{pid}/cmdline", "r") as f:
            cmdline = [arg for arg in f.read().split("\0") if arg]
        return cmdline if cmdline else None
    except Exception:
        return None


def get_foreground_command(pid: int):
    """Get the command running inside a shell process, if any."""
    try:
        child = subprocess.run(
            ["ps", "--ppid", str(pid), "-o", "pid="],
            capture_output=True, text=True,
        ).stdout.strip().split()
        if not child or not child[0]:
            return None
        with open(f"/proc/{child[0]}/cmdline", "r") as f:
            cmdline = [arg for arg in f.read().split("\0") if arg]
        return cmdline if cmdline else None
    except Exception:
        return None


def get_mpv_file(pid: int):
    """Extract video file path from mpv process command line."""
    try:
        with open(f"/proc/{pid}/cmdline", "r") as f:
            cmdline = f.read().split("\0")
        # The last argument is usually the video file
        for arg in reversed(cmdline):
            if arg and not arg.startswith("-") and Path(arg).exists():
                return arg
    except Exception:
        pass
    return None


def save_session():
    clients = get_clients()
    session_data = []
    _term_children = {}
    _term_index = {}

    for c in clients:
        app_class = c.get("class")
        if not app_class:
            continue

        workspace = c["workspace"]["name"] if c.get("workspace") else "1"
        pid = c.get("pid")
        entry = {"class": app_class, "workspace": workspace}

        if app_class.lower() in TERMINALS and pid:
            # Terminals like kitty may share a single PID across multiple windows.
            # We enumerate all shell children once per PID, then assign each
            # window the next child so every window gets its own cwd/nvim state.
            if pid not in _term_children:
                _term_children[pid] = get_shell_children(pid)
                _term_index[pid] = 0

            idx = _term_index[pid]
            children = _term_children[pid]
            if idx < len(children):
                entry["cwd"] = children[idx]["cwd"]
                entry["command"] = children[idx]["command"]
            else:
                entry["cwd"] = None
                entry["command"] = None
            _term_index[pid] = idx + 1

        elif app_class.lower() == "mpv" and pid:
            mpv_file = get_mpv_file(pid)
            entry["mpv_file"] = mpv_file
            entry["cwd"] = None
            entry["command"] = None

        else:
            entry["cwd"] = None
            entry["command"] = None

        session_data.append(entry)

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_FILE, "w") as f:
        json.dump(session_data, f, indent=2)

    notify(f"Session saved ({len(session_data)} apps).")


def restore_session(skip_open=False):
    running_clients = get_clients()
    running_classes = {c.get("class", "").lower() for c in running_clients}

    if not SESSION_FILE.exists():
        notify("No saved session found.", "critical")
        sys.exit(1)

    with open(SESSION_FILE) as f:
        session_data = json.load(f)

    restored, skipped = 0, 0

    for entry in session_data:
        app_class = entry["class"]
        workspace = entry.get("workspace", "1")
        cwd = entry.get("cwd")
        command = entry.get("command")

        # Backward compatibility: old sessions used in_nvim instead of command
        if command is None and entry.get("in_nvim"):
            command = ["nvim"]

        # With --skip-open, skip apps that are already running.
        # Terminals are exempt — they always allow multiple instances.
        if skip_open and app_class.lower() in running_classes and app_class.lower() not in TERMINALS:
            skipped += 1
            continue

        elif app_class.lower() in TERMINALS:
            term = app_class.lower()
            if cwd and Path(cwd).exists():
                if command:
                    cmd = f"{term} --working-directory '{cwd}' -e {shlex.join(command)}"
                else:
                    cmd = f"{term} --working-directory '{cwd}'"
            else:
                if command:
                    cmd = f"{term} -e {shlex.join(command)}"
                else:
                    cmd = term

        elif app_class.lower() == "mpv":
            mpv_file = entry.get("mpv_file")
            if mpv_file and Path(mpv_file).exists():
                cmd = f"mpv --pause '{mpv_file}'"
            else:
                notify(f"MPV skipped (missing file: {mpv_file})", "low")
                skipped += 1
                continue

        else:
            cmd = APP_MAP.get(app_class)

        if not cmd:
            skipped += 1
            continue

        launch = f"[workspace {workspace}] uwsm app -- {cmd}"
        subprocess.run(["hyprctl", "dispatch", "exec", launch])
        restored += 1

    msg = f"Restored {restored} apps"
    if skipped:
        msg += f" (skipped {skipped})"
    notify(msg, "low")


def clear_session():
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        notify("Session cleared. It won’t be restored next time.")
    else:
        notify("No session to clear.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        notify("Usage: hypr-session [save|restore|clear] [--skip-open]")
        sys.exit(1)

    action = sys.argv[1].lower()
    skip_open = "--skip-open" in sys.argv

    if action == "save":
        save_session()
    elif action == "restore":
        restore_session(skip_open=skip_open)
    elif action == "clear":
        clear_session()
    else:
        notify("Invalid argument. Use save|restore|clear.", "critical")

#!/usr/bin/env python3
"""Start Chrome/Edge with remote debugging enabled for CDP-mode collection.

MediaCrawler-style CDP mode reuses your real browser (live login state + real
fingerprint). Run this script once after booting the machine, or any time the
debug browser is closed. It launches the default browser profile with
--remote-debugging-port=9222 and opens Douyin.

Usage:
    python3 start_chrome_debug.py          # launch with default profile
    python3 start_chrome_debug.py --edge   # force Microsoft Edge
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DEBUG_PORT = os.environ.get("APP_DOUYIN_CDP_PORT", "9222")
DOUYIN_URL = "https://www.douyin.com/"
PROJECT_DIR = Path(__file__).resolve().parent
# Chrome 136+ ignores --remote-debugging-port when launched with the default
# profile (security policy); a dedicated user-data-dir is required. Keeping the
# profile inside the project also isolates each team member's login state.
DEBUG_PROFILE_DIR = PROJECT_DIR / ".chrome-debug"


def find_browser(force_edge: bool = False) -> str | None:
    if force_edge:
        candidates = [
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)") + r"\Microsoft\Edge\Application\msedge.exe",
            os.environ.get("ProgramFiles", r"C:\Program Files") + r"\Microsoft\Edge\Application\msedge.exe",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates = [
            os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
            os.environ.get("ProgramFiles", r"C:\Program Files") + r"\Google\Chrome\Application\chrome.exe",
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)") + r"\Google\Chrome\Application\chrome.exe",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    # Linux / PATH lookup
    name = "microsoft-edge" if force_edge else ("google-chrome" if not force_edge else "google-chrome")
    return shutil.which(name) or shutil.which("chromium") or shutil.which("chromium-browser")


def port_open(port: str) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
            return True
    except OSError:
        return False


def main() -> int:
    force_edge = "--edge" in sys.argv
    if port_open(DEBUG_PORT):
        print(f"✓ 调试端口 {DEBUG_PORT} 已就绪，正在打开抖音…")
        webbrowser.open(DOUYIN_URL)
        return 0
    browser = find_browser(force_edge)
    if not browser:
        print("✗ 未找到 Chrome/Edge，请安装后重试。", file=sys.stderr)
        return 1
    print(f"正在启动调试浏览器：{browser}")
    cmd = [
        browser,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={DEBUG_PROFILE_DIR}",
        "--no-first-run",
        "--new-window",
        DOUYIN_URL,
    ]
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(cmd, creationflags=creationflags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"✓ 已启动调试浏览器，端口 {DEBUG_PORT}")
    print("  若本机 Chrome 已在运行，单实例模式可能忽略调试端口：请先关闭全部 Chrome 窗口再运行本脚本。")
    print("  在浏览器中完成抖音扫码登录后，即可在采集页使用 CDP 模式。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Start the frontend, backend, and analyzer as background services."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / ".runtime"
STATE_FILE = RUNTIME_DIR / "services.json"
BACKEND_DIR = ROOT / "fetch-api"
FRONTEND_DIR = ROOT / "if-ui"
ANALYZE_DIR = ROOT / "analyze-api"
SERVICE_NAMES = ("backend", "analyzer", "frontend")
MINIMUM_PYTHON = (3, 9)


def _root_config() -> dict[str, str]:
    result: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _pid_running(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False,
        )
        return f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _venv_python(project_dir: Path) -> Path:
    candidates = [
        project_dir / ".venv" / "Scripts" / "python.exe",
        project_dir / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(f"Virtual environment is missing in {project_dir.name}/.venv.")


def _run_setup(command: list[str], cwd: Path, description: str) -> None:
    """Run a setup command in the foreground so first-run errors are actionable."""
    print(f"{description}...")
    try:
        subprocess.run(command, cwd=cwd, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Cannot run {command[0]!r}. Please install it and add it to PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{description} failed (exit code {exc.returncode}).") from exc


def _file_digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _python_packages_available(python: Path, packages: tuple[str, ...]) -> bool:
    """A setup stamp alone is not enough if someone has recreated a venv."""
    check = "; ".join(f"import {package}" for package in packages)
    return subprocess.run(
        [str(python), "-c", check], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    ).returncode == 0


def _load_setup_state() -> dict[str, str]:
    try:
        loaded = json.loads((RUNTIME_DIR / "setup.json").read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_setup_state(state: dict[str, str]) -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)
    (RUNTIME_DIR / "setup.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def _ensure_python_environment(project_dir: Path, label: str, setup_state: dict[str, str], needs_browser: bool = False) -> None:
    requirements = project_dir / "requirements.txt"
    if not requirements.exists():
        raise RuntimeError(f"{label} is missing requirements.txt.")
    venv_dir = project_dir / ".venv"
    if not venv_dir.exists():
        _run_setup([sys.executable, "-m", "venv", str(venv_dir)], ROOT, f"Creating {label} virtual environment")
    python = _venv_python(project_dir)
    state_key = f"{project_dir.name}-requirements"
    digest = _file_digest(requirements)
    required_modules = (
        ("fastapi", "uvicorn", "playwright", "pydantic_settings")
        if needs_browser
        else ("fastapi", "uvicorn", "httpx", "pydantic_settings")
    )
    if setup_state.get(state_key) != digest or not _python_packages_available(python, required_modules):
        _run_setup([str(python), "-m", "pip", "install", "--upgrade", "pip"], project_dir, f"Updating {label} pip")
        _run_setup([str(python), "-m", "pip", "install", "-r", str(requirements)], project_dir, f"Installing {label} dependencies")
        setup_state[state_key] = digest
    if needs_browser:
        browser_key = f"{project_dir.name}-chromium-{digest}"
        if setup_state.get(browser_key) != "installed":
            _run_setup([str(python), "-m", "playwright", "install", "chromium"], project_dir, "Installing Chromium for platform login and collection")
            setup_state[browser_key] = "installed"


def _ensure_frontend_environment(setup_state: dict[str, str]) -> None:
    package_json = FRONTEND_DIR / "package.json"
    package_lock = FRONTEND_DIR / "package-lock.json"
    if not package_json.exists():
        raise RuntimeError("Frontend is missing package.json.")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("Node.js/npm is not installed or is not available on PATH. Install Node.js LTS, then run start.py again.")
    node = shutil.which("node.exe" if os.name == "nt" else "node")
    if not node:
        raise RuntimeError("Node.js is not available on PATH. Install Node.js LTS, then run start.py again.")
    fingerprint = _file_digest(package_lock if package_lock.exists() else package_json)
    state_key = "frontend-dependencies"
    if not (FRONTEND_DIR / "node_modules").exists() or setup_state.get(state_key) != fingerprint:
        install_command = [npm, "ci"] if package_lock.exists() else [npm, "install"]
        _run_setup(install_command, FRONTEND_DIR, "Installing frontend dependencies")
        setup_state[state_key] = fingerprint


def ensure_environment() -> None:
    """Prepare everything needed for a clean first run on Windows, macOS, or Linux."""
    if sys.version_info < MINIMUM_PYTHON:
        version = ".".join(map(str, MINIMUM_PYTHON))
        raise RuntimeError(f"Python {version} or later is required; current version is {sys.version.split()[0]}.")
    env_path = ROOT / ".env"
    env_example = ROOT / ".env.example"
    if not env_path.exists() and env_example.exists():
        shutil.copyfile(env_example, env_path)
        print("Created .env from .env.example. Review passwords and API keys before production use.")
    setup_state = _load_setup_state()
    _ensure_python_environment(BACKEND_DIR, "backend", setup_state, needs_browser=True)
    _ensure_python_environment(ANALYZE_DIR, "analyzer", setup_state)
    _ensure_frontend_environment(setup_state)
    _save_setup_state(setup_state)


def _npm_command(backend_url: str) -> list[str]:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("npm is not installed or is not available on PATH.")
    RUNTIME_DIR.mkdir(exist_ok=True)
    proxy_path = RUNTIME_DIR / "proxy.conf.json"
    proxy_path.write_text(json.dumps({"/api": {"target": backend_url, "secure": False, "changeOrigin": True}}), encoding="utf-8")
    command = [npm, "run", "start", "--", "--host", "127.0.0.1", "--port", "4200", "--proxy-config", str(proxy_path)]
    return command


def _spawn(name: str, command: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    RUNTIME_DIR.mkdir(exist_ok=True)
    log = (RUNTIME_DIR / f"{name}.log").open("ab", buffering=0)
    options: dict[str, Any] = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        if name == "frontend":
            options["shell"] = True
    else:
        options["start_new_session"] = True
    try:
        return subprocess.Popen(command, **options)
    finally:
        log.close()


def _wait_for_service(name: str, process: subprocess.Popen[bytes], port: int, timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited during startup. Check .runtime/{name}.log.")
        if _port_open(port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"{name} did not open port {port} within {timeout}s. Check .runtime/{name}.log.")


def _terminate(pid: int) -> None:
    if not _pid_running(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        timeout = 5
        while timeout > 0 and _pid_running(pid):
            time.sleep(0.5)
            timeout -= 0.5
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        if not _pid_running(pid):
            return
        time.sleep(0.25)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def _kill_port(port: int, name: str) -> None:
    pids = set()
    result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, check=False)
    for line in result.stdout.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            parts = line.strip().split()
            pids.add(int(parts[-1]))
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    time.sleep(2)
    if _port_open(port):
        raise RuntimeError(f"Port {port} ({name}) is still occupied by other programs. Please close them manually and try again.")


def stop_services(quiet: bool = False) -> None:
    state = _load_state()
    config = _root_config()
    backend_port = int(config.get("APP_FETCH_API_PORT", "8080"))
    analyzer_port = int(config.get("APP_ANALYZE_API_PORT", "8090"))
    stopped = False
    for name in reversed(SERVICE_NAMES):
        pid = state.get(name, {}).get("pid")
        if isinstance(pid, int) and _pid_running(pid):
            _terminate(pid)
            stopped = True
            if not quiet:
                print(f"Stopped {name} (PID {pid}).")
    STATE_FILE.unlink(missing_ok=True)
    if not stopped:
        ports = {backend_port: "backend", analyzer_port: "analyzer", 4200: "frontend"}
        for name in reversed(SERVICE_NAMES):
            port = ports.get(name)
            if port and _port_open(port):
                _kill_port(port, name)
                stopped = True
                if not quiet:
                    print(f"Stopped {name} on port {port}.")
    if not quiet and not stopped:
        print("No managed services are running.")


def start_services() -> None:
    ensure_environment()
    config = _root_config()
    backend_host = config.get("APP_FETCH_API_HOST", "127.0.0.1")
    backend_port = int(config.get("APP_FETCH_API_PORT", "8080"))
    analyzer_host = config.get("APP_ANALYZE_API_HOST", "127.0.0.1")
    analyzer_port = int(config.get("APP_ANALYZE_API_PORT", "8090"))
    state = _load_state()
    running = [name for name in SERVICE_NAMES if isinstance(state.get(name, {}).get("pid"), int) and _pid_running(state[name]["pid"])]
    if len(running) == len(SERVICE_NAMES):
        print("Frontend, backend, and analyzer are already running. Use restart.py to restart them.")
        return
    if running:
        stop_services(quiet=True)
    occupied = [str(port) for port in (backend_port, analyzer_port, 4200) if _port_open(port)]
    if occupied:
        ports = {backend_port: "backend", analyzer_port: "analyzer", 4200: "frontend"}
        for port in (backend_port, analyzer_port, 4200):
            if _port_open(port):
                try:
                    _kill_port(port, ports[port])
                except RuntimeError:
                    pass
        occupied = [str(port) for port in (backend_port, analyzer_port, 4200) if _port_open(port)]
        if occupied:
            raise RuntimeError(f"Required port(s) already in use: {', '.join(occupied)}.")

    backend_command = [str(_venv_python(BACKEND_DIR)), "-c",
        "import sys,asyncio,uvicorn.loops.asyncio as _a;"
        "_s=lambda use_subprocess=False: asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy());"
        "_a.asyncio_setup=_s;"
        f"from uvicorn import run;run('app.main:app',host='{backend_host}',port={backend_port})"]
    analyzer_command = [str(_venv_python(ANALYZE_DIR)), "-c",
        "import sys,asyncio,uvicorn.loops.asyncio as _a;"
        "_s=lambda use_subprocess=False: asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy());"
        "_a.asyncio_setup=_s;"
        f"from uvicorn import run;run('app.main:app',host='{analyzer_host}',port={analyzer_port})"]
    processes: dict[str, subprocess.Popen[bytes]] = {}
    try:
        processes["backend"] = _spawn("backend", backend_command, BACKEND_DIR)
        _wait_for_service("backend", processes["backend"], backend_port)
        processes["analyzer"] = _spawn("analyzer", analyzer_command, ANALYZE_DIR)
        _wait_for_service("analyzer", processes["analyzer"], analyzer_port)
        processes["frontend"] = _spawn("frontend", _npm_command(f"http://{backend_host}:{backend_port}"), FRONTEND_DIR)
        _wait_for_service("frontend", processes["frontend"], 4200)
        _save_state({name: {"pid": process.pid, "started_at": time.time()} for name, process in processes.items()})
    except Exception:
        for process in processes.values():
            _terminate(process.pid)
        raise
    print("Services started in the background.")
    print("Frontend: http://127.0.0.1:4200")
    print(f"Backend:  http://{backend_host}:{backend_port}")
    print(f"Analyzer: http://{analyzer_host}:{analyzer_port}")
    print("Logs:     .runtime/frontend.log, .runtime/backend.log, and .runtime/analyzer.log")


def main() -> int:
    try:
        start_services()
        return 0
    except Exception as exc:
        print(f"Start failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

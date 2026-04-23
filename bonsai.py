#!/usr/bin/env python3
"""Bonsai chat server for local MLX models on macOS."""

import argparse
import glob
import http.server
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path


APP_NAME = "BonsaiChat"
APP_VERSION = "1.0.0"
APP_SIGNATURE = "Nikša Barlović + Codex"
DEFAULT_MODEL = "prism-ml/Ternary-Bonsai-8B-mlx-2bit"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8080
DEFAULT_MLX_HOST = "127.0.0.1"
DEFAULT_MLX_PORT = 8079
DEFAULT_IDLE_TIMEOUT_SECONDS = 300
MLX_STARTUP_TIMEOUT_SECONDS = 300


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def app_support_dir() -> Path:
    override = os.environ.get("BONSAI_HOME")
    if override:
        path = Path(override).expanduser()
    else:
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def shell_join(parts) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def normalize_command(parts):
    if not parts:
        return []
    normalized = list(parts)
    normalized[0] = os.path.expanduser(os.path.expandvars(normalized[0]))
    return normalized


def command_exists(parts) -> bool:
    if not parts:
        return False
    exe = parts[0]
    if "/" in exe:
        return os.path.exists(exe) and os.access(exe, os.X_OK)
    return shutil.which(exe) is not None


def python_has_module(python_bin: str, module_name: str) -> bool:
    try:
        probe = subprocess.run(
            [
                python_bin,
                "-c",
                (
                    "import importlib.util, sys; "
                    f"sys.exit(0 if importlib.util.find_spec({module_name!r}) else 1)"
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return probe.returncode == 0


def wait_for_http(url: str, timeout_seconds: int):
    deadline = time.time() + timeout_seconds
    last_error = "server did not respond"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return True, ""
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(1)
    return False, last_error


class AppState:
    def __init__(self, args: argparse.Namespace):
        self.ui_host = args.ui_host
        self.ui_port = args.ui_port
        self.mlx_host = args.mlx_host
        self.mlx_port = args.mlx_port
        self.open_browser = not args.no_browser
        self.config_dir = app_support_dir()
        self.config_path = self.config_dir / "config.json"
        self.log_path = self.config_dir / "bonsai.log"
        self.html = resource_path("bonsai-chat.html").read_text(encoding="utf-8")

        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._shutdown = threading.Event()

        self._mlx_proc = None
        self._mlx_log_handle = None
        self._mlx_status = "stopped"
        self._last_error = ""
        self._resolved_mlx_command = ""
        self._active_model = ""
        self._last_activity = time.monotonic()

        self.config = self._load_config()
        if args.model:
            self.config["model"] = args.model.strip()
        if args.mlx_command:
            self.config["mlx_command"] = args.mlx_command.strip()
        if args.trust_remote_code:
            self.config["trust_remote_code"] = True
        if args.idle_timeout is not None:
            self.config["idle_timeout_seconds"] = max(0, int(args.idle_timeout))
        self._save_config()

        self._idle_thread = threading.Thread(target=self._idle_watch_loop, daemon=True)
        self._idle_thread.start()

    def _defaults(self):
        return {
            "model": DEFAULT_MODEL,
            "mlx_command": "",
            "trust_remote_code": False,
            "idle_timeout_seconds": DEFAULT_IDLE_TIMEOUT_SECONDS,
        }

    def _load_config(self):
        config = self._defaults()
        if self.config_path.exists():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config.update(loaded)
            except Exception:  # noqa: BLE001
                pass
        config["model"] = str(config.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        config["mlx_command"] = str(config.get("mlx_command") or "").strip()
        config["trust_remote_code"] = bool(config.get("trust_remote_code", False))
        config["idle_timeout_seconds"] = self._coerce_idle_timeout(
            config.get("idle_timeout_seconds")
        )
        return config

    def _save_config(self):
        payload = {
            "model": self.config["model"],
            "mlx_command": self.config.get("mlx_command", ""),
            "trust_remote_code": bool(self.config.get("trust_remote_code", False)),
            "idle_timeout_seconds": self._coerce_idle_timeout(
                self.config.get("idle_timeout_seconds")
            ),
        }
        self.config_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _coerce_idle_timeout(self, value) -> int:
        try:
            timeout = int(value)
        except (TypeError, ValueError):
            timeout = DEFAULT_IDLE_TIMEOUT_SECONDS
        return max(0, timeout)

    def _write_log_line(self, message: str):
        line = f"[{utc_timestamp()}] {message}\n"
        print(line, end="")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _python_candidates(self):
        candidates = []
        if not getattr(sys, "frozen", False):
            candidates.append(sys.executable)
        for candidate in (
            os.environ.get("BONSAI_PYTHON"),
            shutil.which("python3"),
            shutil.which("python"),
        ):
            if candidate:
                candidates.append(candidate)
        unique = []
        seen = set()
        for candidate in candidates:
            candidate = os.path.expanduser(os.path.expandvars(candidate))
            if candidate in seen:
                continue
            seen.add(candidate)
            unique.append(candidate)
        return unique

    def _resolve_mlx_command(self, explicit: str = ""):
        candidates = []
        seen = set()

        def add(parts, source, needs_module=False):
            parts = normalize_command(parts)
            if not parts:
                return
            key = tuple(parts)
            if key in seen:
                return
            seen.add(key)
            if not command_exists(parts):
                return
            if needs_module and not python_has_module(parts[0], "mlx_lm.server"):
                return
            candidates.append((parts, source))

        if explicit:
            add(shlex.split(explicit), "saved config")
        env_command = os.environ.get("BONSAI_MLX_SERVER")
        if env_command:
            add(shlex.split(env_command), "BONSAI_MLX_SERVER")

        on_path = shutil.which("mlx_lm.server")
        if on_path:
            add([on_path], "PATH")

        for match in sorted(
            glob.glob(str(Path.home() / "Library" / "Python" / "*" / "bin" / "mlx_lm.server"))
        ):
            add([match], "Library/Python")

        for python_bin in self._python_candidates():
            add([python_bin, "-m", "mlx_lm.server"], f"{Path(python_bin).name} -m", True)

        if not candidates:
            raise RuntimeError(
                "Could not find mlx_lm.server. Install it with "
                "'python3 -m pip install --user mlx-lm' or set BONSAI_MLX_SERVER."
            )
        return candidates[0]

    def _is_running_locked(self) -> bool:
        return self._mlx_proc is not None and self._mlx_proc.poll() is None

    def touch_activity(self):
        with self._lock:
            self._last_activity = time.monotonic()

    def snapshot(self):
        with self._lock:
            status = self._mlx_status
            if status == "running" and not self._is_running_locked():
                status = "stopped"
            idle_timeout = self._coerce_idle_timeout(self.config.get("idle_timeout_seconds"))
            idle_seconds_remaining = None
            if status == "running" and idle_timeout > 0:
                idle_seconds_remaining = max(
                    0, int(idle_timeout - (time.monotonic() - self._last_activity))
                )
            return {
                "app_name": APP_NAME,
                "app_version": APP_VERSION,
                "app_signature": APP_SIGNATURE,
                "model": self.config["model"],
                "active_model": self._active_model or self.config["model"],
                "trust_remote_code": bool(self.config.get("trust_remote_code", False)),
                "idle_timeout_seconds": idle_timeout,
                "idle_seconds_remaining": idle_seconds_remaining,
                "mlx_status": status,
                "last_error": self._last_error,
                "mlx_command": self.config.get("mlx_command", ""),
                "resolved_mlx_command": self._resolved_mlx_command,
                "config_path": str(self.config_path),
                "log_path": str(self.log_path),
                "ui_url": f"http://localhost:{self.ui_port}",
                "supports_online_lookup": True,
            }

    def _build_launch_command(self):
        command, source = self._resolve_mlx_command(self.config.get("mlx_command", ""))
        full_command = list(command) + [
            "--model",
            self.config["model"],
            "--port",
            str(self.mlx_port),
            "--host",
            self.mlx_host,
        ]
        if self.config.get("trust_remote_code"):
            full_command.append("--trust-remote-code")
        return command, full_command, source

    def _start_mlx_locked(self):
        if self._is_running_locked():
            self.touch_activity()
            return

        command, full_command, source = self._build_launch_command()
        log_handle = self.log_path.open("a", encoding="utf-8")

        with self._lock:
            self._mlx_status = "starting"
            self._last_error = ""

        self._write_log_line(f"Starting MLX from {source}: {shell_join(full_command)}")
        try:
            proc = subprocess.Popen(
                full_command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            log_handle.close()
            with self._lock:
                self._mlx_status = "error"
                self._last_error = str(exc)
            raise RuntimeError(f"Could not launch MLX server: {exc}") from exc

        ready, error_message = wait_for_http(
            f"http://127.0.0.1:{self.mlx_port}/v1/models",
            MLX_STARTUP_TIMEOUT_SECONDS,
        )
        if not ready:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log_handle.close()
            message = (
                f"MLX server did not become ready for model '{self.config['model']}' "
                f"within {MLX_STARTUP_TIMEOUT_SECONDS}s ({error_message})."
            )
            with self._lock:
                self._mlx_status = "error"
                self._last_error = message
            self._write_log_line(message)
            raise RuntimeError(message)

        with self._lock:
            self._mlx_proc = proc
            self._mlx_log_handle = log_handle
            self._mlx_status = "running"
            self._last_error = ""
            self._active_model = self.config["model"]
            self._resolved_mlx_command = shell_join(command)
            self._last_activity = time.monotonic()

        self._write_log_line(f"MLX ready on port {self.mlx_port}.")

    def ensure_mlx_running(self):
        with self._lifecycle_lock:
            self._start_mlx_locked()

    def _wait_for_mlx_exit(self, proc, timeout: int) -> bool:
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
        except KeyboardInterrupt:
            # During shutdown we prefer a clean exit over surfacing another Ctrl+C
            # from the terminal while waiting on the child process to stop.
            return proc.poll() is not None

    def _stop_mlx_locked(self, reason: str):
        with self._lock:
            proc = self._mlx_proc
            log_handle = self._mlx_log_handle
            self._mlx_proc = None
            self._mlx_log_handle = None
            self._mlx_status = "sleeping" if reason == "idle timeout" else "stopped"

        if proc is not None:
            self._write_log_line(f"Stopping MLX ({reason}).")
            try:
                proc.terminate()
            except OSError:
                pass
            if not self._wait_for_mlx_exit(proc, timeout=10):
                try:
                    proc.kill()
                except OSError:
                    pass
                self._wait_for_mlx_exit(proc, timeout=5)

        if log_handle is not None:
            log_handle.close()

    def stop_mlx(self, reason: str = "stopped"):
        with self._lifecycle_lock:
            self._stop_mlx_locked(reason)

    def restart_mlx(self):
        with self._lifecycle_lock:
            self._stop_mlx_locked("restart")
            self._start_mlx_locked()

    def shutdown(self):
        self._shutdown.set()
        self.stop_mlx("shutdown")

    def _idle_watch_loop(self):
        while not self._shutdown.wait(5):
            with self._lock:
                running = self._is_running_locked()
                idle_timeout = self._coerce_idle_timeout(
                    self.config.get("idle_timeout_seconds")
                )
                idle_for = time.monotonic() - self._last_activity

            if not running or idle_timeout <= 0:
                continue
            if idle_for >= idle_timeout:
                try:
                    self.stop_mlx("idle timeout")
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._mlx_status = "error"
                        self._last_error = str(exc)

    def update_config(self, payload: dict):
        if not isinstance(payload, dict):
            raise RuntimeError("Expected a JSON object.")

        apply_now = bool(payload.get("apply", False))
        updated = dict(self.config)

        if "model" in payload:
            model = str(payload["model"]).strip()
            if not model:
                raise RuntimeError("Model cannot be empty.")
            updated["model"] = model

        if "mlx_command" in payload:
            updated["mlx_command"] = str(payload["mlx_command"]).strip()

        if "trust_remote_code" in payload:
            updated["trust_remote_code"] = bool(payload["trust_remote_code"])

        if "idle_timeout_seconds" in payload:
            updated["idle_timeout_seconds"] = self._coerce_idle_timeout(
                payload["idle_timeout_seconds"]
            )

        previous = dict(self.config)
        self.config = updated
        self._save_config()

        if apply_now:
            try:
                self.restart_mlx()
            except Exception as exc:  # noqa: BLE001
                self.config = previous
                self._save_config()
                recovery_error = None
                try:
                    self.restart_mlx()
                except Exception as recovery_exc:  # noqa: BLE001
                    recovery_error = str(recovery_exc)

                message = (
                    f"Could not apply the new model/config: {exc}. "
                    f"Reverted to '{previous['model']}'."
                )
                if recovery_error:
                    message += f" Recovery also failed: {recovery_error}"
                with self._lock:
                    self._last_error = message
                raise RuntimeError(message) from exc

        return self.snapshot()

    def _hf_api(self):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="urllib3 v2 only supports OpenSSL",
            )
            from huggingface_hub import HfApi

        return HfApi()

    def _cached_model_ids(self):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="urllib3 v2 only supports OpenSSL",
            )
            from huggingface_hub import scan_cache_dir

        try:
            cache_info = scan_cache_dir()
        except Exception:  # noqa: BLE001
            return set()

        cached = set()
        for repo in cache_info.repos:
            if getattr(repo, "repo_type", None) == "model" and getattr(repo, "repo_id", None):
                cached.add(repo.repo_id)
        return cached

    def search_models_online(self, query: str, limit: int = 8):
        query = (query or "").strip()
        if not query:
            raise RuntimeError("Search query is empty.")

        api = self._hf_api()
        models = api.list_models(
            search=query,
            sort="downloads",
            direction=-1,
            limit=max(1, min(int(limit), 20)),
            full=False,
            cardData=False,
        )

        results = []
        for model in models:
            results.append(
                {
                    "id": getattr(model, "id", ""),
                    "downloads": getattr(model, "downloads", None),
                    "likes": getattr(model, "likes", None),
                    "pipeline_tag": getattr(model, "pipeline_tag", None),
                    "last_modified": str(
                        getattr(model, "last_modified", None)
                        or getattr(model, "lastModified", None)
                        or ""
                    ),
                }
            )
        return {"query": query, "results": results}

    def model_info_online(self, model_name: str):
        model_name = (model_name or "").strip()
        if not model_name:
            raise RuntimeError("Model name is empty.")

        expanded = os.path.expanduser(model_name)
        if os.path.exists(expanded):
            path = Path(expanded).resolve()
            return {
                "kind": "local",
                "found": True,
                "id": str(path),
                "local_available": True,
                "download_required": False,
                "last_modified": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(path.stat().st_mtime),
                ),
            }

        api = self._hf_api()
        info = api.model_info(model_name)
        local_available = model_name in self._cached_model_ids()
        return {
            "kind": "huggingface",
            "found": True,
            "id": getattr(info, "id", model_name),
            "downloads": getattr(info, "downloads", None),
            "likes": getattr(info, "likes", None),
            "pipeline_tag": getattr(info, "pipeline_tag", None),
            "sha": getattr(info, "sha", None),
            "local_available": local_available,
            "download_required": not local_available,
            "last_modified": str(
                getattr(info, "last_modified", None)
                or getattr(info, "lastModified", None)
                or ""
            ),
        }


class Handler(http.server.BaseHTTPRequestHandler):
    state = None

    def log_message(self, fmt, *args):  # noqa: D401
        return

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            body = self.state.html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.cors()
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/bonsai/config":
            self.send_json(200, self.state.snapshot())
            return

        if parsed.path == "/bonsai/search-models":
            params = urllib.parse.parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            limit = params.get("limit", ["8"])[0]
            try:
                payload = self.state.search_models_online(query, int(limit))
            except Exception as exc:  # noqa: BLE001
                self.send_json(502, {"error": str(exc)})
                return
            self.send_json(200, payload)
            return

        if parsed.path == "/bonsai/model-info":
            params = urllib.parse.parse_qs(parsed.query)
            model_name = params.get("model", [""])[0]
            try:
                payload = self.state.model_info_online(model_name)
            except Exception as exc:  # noqa: BLE001
                self.send_json(404, {"error": str(exc)})
                return
            self.send_json(200, payload)
            return

        if parsed.path.startswith("/v1/"):
            self._proxy("GET", b"")
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if parsed.path == "/bonsai/config":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self.send_json(400, {"error": "Invalid JSON body."})
                return

            try:
                snapshot = self.state.update_config(payload)
            except Exception as exc:  # noqa: BLE001
                self.send_json(400, {"error": str(exc)})
                return

            self.send_json(200, snapshot)
            return

        if parsed.path.startswith("/v1/"):
            self._proxy("POST", body)
            return

        self.send_response(404)
        self.end_headers()

    def _proxy(self, method: str, body: bytes):
        self.state.touch_activity()
        try:
            self.state.ensure_mlx_running()
        except Exception as exc:  # noqa: BLE001
            self.send_json(502, {"error": str(exc)})
            return

        url = f"http://127.0.0.1:{self.state.mlx_port}{self.path}"
        request = urllib.request.Request(url, data=body or None, method=method)
        request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(key, value)
                self.cors()
                self.end_headers()
                while True:
                    chunk = response.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as exc:
            body = exc.read() or json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001
            self.send_json(502, {"error": str(exc)})


def parse_args():
    parser = argparse.ArgumentParser(description="Bonsai local chat server")
    parser.add_argument("--model", type=str, help="Model repo or local model path")
    parser.add_argument("--mlx-command", type=str, help="Custom command used to start mlx_lm.server")
    parser.add_argument("--trust-remote-code", action="store_true", help="Pass --trust-remote-code to MLX")
    parser.add_argument("--ui-host", type=str, default=DEFAULT_UI_HOST)
    parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT)
    parser.add_argument("--mlx-host", type=str, default=DEFAULT_MLX_HOST)
    parser.add_argument("--mlx-port", type=int, default=DEFAULT_MLX_PORT)
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=None,
        help="Seconds of inactivity before the MLX server is stopped (0 disables)",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    return parser.parse_args()


def main():
    args = parse_args()
    state = AppState(args)
    Handler.state = state

    print(f"\n{APP_NAME}")
    print("=" * 36)
    print(f"  Version -> {APP_VERSION}")
    print(f"  Signed by -> {APP_SIGNATURE}")
    print(f"  UI -> http://localhost:{state.ui_port}")
    print(f"  Model -> {state.config['model']}")
    print(f"  Idle sleep -> {state.config['idle_timeout_seconds']}s")

    startup_error = None
    try:
        state.ensure_mlx_running()
    except Exception as exc:  # noqa: BLE001
        startup_error = str(exc)
        print(f"  MLX not ready yet -> {startup_error}")

    server = http.server.ThreadingHTTPServer((state.ui_host, state.ui_port), Handler)
    print("  Press Ctrl+C to stop.\n")

    if state.open_browser:
        threading.Timer(
            0.5,
            lambda: subprocess.Popen(
                ["open", f"http://localhost:{state.ui_port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ),
        ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()
        state.shutdown()


if __name__ == "__main__":
    main()

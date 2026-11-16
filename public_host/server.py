"""Loopback control UI. Import/start serves files only; service calls are explicit."""
from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from public_host.config import ConfigStore, runtime_config, validate_config, validate_program
from public_host.playback import make_player, output_devices
from public_host.providers import Cancelled, CompatibleChat, ProviderError, make_services
from public_host.session import Session
from public_host.program import Program, activity_catalog

WEB = Path(__file__).with_name("web")


class HostBusy(ProviderError):
    pass


class HostApp:
    def __init__(self, store=None):
        self.store = store or ConfigStore()
        self.lock = threading.RLock()
        self.session = None
        self.program = None
        self.run_mode = "manual"
        self.control_port = None
        self.preview = False
        self._job_thread = None
        self._job_cancel = threading.Event()
        self._job = {"running": False, "kind": "", "message": "", "result": None}

    def _busy(self):
        program = self.program.snapshot() if self.program else {}
        return (program.get("running", False) or program.get("blocked", False)
                or (self.session is not None and self.session.snapshot()["running"])
                or (self._job_thread is not None and self._job_thread.is_alive()))

    def _require_idle(self):
        if self._busy():
            raise HostBusy("请先停止会话或等待当前检查完成。")

    def status(self):
        with self.lock:
            result = self.session.snapshot() if self.session else {
                "running": False, "stopping": False, "phase": "idle", "state": "IDLE", "state_metadata": {},
                "speaker": None, "next_speaker": None, "queue_size": 0,
                "last_error": "", "last_outcome": "", "messages": [], "events": []}
            result.update(operation=copy.deepcopy(self._job), preview=self.preview,
                          run_mode=self.run_mode, program=self.program.snapshot() if self.program else None)
            return result

    def save(self, data):
        with self.lock:
            self._require_idle()
            return self.store.save(data)

    def _new_session(self, config, *, preview=False, managed=False):
        from public_host.avatar import VTSAvatar
        chat, synth = make_services(config)
        factory = lambda role: VTSAvatar(role["avatar"], self.store.directory / "vts")
        self.session = Session(config, chat=chat, synth=synth,
                               player=make_player(config), avatar_factory=factory, managed=managed)
        self.preview = preview
        self.session.start()
        return self.session

    def start(self, run_mode="manual"):
        with self.lock:
            self._require_idle()
            if run_mode not in ("manual", "program"):
                raise ProviderError("运行方式应为手动会话或节目单。")
            config = runtime_config(self.store.load())
            if run_mode == "program":
                validate_program(config["program"], roles=config["roles"])
                if config["program"].get("gomoku_port", 8791) == self.control_port:
                    raise ProviderError("五子棋端口不能与运行台端口相同。")
            self.run_mode, self.program = run_mode, None
            self._new_session(config, managed=run_mode == "program")
            if run_mode == "program":
                self.program = Program(config, self.session)
                self.program.start()
            return self.status()

    def next(self):
        with self.lock:
            if not self.program or not self.program.snapshot()["running"]:
                raise HostBusy("请先开始节目单。")
            self.program.next()
            return self.status()

    def end(self):
        with self.lock:
            if not self.program or not self.program.snapshot()["running"]:
                raise HostBusy("请先开始节目单。")
            self.program.end()
            return self.status()

    def stop(self):
        with self.lock:
            self._job_cancel.set()
            if self._job_thread and self._job_thread.is_alive():
                self._job["message"] = "正在停止检查，等待当前请求退出。"
            if self.session:
                self.session.stop()
            if self.program:
                self.program.stop()
            return self.status()

    def interrupt(self):
        with self.lock:
            if self.session:
                self.session.interrupt()
            return self.status()

    def submit(self, text, speaker=None, *, speech=False):
        with self.lock:
            if not self.session or self.preview:
                raise HostBusy("请先开始会话。")
            if self.program:
                return {"request_id": self.program.submit(text, speaker, speech=speech)}
            return {"request_id": (self.session.submit_speech(text, speaker) if speech
                                    else self.session.submit_text(text, speaker))}

    def _begin_job(self, kind, action):
        with self.lock:
            self._require_idle()
            cancel = threading.Event()
            self._job_cancel = cancel
            self._job = {"running": True, "kind": kind, "message": "正在检查…", "result": None}

            def run():
                try:
                    result = action(cancel)
                    if cancel.is_set():
                        raise Cancelled()
                    update = {"message": "检查完成。", "result": result}
                except Cancelled:
                    update = {"message": "检查已取消。", "result": None}
                except ProviderError as exc:
                    update = {"message": str(exc), "result": None}
                except Exception:
                    update = {"message": "检查未完成，请核对配置或本机依赖。", "result": None}
                with self.lock:
                    self._job.update(update, running=False)

            self._job_thread = threading.Thread(target=run, name="public-host-check", daemon=True)
            self._job_thread.start()
            return self.status()

    def probe_chat(self):
        with self.lock:
            config = self.store.load()
            self._require_service(config, "llm")
            def action(cancel):
                CompatibleChat(config["llm"]).complete(
                    [{"role": "user", "content": "Reply with a short greeting."}], cancel)
                return {"message": "已收到聊天服务的有效文本回复。"}
            return self._begin_job("chat", action)

    @staticmethod
    def _require_service(config, name):
        validate_config(config)
        if config["mode"] != "live":
            raise ProviderError("请切换并保存为真实服务模式，再测试连接或试听。")
        if not config[name]["base_url"] or not config[name]["model"].strip():
            raise ProviderError("请先保存该服务的基础地址和模型名。")

    def preview_speech(self, speaker):
        with self.lock:
            self._require_idle()
            self.program, self.run_mode = None, "manual"
            config = self.store.load()
            self._require_service(config, "tts")
            role = next((r for r in config["roles"] if r["name"] == speaker), None)
            if role is None or not role["voice"].strip():
                raise ProviderError("请先选择角色并保存它的音色。")
            config["roles"] = [copy.deepcopy(role)]
            config["roles"][0]["avatar"]["enabled"] = False
            session = self._new_session(config, preview=True)
            request_id = session.submit_speech("你好，这是一段语音试听。", speaker)

            def finish_preview():
                while True:
                    snapshot = session.snapshot()
                    if not snapshot["running"]:
                        break
                    terminal = session.result(request_id)
                    if terminal or snapshot["last_error"]:
                        session.stop()
                        break
                    time.sleep(0.05)
            threading.Thread(target=finish_preview, name="public-preview-cleanup", daemon=True).start()
            return self.status()

    def discover_avatar(self, speaker, *, authorize=False):
        with self.lock:
            config = self.store.load()
            role = next((r for r in config["roles"] if r["name"] == speaker), None)
            if role is None:
                raise ProviderError("请选择要连接的角色。")
            avatar_config = copy.deepcopy(role["avatar"])
            avatar_config.update(enabled=True, model_id="")
            def action(cancel):
                from public_host.avatar import VTSAvatar
                avatar = VTSAvatar(avatar_config, self.store.directory / "vts")
                try:
                    if cancel.is_set():
                        raise Cancelled()
                    avatar.connect(allow_authorize=authorize)
                    if cancel.is_set():
                        raise Cancelled()
                    return {"speaker": speaker, "models": avatar.models(), "parameters": avatar.parameters()}
                finally:
                    avatar.close()
            return self._begin_job("avatar", action)

    def close(self, timeout=15):
        self.stop()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if not self._busy():
                    return True
            time.sleep(0.05)
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "PublicHost/1"

    def log_message(self, *args):
        # No paths, request bodies, secrets or user messages in server logs.
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _local_request(self):
        host = self.headers.get("Host", "")
        valid = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in valid:
            raise ProviderError("仅允许通过本机控制页访问。")
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + host:
            raise ProviderError("请求来源与本机控制页不一致。")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ProviderError("不接受其他网站发起的操作。")

    def do_GET(self):
        try:
            self._local_request()
            path = urlsplit(self.path).path
            if path == "/api/config":
                return self._json(self.server.app.store.public())
            if path == "/api/status":
                return self._json(self.server.app.status())
            if path == "/api/activities":
                return self._json({"activities": activity_catalog()})
            if path == "/api/devices":
                return self._json({"devices": output_devices()})
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                     "/style.css": ("style.css", "text/css")}
            if path not in files:
                return self._json({"error": "页面不存在。"}, 404)
            filename, kind = files[path]
            body = (WEB / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)
        except ProviderError as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._json({"error": "无法读取本机页面或配置。"}, 500)

    def do_POST(self):
        try:
            self._local_request()
            if self.headers.get("X-Public-Host") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ProviderError("操作需要本机页面的 JSON 请求。")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100_000:
                raise ProviderError("请求内容为空或过大。")
            self.connection.settimeout(5)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ProviderError("请求格式不正确。")
            app, path = self.server.app, urlsplit(self.path).path
            if path == "/api/config": result = app.save(data)
            elif path == "/api/start": result = app.start(data.get("run_mode", "manual"))
            elif path == "/api/next": result = app.next()
            elif path == "/api/end": result = app.end()
            elif path == "/api/stop": result = app.stop()
            elif path == "/api/interrupt": result = app.interrupt()
            elif path == "/api/chat": result = app.submit(data.get("text"), data.get("speaker"))
            elif path == "/api/say": result = app.submit(data.get("text"), data.get("speaker"), speech=True)
            elif path == "/api/probe-chat": result = app.probe_chat()
            elif path == "/api/preview": result = app.preview_speech(data.get("speaker"))
            elif path == "/api/avatar": result = app.discover_avatar(data.get("speaker"), authorize=data.get("authorize") is True)
            else: return self._json({"error": "操作不存在。"}, 404)
            self._json(result)
        except HostBusy as exc:
            self._json({"error": str(exc)}, 409)
        except ProviderError as exc:
            self._json({"error": str(exc)}, 400)
        except (ValueError, TypeError, KeyError):
            self._json({"error": "请求字段格式不正确，请检查输入。"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._json({"error": "操作未完成，请检查本机依赖与配置。"}, 500)


def make_server(*, app=None, port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.app = app or HostApp()
    server.app.control_port = server.server_port
    return server

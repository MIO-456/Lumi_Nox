"""Optional VTube Studio output; never reads the private streamer's credentials."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

from public_host.providers import Cancelled, ProviderError


class VTSAvatar:
    """Drive one explicitly selected model through VTS tracking inputs.

    Empty model_id is discovery-only: authentication/listing work, injection does
    not. Audio callbacks should supply increasing integer generations so delayed
    frames from previous utterances cannot reopen the mouth.
    """

    PLUGIN_NAME = "Lumi Nox Public Host"
    PLUGIN_DEVELOPER = "Lumi Nox Contributors"

    def __init__(self, config, token_dir, *, connector: Callable | None = None,
                 request_timeout: float = 2.0, frame_interval: float = 0.05,
                 model_check_interval: float = 0.5, authorization_timeout: float = 60.0):
        self.config = dict(config)
        self.enabled = bool(self.config.get("enabled", False))
        self.url = self.config.get("url", "ws://127.0.0.1:8001")
        self.model_id = self.config.get("model_id", "")
        self.mouth_parameter = self.config.get("mouth_parameter", "MouthOpen")
        try:
            self.mouth_gain = float(self.config.get("mouth_gain", 1))
        except (TypeError, ValueError):
            raise ProviderError("VTS mouth gain must be a positive number.") from None
        if not math.isfinite(self.mouth_gain) or self.mouth_gain <= 0:
            raise ProviderError("VTS mouth gain must be a positive number.")
        if not isinstance(self.url, str):
            raise ProviderError("VTS requires a ws:// or wss:// address.")
        try:
            address = urlsplit(self.url)
            valid_url = (address.scheme in {"ws", "wss"} and address.hostname
                         and not address.username and not address.password
                         and not address.query and not address.fragment)
            address.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ProviderError("VTS requires a ws:// or wss:// address without credentials.")
        if not isinstance(self.model_id, str) or not isinstance(self.mouth_parameter, str):
            raise ProviderError("VTS model and mouth parameter must be text.")
        if not self.mouth_parameter:
            raise ProviderError("Choose a VTS mouth input parameter.")
        self.token_dir = Path(token_dir)
        self._token_path = self.token_dir / (hashlib.sha256(self.url.encode()).hexdigest() + ".json")
        self._connector = connector
        self._timeout = max(0.05, float(request_timeout))
        self._authorization_timeout = max(self._timeout, float(authorization_timeout))
        self._frame_interval = max(0.01, float(frame_interval))
        self._model_check_interval = max(0.01, float(model_check_interval))
        self._state_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._socket = None
        self._thread = None
        self._connected = False
        self._closed = True
        self._last_error = ""
        self._level = 0.0
        self._generation = -1

    @property
    def connected(self):
        with self._state_lock:
            return self._connected

    @property
    def last_error(self):
        with self._state_lock:
            return self._last_error

    def check_error(self):
        """Surface background failure on the session thread, including at EOF."""
        with self._state_lock:
            if self._last_error:
                raise ProviderError(self._last_error)

    def _load_token(self):
        try:
            if not self._token_path.exists():
                return None
            if self._token_path.stat().st_size > 4096:
                raise ValueError
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
            token = data.get("authenticationToken")
            if not isinstance(token, str) or not token or len(token) > 64 or not token.isascii():
                raise ValueError
            return token
        except (OSError, ValueError, TypeError, AttributeError):
            raise ProviderError("Cannot read this public host's VTS token; authorize it again.") from None

    def _save_token(self, token):
        temporary = None
        try:
            self.token_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.token_dir,
                                             prefix=".vts-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                json.dump({"authenticationToken": token}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._token_path)
        except OSError:
            raise ProviderError("Cannot save this public host's VTS authorization.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _open_socket(self):
        connector = self._connector
        if connector is None:
            try:
                from websockets.sync.client import connect
            except ImportError:
                raise ProviderError("VTS requires websockets >= 12; install the host requirements.") from None
            connector = connect
        try:
            return connector(self.url, open_timeout=self._timeout,
                             close_timeout=self._timeout, max_size=1024 * 1024)
        except Exception:
            raise ProviderError("Cannot connect to VTube Studio; check its API switch and port.") from None

    def _request(self, message_type, data=None, *, guard=None, timeout=None):
        with self._request_lock:
            if guard is not None and not guard():
                raise Cancelled("VTS output is no longer current.")
            with self._state_lock:
                socket = self._socket
            if socket is None:
                raise ProviderError("VTube Studio is not connected.")
            request_id = uuid.uuid4().hex
            packet = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
                      "requestID": request_id, "messageType": message_type}
            if data is not None:
                packet["data"] = data
            deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
            try:
                socket.send(json.dumps(packet, allow_nan=False))
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    reply = json.loads(socket.recv(timeout=remaining))
                    if not isinstance(reply, dict):
                        raise ValueError
                    if reply.get("requestID") != request_id:
                        continue
                    if reply.get("messageType") == "APIError":
                        error_id = (reply.get("data") or {}).get("errorID")
                        suffix = f" (error {error_id})" if type(error_id) is int else ""
                        raise ProviderError("VTube Studio rejected the request" + suffix + ".")
                    expected = message_type.removesuffix("Request") + "Response"
                    if reply.get("messageType") != expected or not isinstance(reply.get("data"), dict):
                        raise ValueError
                    return reply["data"]
            except (ProviderError, Cancelled):
                raise
            except TimeoutError:
                raise ProviderError("VTube Studio did not respond in time.") from None
            except Exception:
                raise ProviderError("VTube Studio disconnected or returned an invalid response.") from None

    def _authenticate(self, token):
        data = self._request("AuthenticationRequest", {
            "pluginName": self.PLUGIN_NAME, "pluginDeveloper": self.PLUGIN_DEVELOPER,
            "authenticationToken": token})
        return data.get("authenticated") is True

    def connect(self, allow_authorize=False):
        if not self.enabled:
            return self
        with self._lifecycle_lock:
            if self.connected:
                return self
            old_thread = self._thread
            if old_thread is not None and old_thread.is_alive():
                old_thread.join(timeout=self._timeout + 0.25)
                if old_thread.is_alive():
                    raise ProviderError("Previous VTS connection is still stopping.")
            try:
                token = self._load_token()
            except ProviderError:
                if not allow_authorize:
                    raise
                token = None
            if token is None and not allow_authorize:
                raise ProviderError("Authorize this public host in VTube Studio first.")
            with self._state_lock:
                self._closed = False
                self._last_error = ""
                self._level = 0.0
                self._stop.clear()
            try:
                self._socket = self._open_socket()
                authenticated = False
                if token:
                    try:
                        authenticated = self._authenticate(token)
                    except ProviderError:
                        if not allow_authorize:
                            raise
                if not authenticated:
                    if not allow_authorize:
                        raise ProviderError("VTS authorization was rejected; use the authorize button again.")
                    data = self._request("AuthenticationTokenRequest", {
                        "pluginName": self.PLUGIN_NAME, "pluginDeveloper": self.PLUGIN_DEVELOPER},
                        timeout=self._authorization_timeout)
                    token = data.get("authenticationToken")
                    if not isinstance(token, str) or not token or len(token) > 64 or not token.isascii():
                        raise ProviderError("VTube Studio did not provide a valid authorization token.")
                    if not self._authenticate(token):
                        raise ProviderError("VTube Studio did not authorize this public host.")
                    self._save_token(token)
                if self.model_id:
                    self._check_model()
                    parameters = self.parameters()
                    if self.mouth_parameter not in {item.get("name") for item in parameters}:
                        raise ProviderError("Configured mouth input is not available in VTube Studio.")
                with self._state_lock:
                    self._connected = True
                if self.model_id:
                    self._thread = threading.Thread(target=self._run, name="public-host-vts", daemon=True)
                    self._thread.start()
                return self
            except Exception as exc:
                with self._state_lock:
                    self._connected = False
                    self._closed = True
                    self._stop.set()
                    self._last_error = str(exc) if isinstance(exc, ProviderError) else "VTS connection failed."
                self._dispose_socket()
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError("VTS connection failed.") from None

    def models(self):
        data = self._request("AvailableModelsRequest")
        models = data.get("availableModels")
        if not isinstance(models, list) or any(not isinstance(item, dict) for item in models):
            raise ProviderError("VTube Studio returned an invalid model list.")
        return [{key: item.get(key) for key in ("modelID", "modelName", "modelLoaded")}
                for item in models]

    def parameters(self):
        data = self._request("InputParameterListRequest")
        defaults, custom = data.get("defaultParameters", []), data.get("customParameters", [])
        if not isinstance(defaults, list) or not isinstance(custom, list):
            raise ProviderError("VTube Studio returned an invalid parameter list.")
        result = defaults + custom
        if any(not isinstance(item, dict) for item in result):
            raise ProviderError("VTube Studio returned an invalid parameter list.")
        return result

    def _check_model(self):
        data = self._request("CurrentModelRequest")
        if data.get("modelLoaded") is not True or data.get("modelID") != self.model_id:
            raise ProviderError("VTS model does not match the configured model; output stopped.")

    def set_mouth_level(self, level, generation=None):
        if not self.enabled:
            return
        try:
            level = float(level)
        except (ValueError, TypeError):
            raise ProviderError("Mouth level must be a finite number.") from None
        if not math.isfinite(level):
            raise ProviderError("Mouth level must be a finite number.")
        if generation is not None and (type(generation) is not int or generation < 0):
            raise ProviderError("Mouth generation must be a non-negative integer.")
        with self._state_lock:
            if self._closed:
                return
            if self._last_error:
                raise ProviderError(self._last_error)
            if not self._connected or not self.model_id:
                raise ProviderError("Select and connect a VTS model before driving its mouth.")
            if generation is not None:
                if generation < self._generation:
                    return
                self._generation = generation
            self._level = max(0.0, min(1.0, level * self.mouth_gain))

    def _may_inject(self, generation):
        with self._state_lock:
            return (not self._closed and not self._stop.is_set() and self._connected
                    and generation == self._generation)

    def _inject(self, level, *, guard=None):
        self._request("InjectParameterDataRequest", {
            "faceFound": True, "mode": "set",
            "parameterValues": [{"id": self.mouth_parameter, "value": level, "weight": 1.0}]
        }, guard=guard)

    def _run(self):
        next_check = time.monotonic()
        try:
            while not self._stop.is_set():
                if time.monotonic() >= next_check:
                    self._check_model()
                    next_check = time.monotonic() + self._model_check_interval
                with self._state_lock:
                    level, generation = self._level, self._generation
                try:
                    self._inject(level, guard=lambda: self._may_inject(generation))
                except Cancelled:
                    if self._stop.is_set():
                        return
                self._stop.wait(self._frame_interval)
        except Exception as exc:
            with self._state_lock:
                if not self._closed:
                    self._last_error = str(exc) if isinstance(exc, ProviderError) else "VTS output failed."
                self._connected = False
                self._stop.set()
            self._dispose_socket()

    def _dispose_socket(self):
        with self._state_lock:
            socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def close(self):
        with self._lifecycle_lock:
            with self._state_lock:
                self._closed = True
                self._stop.set()
                self._level = 0.0
                self._generation += 1
                connected = self._connected
                self._connected = False
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=self._timeout + 0.25)
            try:
                if connected and self.model_id and self._socket is not None:
                    # Never close the mouth on an unrelated model the user loaded.
                    self._check_model()
                    self._inject(0.0)
            except (ProviderError, Cancelled):
                pass
            finally:
                self._dispose_socket()
                self._thread = None

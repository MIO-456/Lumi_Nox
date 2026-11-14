"""Local-only configuration; no implicit environment or production data loading."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from public_host.providers import ProviderError

LOCAL_DIR = Path(__file__).resolve().parents[1] / ".local-host"


def default_config() -> dict:
    def role(name, enabled):
        return {"name": name, "enabled": enabled,
                "system_prompt": "你是一个友好的虚拟主播，用简短自然的话回答观众。",
                "voice": "", "avatar": {"enabled": False, "url": "ws://127.0.0.1:" + ("8001" if enabled else "8002"),
                                           "model_id": "", "mouth_parameter": "MouthOpen", "mouth_gain": 1.0}}
    service = {"provider": "openai-compatible", "base_url": "", "model": "", "api_key": "", "timeout": 30}
    return {"version": 1, "mode": "live", "llm": {**service, "max_tokens": 256, "token_limit_field": "max_tokens"},
            "tts": dict(service), "audio": {"device": None},
            "roles": [role("示例A", True), role("示例B", False)]}


def _text(value, label, limit=2048, *, blank=True):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ProviderError(f"{label}格式不正确。")
    if not blank and not value.strip():
        raise ProviderError(f"请填写{label}。")
    return value


def _number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not low <= value <= high:
        raise ProviderError(f"{label}必须在 {low}–{high} 之间。")


def validate_config(data: dict, *, for_start=False) -> dict:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ProviderError("配置版本不正确，需要 version: 1。")
    if data.get("mode") not in ("live", "demo"):
        raise ProviderError("请选择真实服务或离线演示。")
    for service_name in ("llm", "tts"):
        service = data.get(service_name)
        if not isinstance(service, dict) or service.get("provider") != "openai-compatible":
            raise ProviderError("当前宿主支持 OpenAI-compatible 协议，请选择对应适配器。")
        url = _text(service.get("base_url"), "服务地址", 2048)
        if url:
            try:
                parsed = urlsplit(url)
                port = parsed.port
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError()
            except ValueError:
                raise ProviderError("服务地址需为 HTTP(S) 基础地址，不要在地址中填写 Key、查询串或账号。") from None
        _text(service.get("model"), "模型名", 200)
        key = _text(service.get("api_key"), "API Key", 4096)
        if "\n" in key or "\r" in key:
            raise ProviderError("API Key 不能包含换行。")
        _number(service.get("timeout"), 1, 120, "请求超时")
        if for_start and data["mode"] == "live" and (not url or not service["model"].strip()):
            raise ProviderError("请填写聊天和语音服务的基础地址与模型名。")
    llm = data["llm"]
    if llm.get("token_limit_field") not in ("max_tokens", "max_completion_tokens", ""):
        raise ProviderError("请选择支持的回复长度字段，或关闭该字段。")
    _number(llm.get("max_tokens"), 1, 4096, "回复长度")
    if not isinstance(llm["max_tokens"], int):
        raise ProviderError("回复长度必须是整数。")
    audio = data.get("audio")
    if not isinstance(audio, dict) or (audio.get("device") is not None and (isinstance(audio["device"], bool) or not isinstance(audio["device"], (int, str)))):
        raise ProviderError("音频设备需为设备名、编号或 null（系统默认）。")
    roles = data.get("roles")
    if not isinstance(roles, list) or not 1 <= len(roles) <= 2:
        raise ProviderError("请配置一至两个角色。")
    names, endpoints, active_count = set(), set(), 0
    for role in roles:
        if not isinstance(role, dict) or type(role.get("enabled")) is not bool:
            raise ProviderError("角色配置格式不正确。")
        name = _text(role.get("name"), "角色名", 48, blank=False).strip()
        if name.casefold() in names:
            raise ProviderError("两个角色不能使用同一个名字。")
        names.add(name.casefold())
        _text(role.get("system_prompt"), "角色设定", 12000)
        _text(role.get("voice"), "音色", 200)
        active_count += int(role["enabled"])
        if for_start and data["mode"] == "live" and role["enabled"] and not role["voice"].strip():
            raise ProviderError("请为启用的角色填写语音服务的音色名或 ID。")
        avatar = role.get("avatar")
        if not isinstance(avatar, dict) or type(avatar.get("enabled")) is not bool:
            raise ProviderError("VTS 配置格式不正确。")
        url = _text(avatar.get("url"), "VTS 地址", 200)
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "ws" or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
                raise ValueError()
            endpoint = parsed.port or 80
        except ValueError:
            raise ProviderError("VTS 地址应为本机 ws://127.0.0.1:端口。") from None
        _text(avatar.get("model_id"), "VTS 模型 ID", 200)
        _text(avatar.get("mouth_parameter"), "VTS 嘴型输入参数", 100, blank=False)
        _number(avatar.get("mouth_gain"), 0.1, 10, "嘴型灵敏度")
        if role["enabled"] and avatar["enabled"]:
            if endpoint in endpoints:
                raise ProviderError("双角色必须连接不同端口的 VTS 实例。")
            endpoints.add(endpoint)
            if for_start and not avatar["model_id"]:
                raise ProviderError("请填写 VTS 模型 ID，并在 VTS 中加载该模型。")
    if not active_count:
        raise ProviderError("请至少启用一个角色。")
    return copy.deepcopy(data)


def runtime_config(data):
    result = validate_config(data)
    if result["mode"] == "demo":
        for role in result["roles"]:
            role["avatar"]["enabled"] = False
    result = validate_config(result, for_start=True)
    result["roles"] = [r for r in result["roles"] if r["enabled"]]
    return result


class ConfigStore:
    def __init__(self, directory: Path | str = LOCAL_DIR):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "config.json"

    def load(self) -> dict:
        if not self.path.exists():
            return default_config()
        try:
            if self.path.stat().st_size > 100_000:
                raise ValueError()
            return validate_config(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            raise ProviderError("本机配置无法读取，请检查配置文件格式。") from None

    def public(self) -> dict:
        data = self.load()
        for name in ("llm", "tts"):
            data[name]["has_api_key"] = bool(data[name].pop("api_key", ""))
        return data

    def save(self, submitted: dict) -> dict:
        data = copy.deepcopy(submitted)
        if not isinstance(data, dict):
            raise ProviderError("配置应为 JSON 对象。")
        old = self.load()
        for name in ("llm", "tts"):
            service = data.get(name)
            if not isinstance(service, dict):
                raise ProviderError("缺少服务配置。")
            service.pop("has_api_key", None)
            clear = service.pop("clear_api_key", False)
            if type(clear) is not bool:
                raise ProviderError("清除 Key 选项格式不正确。")
            if clear:
                service["api_key"] = ""
            elif not service.get("api_key"):
                same_endpoint = service.get("base_url", "").rstrip("/") == old[name]["base_url"].rstrip("/")
                service["api_key"] = old[name]["api_key"] if same_endpoint else ""
        data = validate_config(data)
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".config-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise ProviderError("无法保存本机配置，请检查目录权限。") from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink(missing_ok=True)
        return self.public()

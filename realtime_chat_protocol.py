"""端到端实时对话二进制协议帧 — 构造 + 解析。

来源：火山引擎《WebSocket 协议-V3》文档 + dtd_test/ 探测验证。
"""
import gzip
import json
from typing import Any, Dict

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

# Message Type
CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY_REQUEST = 0b0010
SERVER_FULL_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

# Message Type Specific Flags
NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
NEG_SEQUENCE = 0b0010
NEG_SEQUENCE_1 = 0b0011
MSG_WITH_EVENT = 0b0100

# Serialization
NO_SERIALIZATION = 0b0000
JSON = 0b0001

# Compression
NO_COMPRESSION = 0b0000
GZIP = 0b0001

# Event ids used by the realtime dialogue protocol.
EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_START_SESSION = 100
EVENT_FINISH_SESSION = 102
EVENT_AUDIO_ONLY = 200
EVENT_SAY_HELLO = 300
EVENT_CHAT_TTS_TEXT = 500
EVENT_CHAT_TEXT_QUERY = 501
EVENT_CONVERSATION_CREATE = 510
EVENT_CONVERSATION_UPDATE = 511
EVENT_CONVERSATION_RETRIEVE = 512
EVENT_CONVERSATION_DELETE = 514

EVENT_CONNECTION_STARTED = 50
EVENT_SESSION_STARTED = 150
EVENT_SESSION_FINISHED = 152
EVENT_ASR_INFO = 450
EVENT_ASR_RESPONSE = 451
EVENT_ASR_ENDED = 459
EVENT_TTS_SENTENCE_START = 350
EVENT_TTS_RESPONSE = 352
EVENT_TTS_ENDED = 359
EVENT_CHAT_RESPONSE = 550
EVENT_CHAT_TEXT_QUERY_CONFIRMED = 553
EVENT_CHAT_RESPONSE_END = 559
EVENT_CONVERSATION_CREATED = 567
EVENT_CONVERSATION_UPDATED = 568
EVENT_CONVERSATION_RETRIEVED = 569
EVENT_CONVERSATION_TRUNCATED = 570
EVENT_CONVERSATION_DELETED = 571
EVENT_ERROR = 599


def generate_header(
    version: int = PROTOCOL_VERSION,
    message_type: int = CLIENT_FULL_REQUEST,
    message_type_specific_flags: int = MSG_WITH_EVENT,
    serial_method: int = JSON,
    compression_type: int = GZIP,
    reserved_data: int = 0x00,
    extension_header: bytes = bytes(),
) -> bytearray:
    header = bytearray()
    header_size = int(len(extension_header) / 4) + 1
    header.append((version << 4) | header_size)
    header.append((message_type << 4) | message_type_specific_flags)
    header.append((serial_method << 4) | compression_type)
    header.append(reserved_data)
    header.extend(extension_header)
    return header


def build_event_frame(event_id: int, session_id: str, payload: dict) -> bytes:
    """构造带 event + session_id + JSON payload 的客户端帧
    （用于 SayHello/ChatTextQuery/StartSession/FinishSession 等）。"""
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode())
    frame = bytearray(generate_header())
    frame.extend(event_id.to_bytes(4, "big"))
    sid_bytes = session_id.encode()
    frame.extend(len(sid_bytes).to_bytes(4, "big"))
    frame.extend(sid_bytes)
    frame.extend(len(body).to_bytes(4, "big"))
    frame.extend(body)
    return bytes(frame)


def build_event_frame_no_session(event_id: int, payload: dict) -> bytes:
    """StartConnection / FinishConnection 不带 session_id 的客户端帧。"""
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode())
    frame = bytearray(generate_header())
    frame.extend(event_id.to_bytes(4, "big"))
    frame.extend(len(body).to_bytes(4, "big"))
    frame.extend(body)
    return bytes(frame)


def build_audio_frame(session_id: str, audio: bytes) -> bytes:
    """构造 task_request (event 200) 音频帧 — 二进制不序列化 + gzip。"""
    frame = bytearray(generate_header(
        message_type=CLIENT_AUDIO_ONLY_REQUEST,
        serial_method=NO_SERIALIZATION,
    ))
    frame.extend((200).to_bytes(4, "big"))
    sid_bytes = session_id.encode()
    frame.extend(len(sid_bytes).to_bytes(4, "big"))
    frame.extend(sid_bytes)
    body = gzip.compress(audio)
    frame.extend(len(body).to_bytes(4, "big"))
    frame.extend(body)
    return bytes(frame)


def parse_response(res: bytes) -> Dict[str, Any]:
    """解析服务端帧。返回 dict：
       - message_type: "SERVER_FULL_RESPONSE" | "SERVER_ACK" | "SERVER_ERROR_RESPONSE" | (UNKNOWN_)
       - event: int (如有)
       - session_id: str (如有)
       - payload_msg: dict | bytes
       - 错误帧含 code 字段
    """
    if isinstance(res, str):
        return {}
    header_size = res[0] & 0x0f
    message_type = res[1] >> 4
    flags = res[1] & 0x0f
    serialization = res[2] >> 4
    compression = res[2] & 0x0f
    payload = res[header_size * 4:]
    result: Dict[str, Any] = {}

    if message_type in (SERVER_FULL_RESPONSE, SERVER_ACK):
        result["message_type"] = (
            "SERVER_FULL_RESPONSE" if message_type == SERVER_FULL_RESPONSE else "SERVER_ACK"
        )
        start = 0
        if flags & NEG_SEQUENCE:
            result["seq"] = int.from_bytes(payload[:4], "big", signed=False)
            start += 4
        if flags & MSG_WITH_EVENT:
            result["event"] = int.from_bytes(payload[start:start + 4], "big")
            start += 4
        payload = payload[start:]
        sid_size = int.from_bytes(payload[:4], "big", signed=True)
        result["session_id"] = payload[4:4 + sid_size].decode("utf-8", errors="replace")
        payload = payload[4 + sid_size:]
        body_size = int.from_bytes(payload[:4], "big")
        body = payload[4:4 + body_size]
        if compression == GZIP:
            body = gzip.decompress(body)
        if serialization == JSON:
            result["payload_msg"] = json.loads(body.decode("utf-8"))
        else:
            result["payload_msg"] = body
        return result

    if message_type == SERVER_ERROR_RESPONSE:
        result["message_type"] = "SERVER_ERROR_RESPONSE"
        result["code"] = int.from_bytes(payload[:4], "big")
        body_size = int.from_bytes(payload[4:8], "big")
        body = payload[8:8 + body_size]
        if compression == GZIP:
            body = gzip.decompress(body)
        if serialization == JSON:
            result["payload_msg"] = json.loads(body.decode("utf-8"))
        else:
            result["payload_msg"] = body
        return result

    return {"message_type": f"UNKNOWN_0x{message_type:x}"}

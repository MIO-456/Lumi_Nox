"""
说话人调度器 — 维护 next_speaker、@ 检测、input queue。

调度模型（spec 第 3 节）：
- 每条用户输入只触发一个角色回应（不是两个都回）
- 默认按 next_speaker 轮换；输入文本 @ 了名字就那个先回
- 弹幕 / SC 不打断当前发言，进 input queue
- ASR 语音输入打断当前发言，立即处理（在 conversation 层处理打断）
- 单角色模式：active_speakers 只一项，next_speaker 永远固定

跨角色 history 镜像在 conversation 层处理（每个角色看到的是 [对方说] 前缀的 user 消息）。
"""
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SpeakerScheduler:
    active_speakers: list                       # ["Lumi"] / ["Nox"] / ["Lumi", "Nox"]
    _next_idx: int = 0
    _input_queue: deque = field(default_factory=deque)
    _queue_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def next_speaker(self) -> str:
        return self.active_speakers[self._next_idx]

    def advance(self):
        """一条发言播完后调用，翻转到下一个 speaker。
        单角色场景下 next_speaker 仍然指向同一人（取模 1 = 0）。"""
        self._next_idx = (self._next_idx + 1) % len(self.active_speakers)

    def advance_from(self, actual_speaker: str):
        """按实际说话的人推进到下一个角色。"""
        if actual_speaker not in self.active_speakers:
            self.advance()
            return
        idx = self.active_speakers.index(actual_speaker)
        self._next_idx = (idx + 1) % len(self.active_speakers)

    @staticmethod
    def _extract_routing_text(text: str) -> str:
        """去掉输入源前缀，只保留真正需要做点名判断的正文。"""
        if not text:
            return ""
        parts = text.split("：", 2)
        if len(parts) >= 3 and parts[0] in {"弹幕", "SC", "礼物", "上舰", "进房", "互动"}:
            return parts[2]
        if len(parts) >= 2 and parts[0] in {"Mio", "键盘", "未知"}:
            return parts[1]
        return text

    def detect_addressed_speaker(self, text: str) -> Optional[str]:
        """检查文本里 @ 了哪个角色名。
        - 没 @：返回 None
        - 只 @ 了一个：返回该角色名
        - @ 了多个：返回 None（按 next_speaker 走）
        """
        route_text = self._extract_routing_text(text)
        addressed = []
        for name in self.active_speakers:
            # 大小写不敏感的子串匹配（中英文都能命中）
            if name.lower() in route_text.lower():
                addressed.append(name)
        if len(addressed) == 1:
            return addressed[0]
        return None

    def pick_speaker(self, user_input: Optional[str]) -> str:
        """决定本轮谁说话。
        - 无 user_input（空转）：next_speaker
        - 有 user_input：先看 @，否则 next_speaker
        """
        if user_input:
            addressed = self.detect_addressed_speaker(user_input)
            if addressed:
                return addressed
        return self.next_speaker

    def enqueue_input(self, text: str, source: str = "danmaku", speaker: str = "",
                      display_text: str = "", label: str = "", uid: int = 0):
        """弹幕 / SC / 礼物 入队，不打断当前发言。

        uid 为 B站 数字 UID（无则 0），供下游按 identity_key 写/读记忆用。
        """
        with self._queue_lock:
            item = {"text": text, "source": source, "uid": uid}
            if speaker:
                item["speaker"] = speaker
            if display_text:
                item["display_text"] = display_text
            if label:
                item["label"] = label
            self._input_queue.append(item)

    def pop_input(self) -> Optional[dict]:
        """每条 AI 发言播完后调用，取队列下一条作为下一轮 user message。"""
        with self._queue_lock:
            if self._input_queue:
                return self._input_queue.popleft()
        return None

    def pop_all_inputs(self, max_items: int = 0) -> list:
        """一次性取出当前队列里所有未回的输入，合并成一轮喂给 LLM。

        - 去重：同一个人(speaker)发的相同内容(text)只留最新一条，防刷屏；不误伤不同人发的相同短弹幕。
        - 不洪流（去重后 ≤ max_items，或 max_items=0）：全取、按时间正序、一条不丢。
        - 洪流（去重后 > max_items）：按优先级 + 取新装进 max_items 个名额——
            付费类(SC/上舰/礼物)全保不丢 > 弹幕取最新 > 互动类(进房/关注/分享/点赞)垫底、装不下就丢；
            付费类即使本身超 max_items 也全保（付费必回，优先于"别撑爆提示词"）。
          被丢弃的打印一笔、不静默。无论哪种情况，返回都按时间正序（老→新），便于模型理解先后。
        """
        with self._queue_lock:
            all_items = []
            while self._input_queue:
                all_items.append(self._input_queue.popleft())
        if not all_items:
            return []
        # 去重：同 (speaker, text) 留最新一条（从新往老扫，每 key 首见即最新）
        seen = set()
        deduped_rev = []
        for it in reversed(all_items):
            key = (it.get("speaker", ""), it.get("text", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped_rev.append(it)
        deduped = list(reversed(deduped_rev))  # 回到时间正序，每 key 留的是最新那条
        if not max_items or len(deduped) <= max_items:
            return deduped
        # 洪流：按优先级配额取新
        PAID = {"super_chat", "guard_buy", "gift"}
        INTERACT = {"enter_room", "interact"}
        paid = [x for x in deduped if x.get("source") in PAID]
        danmaku = [x for x in deduped if x.get("source") not in PAID and x.get("source") not in INTERACT]
        interact = [x for x in deduped if x.get("source") in INTERACT]
        chosen = list(paid)  # 付费全保（哪怕略超 max_items）
        rem = max_items - len(chosen)
        if rem > 0:
            chosen += danmaku[-rem:]  # 弹幕取最新 rem 条
        rem = max_items - len(chosen)
        if rem > 0:
            chosen += interact[-rem:]  # 互动取最新 rem 条
        chosen_ids = {id(x) for x in chosen}
        result = [x for x in deduped if id(x) in chosen_ids]  # 保持时间正序
        dropped = len(deduped) - len(result)
        if dropped:
            print(
                f"[弹幕调度] 洪流取舍：到 {len(all_items)} 条(去重后 {len(deduped)})，"
                f"保留 {len(result)}(付费 {len(paid)}) 丢 {dropped}(旧弹幕/低优先互动)",
                flush=True,
            )
        return result

    def queue_size(self) -> int:
        with self._queue_lock:
            return len(self._input_queue)

    def reset_rotation(self, start_speaker: Optional[str] = None):
        """重置轮换游标。start_speaker 为 None 时默认从 active_speakers[0] 开始。
        用于直播开始时确保从指定角色（默认 Lumi）开口。"""
        if start_speaker is None:
            self._next_idx = 0
            return
        if start_speaker not in self.active_speakers:
            raise ValueError(f"start_speaker {start_speaker} 不在 active_speakers 中")
        self._next_idx = self.active_speakers.index(start_speaker)

"""发声器策略：把一次 speak() 的多句文本变成实际音频。

两种实现：借端到端（BorrowE2EEmitter）/ 独立 TTS（IndependentTTSEmitter，当前为 CosyVoice）。
speak() 按 run_architecture 选其一，对 speak() 暴露统一接口：
feed / finish / abort / stream_task_id。发声器本身与具体引擎无关，只调 synth 的
open/feed/finish/abort 生命周期。
"""


class TtsEmitter:
    def feed(self, sentence: str):
        """送一句去合成，返回本次发声流的 task_id（首句建立，后续复用）。"""
        raise NotImplementedError

    def finish(self) -> None:
        """收尾（端到端发末包 / 独立关流）。"""
        raise NotImplementedError

    def abort(self) -> None:
        """被打断时调用（端到端按文档不发末包；独立中断合成）。"""
        raise NotImplementedError

    @property
    def stream_task_id(self):
        return None


class BorrowE2EEmitter(TtsEmitter):
    """端到端架构：借 realtime_chat.say_streaming 把多句拼成一个 ChatTTSText 流。"""

    def __init__(self, realtime_chat_mod, speaker):
        self._rc = realtime_chat_mod
        self._speaker = speaker
        self._task_id = None

    def feed(self, sentence: str):
        is_first = self._task_id is None
        tid = self._rc.say_streaming(
            sentence, is_first=is_first, is_last=False,
            task_id=self._task_id, speaker=self._speaker,
        )
        if is_first:
            self._task_id = tid
        return self._task_id

    def finish(self) -> None:
        if self._task_id is not None:
            self._rc.say_streaming(
                "", is_first=False, is_last=True,
                task_id=self._task_id, speaker=self._speaker,
            )

    def abort(self) -> None:
        # 端到端按协议被打断时不发末包，避免状态异常（见 realtime-chat 踩坑 3）
        pass

    @property
    def stream_task_id(self):
        return self._task_id


class IndependentTTSEmitter(TtsEmitter):
    """文本架构：把多句喂给独立 TTS 合成器（CosyVoice），首句建流。"""

    def __init__(self, synth, voice_id, model, cable_index):
        self._synth = synth
        self._voice_id = voice_id
        self._model = model
        self._cable_index = cable_index
        self._opened = False
        self._counter = 0

    def feed(self, sentence: str):
        if not self._opened:
            self._synth.open(self._voice_id, self._model, self._cable_index)
            self._opened = True
        self._synth.feed(sentence)
        self._counter += 1
        return "local_tts_stream"

    def finish(self) -> None:
        if self._opened:
            self._synth.finish()

    def abort(self) -> None:
        if self._opened:
            self._synth.abort()

    @property
    def stream_task_id(self):
        return "local_tts_stream" if self._opened else None

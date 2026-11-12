"""发声器策略：把一次 speak() 的多句文本变成实际音频。

两种实现：借端到端（BorrowE2EEmitter）/ 独立 TTS（IndependentTTSEmitter，支持 CosyVoice / Qwen）。
speak() 按 run_architecture 选其一，对 speak() 暴露统一接口：
feed / finish / abort / stream_task_id。发声器本身与具体引擎无关，只调 synth 的
open/feed/finish/abort 生命周期。
"""


class TtsEmitter:
    def feed(self, sentence: str, on_audio_start=None):
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
    """文本架构：把多句喂给独立 TTS 合成器，首句建流。"""

    def __init__(
        self,
        synth,
        voice_id=None,
        model=None,
        cable_index=None,
        *,
        open_kwargs=None,
    ):
        self._synth = synth
        self._voice_id = voice_id
        self._model = model if model is not None else (open_kwargs or {}).get("model")
        self._cable_index = cable_index
        self._open_kwargs = dict(open_kwargs) if open_kwargs is not None else None
        self._opened = False
        self._counter = 0

    def feed(self, sentence: str, on_audio_start=None, *, before_audio=None, pause_offsets=()):
        if not self._opened:
            if self._open_kwargs is None:
                self._synth.open(self._voice_id, self._model, self._cable_index)
            else:
                self._synth.open(**self._open_kwargs)
            self._opened = True
        if pause_offsets:
            if not self.supports_pause_tags:
                raise ValueError("This TTS backend does not support pause controls")
            self._synth.feed(sentence, on_audio_start=on_audio_start,
                             before_audio=before_audio, pause_offsets=pause_offsets)
        elif before_audio is not None:
            self._synth.feed(sentence, on_audio_start=on_audio_start, before_audio=before_audio)
        elif on_audio_start is None:
            self._synth.feed(sentence)
        else:
            self._synth.feed(sentence, on_audio_start=on_audio_start)
        self._counter += 1
        return "local_tts_stream"

    def finish(self) -> None:
        if self._opened:
            self._synth.finish()

    def abort(self) -> None:
        if self._opened:
            self._synth.abort()

    def request_abort(self) -> None:
        request = getattr(self._synth, "request_abort", None)
        if self._opened and callable(request):
            request()

    @property
    def supports_pause_tags(self):
        from audio.speech_pauses import supports_pause_tags
        return (supports_pause_tags(self._model)
                and bool(getattr(self._synth, "supports_pause_tags", False)))

    @property
    def stream_task_id(self):
        return "local_tts_stream" if self._opened else None

    @property
    def outcome(self):
        return getattr(self._synth, "outcome", "no_audio")

    @property
    def failed_sentences(self):
        return getattr(self._synth, "failed_sentences", ())

    @property
    def effective_backend(self):
        return getattr(self._synth, "effective_backend", "")

    @property
    def profile_id(self):
        return getattr(self._synth, "profile_id", "")

    @property
    def sentence_results(self):
        return getattr(self._synth, "sentence_results", ())

    @property
    def audio_duration_seconds(self):
        return getattr(self._synth, "audio_duration_seconds", 0.0)

    @property
    def generation_mode(self):
        return getattr(self._synth, "_generation_mode", "sentence")

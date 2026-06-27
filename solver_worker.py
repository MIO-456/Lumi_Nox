"""常驻求解子进程：把 Wordle / 汉兜 的 O(N²) 熵求解从主进程挪出来算。

毛病三背景：这两个游戏中盘候选词多时，"算最优猜测"是几百万次纯 Python 比对、要十几秒，
跑在主进程里会占着 CPU/GIL 把皮套 30Hz 动作循环挤成"慢放再快进"。把它放到这个独立子进程里
算，主进程的 GIL 就空出来了，皮套照常动。

本文件**只 import 那两个轻量 engine（实测 ~150ms，不牵 torch/lumi）**，绝不 import 主程序。

协议：stdin/stdout 上的二进制帧 = 4 字节大端长度 + pickle 负载。
  请求: {"engine": "wordle"|"handle", "candidates": [...], "n": int}
  响应: {"ok": True, "top": [(word, entropy), ...]} 或 {"ok": False, "error": "..."}
候选用 pickle 传：Wordle 是字符串列表，汉兜是 ParsedIdiom（简单 dataclass，可直接序列化）。
"""

import pickle
import struct
import sys

import wordle_engine
import handle_engine

_SOLVERS = {
    "wordle": wordle_engine.get_best_guesses,
    "handle": handle_engine.get_best_guesses,
}


def _read_frame(f):
    hdr = f.read(4)
    if len(hdr) < 4:
        return None
    (n,) = struct.unpack(">I", hdr)
    data = b""
    while len(data) < n:
        chunk = f.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return pickle.loads(data)


def _write_frame(f, obj):
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    f.write(struct.pack(">I", len(data)))
    f.write(data)
    f.flush()


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        req = _read_frame(stdin)
        if req is None:
            break  # 主进程关了管道 → 正常退出
        try:
            solver = _SOLVERS[req["engine"]]
            top = solver(req["candidates"], n=req.get("n", 5))
            _write_frame(stdout, {"ok": True, "top": top})
        except Exception as e:
            _write_frame(stdout, {"ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()

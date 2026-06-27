"""主进程侧的求解子进程客户端。

懒启动并管理一个常驻 solver_worker 子进程；线程安全地一问一答；带超时；
任何失败/超时都自动回退到调用方提供的"进程内"求解函数 —— 所以游戏永不因此卡死或崩，
最坏情况退化为改造前的行为。

用法（游戏 Bot 里）：
    import solver_client
    top = solver_client.best_guesses_or_fallback("wordle", candidates, 5, get_best_guesses)
"""

import os
import pickle
import struct
import subprocess
import sys
import threading
import queue

_WORKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solver_worker.py")
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 求解最长等多久（秒）。中盘候选多时单次求解可能数秒~十几秒，要给够；
# 超过则判子进程卡死 → 杀掉重启 + 本次回退进程内。
DEFAULT_TIMEOUT = 30.0

_DEAD = object()  # reader 线程在子进程退出时投递的哨兵


class SolverUnavailable(Exception):
    """子进程不可用（启动失败/超时/退出）→ 调用方应回退进程内。"""


class _WorkerProc:
    def __init__(self):
        self._proc = None
        self._respq = None
        self._lock = threading.Lock()

    def _spawn(self):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW：不弹黑窗
        self._proc = subprocess.Popen(
            [sys.executable, "-u", _WORKER_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=_PROJECT_DIR,
            **kwargs,
        )
        respq: queue.Queue = queue.Queue()
        self._respq = respq
        # reader 线程绑定到"这一个"子进程和队列，避免重启后旧线程污染新队列
        threading.Thread(
            target=self._read_loop, args=(self._proc, respq), daemon=True
        ).start()

    def _read_loop(self, proc, respq):
        f = proc.stdout
        try:
            while True:
                hdr = f.read(4)
                if len(hdr) < 4:
                    break
                (n,) = struct.unpack(">I", hdr)
                data = b""
                while len(data) < n:
                    chunk = f.read(n - len(data))
                    if not chunk:
                        break
                    data += chunk
                if len(data) < n:
                    break
                respq.put(pickle.loads(data))
        except Exception:
            pass
        finally:
            respq.put(_DEAD)

    def _kill(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None
        self._respq = None

    def request(self, engine, candidates, n, timeout):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
            respq = self._respq
            payload = pickle.dumps(
                {"engine": engine, "candidates": candidates, "n": n},
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            try:
                self._proc.stdin.write(struct.pack(">I", len(payload)))
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()
            except Exception as e:
                self._kill()
                raise SolverUnavailable(f"写请求失败: {e}")

            try:
                resp = respq.get(timeout=timeout)
            except queue.Empty:
                self._kill()  # 超时 → 杀掉重启，避免下次响应错位
                raise SolverUnavailable("求解超时")

            if resp is _DEAD:
                self._kill()
                raise SolverUnavailable("子进程退出")
            if not resp.get("ok"):
                # 求解函数自身抛错：不算子进程问题，但也回退进程内更稳
                raise SolverUnavailable(f"求解出错: {resp.get('error')}")
            return resp["top"]


_worker = _WorkerProc()


def best_guesses_or_fallback(engine, candidates, n, fallback, timeout=DEFAULT_TIMEOUT, log_fn=None):
    """优先走子进程求解；失败/超时 → 用 fallback(candidates, n=n) 进程内兜底。

    Args:
        engine: "wordle" | "handle"
        candidates: 候选列表（Wordle 是 str，汉兜是 ParsedIdiom）
        n: 取前几名
        fallback: 进程内求解函数，签名 fallback(candidates, n=n)
    """
    try:
        return _worker.request(engine, candidates, n, timeout)
    except Exception as e:
        if log_fn:
            try:
                log_fn(f"[求解子进程] 回退进程内: {type(e).__name__}: {e}")
            except Exception:
                pass
        return fallback(candidates, n=n)


def shutdown():
    """进程收尾时调用：杀掉求解子进程。"""
    _worker._kill()

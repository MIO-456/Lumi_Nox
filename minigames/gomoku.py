"""15x15 Gomoku rules and local bots for two configured characters."""

from __future__ import annotations

import random
import threading
from copy import deepcopy

from minigames.contracts import DecisionRequest
from minigames.host import MinigameHost
from minigames.result_speech import ResultSpeechCoordinator


BOARD_SIZE = 15
WIN_LENGTH = 5
DIRECTIONS = ((1, 0), (0, 1), (1, 1), (1, -1))


class GomokuEngine:
    def __init__(self, size: int = BOARD_SIZE):
        self.size = int(size)
        self.board = [[0 for _ in range(self.size)] for _ in range(self.size)]
        self.current_player = 1
        self.winner: int | None = None
        self.winning_line: list[list[int]] = []
        self.is_draw = False
        self.move_count = 0
        self.last_move: list[int] | None = None

    @property
    def terminal(self) -> bool:
        return self.winner is not None or self.is_draw

    def place(self, row: int, col: int) -> None:
        row, col = int(row), int(col)
        if self.terminal:
            raise ValueError("game is already finished")
        if not (0 <= row < self.size and 0 <= col < self.size):
            raise ValueError("cell is outside the board")
        if self.board[row][col] != 0:
            raise ValueError("cell is already occupied")

        player = self.current_player
        self.board[row][col] = player
        self.move_count += 1
        self.last_move = [row, col]
        line = self._winning_line_from(row, col, player)
        if line:
            self.winner = player
            self.winning_line = line
        elif self.move_count == self.size * self.size:
            self.is_draw = True
        else:
            self.current_player = 3 - player

    def empty_cells(self) -> list[tuple[int, int]]:
        return [
            (row, col)
            for row in range(self.size)
            for col in range(self.size)
            if self.board[row][col] == 0
        ]

    def force_draw(self) -> None:
        if self.terminal:
            return
        self.is_draw = True

    def candidate_cells(self, radius: int = 2) -> list[tuple[int, int]]:
        if self.move_count == 0:
            center = self.size // 2
            return [(center, center)]
        candidates = set()
        for row in range(self.size):
            for col in range(self.size):
                if self.board[row][col] == 0:
                    continue
                for rr in range(max(0, row - radius), min(self.size, row + radius + 1)):
                    for cc in range(max(0, col - radius), min(self.size, col + radius + 1)):
                        if self.board[rr][cc] == 0:
                            candidates.add((rr, cc))
        return sorted(candidates)

    def would_win(self, row: int, col: int, player: int) -> bool:
        if self.board[row][col] != 0:
            return False
        self.board[row][col] = player
        try:
            return bool(self._winning_line_from(row, col, player))
        finally:
            self.board[row][col] = 0

    def _winning_line_from(self, row: int, col: int, player: int) -> list[list[int]]:
        for dr, dc in DIRECTIONS:
            cells = [[row, col]]
            rr, cc = row - dr, col - dc
            while 0 <= rr < self.size and 0 <= cc < self.size and self.board[rr][cc] == player:
                cells.insert(0, [rr, cc])
                rr, cc = rr - dr, cc - dc
            rr, cc = row + dr, col + dc
            while 0 <= rr < self.size and 0 <= cc < self.size and self.board[rr][cc] == player:
                cells.append([rr, cc])
                rr, cc = rr + dr, cc + dc
            if len(cells) >= WIN_LENGTH:
                return cells
        return []


class GomokuAI:
    def __init__(
        self,
        *,
        attack_weight: float = 1.0,
        defense_weight: float = 1.08,
        seed: int | None = None,
    ):
        self.attack_weight = float(attack_weight)
        self.defense_weight = float(defense_weight)
        self._random = random.Random(seed)

    def choose(self, engine: GomokuEngine) -> tuple[int, int]:
        if engine.terminal:
            raise ValueError("cannot choose a move after the game ends")
        if engine.move_count == 0:
            center = engine.size // 2
            return center, center

        if engine.move_count == 1:
            center = engine.size // 2
            replies = [
                (center - 1, center),
                (center + 1, center),
                (center, center - 1),
                (center, center + 1),
            ]
            return self._random.choice(replies)

        if engine.move_count == 2 and engine.current_player == 1:
            # 平衡先手优势：黑棋第二颗从中心 5×5 之外的第一圈开局。
            # 只约束这一手，后续仍由正常攻防评分决定。
            center = engine.size // 2
            balancing_ring = [
                (row, col)
                for row in range(engine.size)
                for col in range(engine.size)
                if engine.board[row][col] == 0
                and max(abs(row - center), abs(col - center)) == 3
            ]
            return self._random.choice(balancing_ring)

        candidates = engine.candidate_cells()
        player = engine.current_player
        opponent = 3 - player

        winning = [cell for cell in candidates if engine.would_win(*cell, player)]
        if winning:
            return self._pick_tied(winning, engine.size)
        blocking = [cell for cell in candidates if engine.would_win(*cell, opponent)]
        if blocking:
            return self._pick_tied(blocking, engine.size)

        scored = []
        for row, col in candidates:
            attack = self._placement_score(engine, row, col, player)
            defense = self._placement_score(engine, row, col, opponent)
            center = engine.size // 2
            center_bonus = max(0, engine.size - abs(row - center) - abs(col - center))
            total = (
                attack * self.attack_weight
                + defense * self.defense_weight
                + center_bonus
                + self._random.random() * 0.01
            )
            scored.append((total, row, col))
        scored.sort(reverse=True)
        _score, row, col = scored[0]
        return row, col

    def _pick_tied(self, cells: list[tuple[int, int]], size: int) -> tuple[int, int]:
        center = size // 2
        ranked = sorted(
            cells,
            key=lambda cell: (abs(cell[0] - center) + abs(cell[1] - center), cell),
        )
        best_distance = abs(ranked[0][0] - center) + abs(ranked[0][1] - center)
        best = [
            cell for cell in ranked
            if abs(cell[0] - center) + abs(cell[1] - center) == best_distance
        ]
        return self._random.choice(best)

    @staticmethod
    def _placement_score(
        engine: GomokuEngine,
        row: int,
        col: int,
        player: int,
    ) -> float:
        score = 0.0
        engine.board[row][col] = player
        try:
            for dr, dc in DIRECTIONS:
                length = 1
                open_ends = 0
                for sign in (-1, 1):
                    rr, cc = row + sign * dr, col + sign * dc
                    while (
                        0 <= rr < engine.size
                        and 0 <= cc < engine.size
                        and engine.board[rr][cc] == player
                    ):
                        length += 1
                        rr, cc = rr + sign * dr, cc + sign * dc
                    if 0 <= rr < engine.size and 0 <= cc < engine.size and engine.board[rr][cc] == 0:
                        open_ends += 1
                if length >= 5:
                    score += 1_000_000
                elif length == 4:
                    score += 100_000 if open_ends == 2 else 18_000
                elif length == 3:
                    score += 8_000 if open_ends == 2 else 900
                elif length == 2:
                    score += 700 if open_ends == 2 else 90
                else:
                    score += 15 * open_ends

                # Also value one-gap five-cell windows, so the bots can build
                # and notice broken threes/fours instead of only straight runs.
                for offset in range(-4, 1):
                    cells = []
                    for step in range(5):
                        rr = row + (offset + step) * dr
                        cc = col + (offset + step) * dc
                        if not (0 <= rr < engine.size and 0 <= cc < engine.size):
                            cells = []
                            break
                        cells.append(engine.board[rr][cc])
                    if cells and all(value in (0, player) for value in cells):
                        stones = cells.count(player)
                        score += (0, 2, 20, 260, 4_000, 1_000_000)[stones]
        finally:
            engine.board[row][col] = 0
        return score


class GomokuAdapter:
    game_id = "gomoku"
    label = "五子棋"
    game_prompt = ""

    def __init__(self, player_names: list[str]):
        if len(player_names) != 2 or len(set(player_names)) != 2:
            raise ValueError("gomoku requires exactly two distinct players")
        self.player_names = list(player_names)
        self.engine = GomokuEngine()
        self.round_no = 0
        self.black_slot = 0
        self.scores = {name: 0 for name in self.player_names}
        self._result: dict | None = None

    def reset_match(self, seed=None) -> None:
        self.round_no = 0
        self.scores = {name: 0 for name in self.player_names}
        self.reset(seed=seed)

    def reset(self, seed=None) -> None:
        del seed
        self.round_no += 1
        self.black_slot = (self.round_no - 1) % 2
        self.engine = GomokuEngine()
        self._result = None

    def _slot_for_color(self, color: int) -> int:
        return self.black_slot if color == 1 else 1 - self.black_slot

    def _color_for_slot(self, slot: int) -> int:
        return 1 if slot == self.black_slot else 2

    def _name_for_color(self, color: int) -> str:
        return self.player_names[self._slot_for_color(color)]

    def current_player_slot(self) -> int:
        return self._slot_for_color(self.engine.current_player)

    def snapshot(self, viewer: str | None = None) -> dict:
        del viewer
        actor = "" if self.engine.terminal else self._name_for_color(self.engine.current_player)
        colors = {
            name: ("black" if self._color_for_slot(slot) == 1 else "white")
            for slot, name in enumerate(self.player_names)
        }
        result = self._result or {}
        return {
            "size": self.engine.size,
            "board": deepcopy(self.engine.board),
            "round_no": self.round_no,
            "actor": actor,
            "current_color": (
                "black" if self.engine.current_player == 1 else "white"
            ),
            "colors": colors,
            "move_count": self.engine.move_count,
            "last_move": deepcopy(self.engine.last_move),
            "status": "draw" if self.engine.is_draw else ("win" if self.engine.winner else "playing"),
            "winner": result.get("winner"),
            "loser": result.get("loser"),
            "winner_color": result.get("winner_color"),
            "draw": self.engine.is_draw,
            "scores": deepcopy(self.scores),
            "winning_line": deepcopy(self.engine.winning_line),
            "finish_reason": result.get("finish_reason"),
        }

    def legal_actions(self, actor: str) -> list[dict]:
        if self.engine.terminal or actor != self._name_for_color(self.engine.current_player):
            return []
        return [
            {"action": "place_stone", "row": row, "col": col}
            for row, col in self.engine.empty_cells()
        ]

    def apply_action(self, actor: str, action: dict) -> dict:
        expected_actor = self._name_for_color(self.engine.current_player)
        if actor != expected_actor:
            raise ValueError(f"it is not {actor}'s turn")
        self.engine.place(action["row"], action["col"])
        if self.engine.terminal and self._result is None:
            self._result = self._build_result()
            winner = self._result.get("winner")
            if winner:
                self.scores[winner] += 1
                self._result["scores"] = deepcopy(self.scores)
        return {
            "event_type": "stone_placed",
            "actor": actor,
            "color": "black" if self._color_for_slot(self.player_names.index(actor)) == 1 else "white",
            "move": [int(action["row"]), int(action["col"])],
            "move_count": self.engine.move_count,
        }

    def finish_round(self, *, reason: str) -> None:
        if self.engine.terminal:
            return
        if reason != "round_timeout":
            raise ValueError(f"unsupported Gomoku finish reason: {reason}")
        self.engine.force_draw()
        self._result = self._build_result()
        self._result["finish_reason"] = reason

    def _build_result(self) -> dict:
        winner = self._name_for_color(self.engine.winner) if self.engine.winner else None
        loser = next((name for name in self.player_names if name != winner), None) if winner else None
        return {
            "round_no": self.round_no,
            "winner": winner,
            "loser": loser,
            "winner_color": (
                "black" if self.engine.winner == 1 else ("white" if self.engine.winner == 2 else None)
            ),
            "draw": self.engine.is_draw,
            "move_count": self.engine.move_count,
            "scores": deepcopy(self.scores),
            "winning_line": deepcopy(self.engine.winning_line),
        }

    def is_terminal(self) -> bool:
        return self.engine.terminal

    def result(self) -> dict:
        if self._result is None:
            raise ValueError("gomoku round is not finished")
        return deepcopy(self._result)

    def describe_decision(self, decision: DecisionRequest) -> dict:
        return {
            "state_text": f"第{self.round_no}局轮到{decision.actor}落子",
            "tools": [],
            "spectator_text": "",
        }


class GomokuBotPair:
    def __init__(self, adapter: GomokuAdapter, *, seed: int | None = None):
        self.adapter = adapter
        seed_rng = random.Random(seed)
        self._bots = {
            adapter.player_names[0]: GomokuAI(
                attack_weight=1.02,
                defense_weight=1.0,
                seed=seed_rng.randrange(2**31),
            ),
            adapter.player_names[1]: GomokuAI(
                attack_weight=1.05,
                defense_weight=1.0,
                seed=seed_rng.randrange(2**31),
            ),
        }

    def choose_action(self, decision: DecisionRequest) -> dict:
        row, col = self._bots[decision.actor].choose(self.adapter.engine)
        return {"action": "place_stone", "row": row, "col": col}


class GomokuResultAnnouncer:
    """Give each character one short, perspective-correct result line."""

    def __init__(
        self,
        coordinator,
        *,
        dispatch_timeout: float = 2.0,
        speech_timeout: float = 20.0,
    ):
        self.coordinator = coordinator
        self.dispatch_timeout = float(dispatch_timeout)
        self.speech_timeout = float(speech_timeout)
        self._cancelled = threading.Event()

    def reset(self) -> None:
        self._cancelled.clear()

    def cancel(self) -> None:
        self._cancelled.set()

    def _speak(self, text: str, speaker: str, context: dict) -> bool:
        completed = self.coordinator.speak(
            text,
            speaker=speaker,
            context=context,
            dispatch_timeout=self.dispatch_timeout,
            speech_timeout=self.speech_timeout,
            stop_event=self._cancelled,
        )
        if not completed and not self._cancelled.is_set():
            raise RuntimeError("五子棋结算发言未完成，请检查语音服务或停止状态。")
        return completed

    def __call__(self, result: dict, round_no: int) -> None:
        context = {
            "game": "五子棋",
            "round_no": int(round_no),
            "winner": result.get("winner"),
            "loser": result.get("loser"),
            "move_count": int(result.get("move_count") or 0),
            "scores": deepcopy(result.get("scores") or {}),
        }
        if result.get("finish_reason"):
            context["finish_reason"] = result["finish_reason"]
        if result.get("draw"):
            draw_fact = (
                "本局达到两分钟上限，已判为平局。"
                if result.get("finish_reason") == "round_timeout"
                else "这一局五子棋打成平局。"
            )
            for speaker in context["scores"]:
                if self._cancelled.is_set():
                    break
                self._speak(
                    f"{draw_fact}请以你自己的口吻说一句简短感想，"
                    "只谈角色和胜负，不要讨论执棋颜色、先后手、规则或落子坐标。",
                    speaker,
                    context,
                )
            return

        loser = str(result.get("loser") or "")
        winner = str(result.get("winner") or "")
        if loser:
            self._speak(
                "你在这一局五子棋中落败。请以你自己的口吻说一句简短感想，"
                "只谈你和对手的胜负，不要讨论执棋颜色、先后手、规则或落子坐标。",
                loser,
                context,
            )
        if winner and not self._cancelled.is_set():
            self._speak(
                "你在这一局五子棋中获胜。请以你自己的口吻说一句简短感想，"
                "只谈你和对手的胜负，不要讨论执棋颜色、先后手、规则或落子坐标。",
                winner,
                context,
            )


def create_gomoku_host(
    *,
    bus,
    active_characters: list[str],
    html_path,
    event_callback=None,
    registry=None,
    move_interval: float = 1.0,
    round_pause: float = 1.5,
    round_timeout: float = 120.0,
    seed: int | None = None,
) -> MinigameHost:
    """Build the complete algorithm-vs-algorithm livestream mode."""
    if len(active_characters) != 2 or len(set(active_characters)) != 2:
        raise ValueError("gomoku requires exactly two distinct characters")
    adapter = GomokuAdapter(list(active_characters))
    bots = GomokuBotPair(adapter, seed=seed)
    kwargs = {}
    if registry is not None:
        kwargs["registry"] = registry
        browser = registry.create_browser(adapter.game_id, html_path)
    else:
        from minigames.web_bridge import MinigameWebBridge
        browser = MinigameWebBridge(html_path, game_id=adapter.game_id)
    speech = ResultSpeechCoordinator(bus)
    announcer = GomokuResultAnnouncer(speech)
    return MinigameHost(
        adapter,
        bus=bus,
        browser=browser,
        participation_mode="duel",
        active_characters=list(active_characters),
        event_callback=event_callback,
        action_provider=bots,
        move_interval=move_interval,
        move_interval_max=move_interval * 1.3,
        repeat_rounds=True,
        round_pause=round_pause,
        round_timeout=round_timeout,
        round_finished_callback=announcer,
        **kwargs,
    )

"""Bound solver work without timing assertions or external word downloads."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from games.word_games.wordle import engine as wordle
from games.word_games.handle import engine as handle


class SolverSamplingTests(unittest.TestCase):
    def test_large_wordle_pool_limits_work_and_keeps_answers_legal(self):
        candidates = [f"candidate-{index}" for index in range(2000)]
        with patch.object(wordle, "compute_entropy", return_value=1.0) as entropy:
            result = wordle.get_best_guesses(candidates, n=5)
        self.assertLessEqual(entropy.call_count, 500)
        self.assertTrue(all(len(call.args[1]) <= 400 for call in entropy.call_args_list))
        self.assertEqual(len(result), 5)
        self.assertTrue(all(word in candidates for word, _ in result))

    def test_large_handle_pool_limits_work_and_keeps_answers_legal(self):
        candidates = [SimpleNamespace(word=f"idiom-{index}") for index in range(2000)]
        with patch.object(handle, "compute_entropy", return_value=1.0) as entropy:
            result = handle.get_best_guesses(candidates, n=5)
        self.assertLessEqual(entropy.call_count, 500)
        self.assertTrue(all(len(call.args[1]) <= 400 for call in entropy.call_args_list))
        self.assertEqual(len(result), 5)
        self.assertTrue(all(word in {candidate.word for candidate in candidates} for word, _ in result))

    def test_small_wordle_pool_still_uses_exact_entropy(self):
        candidates = ["apple", "angle", "amble", "algae"]
        expected = sorted(((word, wordle.compute_entropy(word, candidates)) for word in candidates),
                          key=lambda item: -item[1])
        self.assertEqual(wordle.get_best_guesses(candidates), expected)

    def test_tiny_pools_do_not_sample_or_evaluate(self):
        for engine, candidates, expected in (
            (wordle, ["apple", "angle"], [("apple", 0.0), ("angle", 0.0)]),
            (handle, [SimpleNamespace(word="测试成语")], [("测试成语", 0.0)]),
        ):
            with self.subTest(engine=engine.__name__), patch.object(engine, "compute_entropy") as entropy:
                self.assertEqual(engine.get_best_guesses(candidates), expected)
                entropy.assert_not_called()


if __name__ == "__main__":
    unittest.main()

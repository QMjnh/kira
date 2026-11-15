from __future__ import annotations

import threading
import time
import unittest

from kira.tasks import ordered_parallel


class OrderedParallelTests(unittest.TestCase):
    def test_runs_concurrently_but_yields_in_order_with_per_item_errors(self) -> None:
        running, peak, lock = 0, 0, threading.Lock()

        def work(value: int) -> int:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05 if value % 2 else 0.01)
            with lock:
                running -= 1
            if value == 3:
                raise OSError("network down")
            return value * 10

        started = time.monotonic()
        results = list(ordered_parallel(range(12), work, workers=4))
        self.assertLess(time.monotonic() - started, 12 * 0.03)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual([item for item, _, _ in results], list(range(12)))
        self.assertIsInstance(results[3][2], OSError)
        self.assertEqual([result for item, result, _ in results if item != 3], [v * 10 for v in range(12) if v != 3])

    def test_stopping_early_discards_finished_work_and_starts_nothing_new(self) -> None:
        cancelled, started, discarded = threading.Event(), [], []

        def work(value: int) -> int:
            started.append(value)
            time.sleep(0.02)
            return value

        downloads = ordered_parallel(range(100), work, workers=2, cancelled=cancelled, discard=discarded.append)
        for item, _, _ in downloads:
            if item == 1:
                cancelled.set()
                downloads.close()
        self.assertLess(len(started), 10)
        self.assertEqual(sorted(discarded + [0, 1]), sorted(started))


if __name__ == "__main__":
    unittest.main()

"""Background tasks with progress, so long imports and syncs never block a request."""

from __future__ import annotations

import concurrent.futures
import threading
import traceback
import uuid
from collections.abc import Callable, Iterable, Iterator
from http import HTTPStatus

from .errors import KiraError
from .media import utc_now

MAX_FINISHED = 50
DOWNLOAD_WORKERS = 6


def ordered_parallel(
    items: Iterable,
    work: Callable,
    workers: int = DOWNLOAD_WORKERS,
    cancelled: threading.Event | None = None,
    discard: Callable | None = None,
) -> Iterator[tuple[object, object, BaseException | None]]:
    """Run ``work(item)`` on several threads; yield ``(item, result, error)`` in the original order.

    Only a small window runs ahead of the consumer, so a slow consumer (e.g. ingest) bounds how many
    downloads sit staged on disk, and cancelling stops new work from starting. Results that finish
    after the consumer stops are passed to ``discard`` so staged files are not left behind.
    """
    pending: list[tuple[object, concurrent.futures.Future]] = []
    iterator = iter(items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kira-download") as pool:
        try:
            while True:
                while len(pending) < workers * 2 and not (cancelled and cancelled.is_set()):
                    item = next(iterator, StopIteration)
                    if item is StopIteration:
                        break
                    pending.append((item, pool.submit(work, item)))
                if not pending:
                    return
                item, future = pending.pop(0)
                try:
                    yield item, future.result(), None
                except Exception as exc:  # noqa: BLE001 - handed to the caller to report per item
                    yield item, None, exc
        finally:
            for _, future in pending:
                if not future.cancel() and discard:
                    try:
                        discard(future.result())
                    except Exception:  # noqa: BLE001 - the item already failed; nothing to clean up
                        pass


class Task:
    def __init__(self, kind: str, title: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.state = "running"
        self.done = 0
        self.total = 0
        self.current = ""
        self.result: object = None
        self.error = ""
        self.started_at = utc_now()
        self.finished_at = ""
        self.cancelled = threading.Event()

    def progress(self, done: int, total: int, current: str = "") -> None:
        self.done, self.total, self.current = done, total, current

    def view(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "current": self.current,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class TaskRunner:
    """Runs one task of each kind at a time; starting a running kind returns the running task."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tasks: dict[str, Task] = {}

    def start(self, kind: str, title: str, work: Callable[[Task], object], *, exclusive: bool = True) -> dict:
        with self.lock:
            if exclusive:
                for task in self.tasks.values():
                    if task.kind == kind and task.state == "running":
                        return task.view()
            task = Task(kind, title)
            self.tasks[task.id] = task
            finished = [item for item in self.tasks.values() if item.state != "running"]
            for item in sorted(finished, key=lambda item: item.started_at)[:-MAX_FINISHED]:
                self.tasks.pop(item.id, None)
        threading.Thread(target=self._run, args=(task, work), name=f"kira-{kind}", daemon=True).start()
        return task.view()

    @staticmethod
    def _run(task: Task, work: Callable[[Task], object]) -> None:
        try:
            task.result = work(task)
            task.state = "cancelled" if task.cancelled.is_set() else "done"
        except KiraError as exc:
            task.state, task.error = "failed", exc.message
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as the task error
            traceback.print_exc()
            task.state, task.error = "failed", str(exc) or exc.__class__.__name__
        finally:
            task.finished_at = utc_now()

    def get(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise KiraError(HTTPStatus.NOT_FOUND, "Task not found")
        return task.view()

    def cancel(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise KiraError(HTTPStatus.NOT_FOUND, "Task not found")
        task.cancelled.set()
        return task.view()

    def list(self) -> list[dict]:
        with self.lock:
            tasks = list(self.tasks.values())
        return [task.view() for task in sorted(tasks, key=lambda item: item.started_at, reverse=True)]

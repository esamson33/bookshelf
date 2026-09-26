"""Background work, and the event stream that reports it.

A title lookup takes tens of seconds — an OpenAI call that searches the web —
which the CLI handled by simply blocking a terminal. A browser cannot: the
request would sit there until it timed out, showing nothing. So the work runs
on a thread and its progress is pushed to the page instead.

The progress text itself is not new. `Context.out` is already the seam every
plan builder writes through ("Looking up titles for N new file(s)…",
SummaryPlan's per-batch line); handing a job's `report` to a plan in place of
`print` is all it takes to move those lines from a terminal to a browser.

One job runs at a time. Pending proposals are global state keyed by directory,
so two concurrent lookups would race to stage rows over each other.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Callable

#: Dropped events are worse than a slow client, but an abandoned connection
#: must not grow without bound. Deep enough that no real job comes close.
QUEUE_LIMIT = 1000


@dataclass
class Job:
    """One unit of background work, and everything the UI shows about it."""

    id: int
    kind: str                                        # shelve | rename | summaries
    state: str = "running"                           # running | done | error
    lines: list[str] = field(default_factory=list)   # what the CLI used to print
    result: dict | None = None
    error: str | None = None

    def snapshot(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "state": self.state,
            "lines": list(self.lines), "result": self.result, "error": self.error,
        }


class Busy(Exception):
    """A job is already running; the caller answers 409."""


class JobManager:
    """Runs one job at a time and fans its progress out to subscribers.

    Subscribers are plain queues — one per open event-stream connection. A
    subscriber that stops draining loses events rather than blocking the job.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._next_id = 1
        self._current: Job | None = None
        self._subscribers: list[queue.Queue] = []

    # ── jobs ─────────────────────────────────────────────────────────────

    def current(self) -> Job | None:
        with self._lock:
            return self._current

    def snapshot(self) -> dict | None:
        job = self.current()
        return job.snapshot() if job else None

    def reset(self) -> None:
        """Forget the last job and drop every subscriber.

        Only for tests: the manager is a process-wide singleton, so without
        this one test's finished job is still `current()` in the next.
        """
        with self._lock:
            self._current = None
            self._subscribers.clear()

    def start(self, kind: str, fn: Callable[[Callable[..., None]], dict | None]) -> Job:
        """Run `fn(report)` on a daemon thread. Raises Busy if one is running.

        `fn` receives a print-shaped `report` callable to pass as `Context.out`
        (or to assign to `Plan.out`); whatever dict it returns becomes the
        job's result.
        """
        with self._lock:
            if self._current is not None and self._current.state == "running":
                raise Busy(f"A {self._current.kind} job is already running.")
            job = Job(id=self._next_id, kind=kind)
            self._next_id += 1
            self._current = job

        self._emit(job)
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return job

    def _run(self, job: Job, fn) -> None:
        def report(*args) -> None:
            line = " ".join(str(a) for a in args).rstrip()
            if not line:
                return                      # the CLI's spacing blank lines
            with self._lock:
                job.lines.append(line)
            self._emit(job, line=line)

        try:
            job.result = fn(report) or {}
            job.state = "done"
        except Exception as e:                        # noqa: BLE001 — reported, not raised
            job.state = "error"
            job.error = f"{type(e).__name__}: {e}"
        self._emit(job)

    # ── event stream ─────────────────────────────────────────────────────

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: dict) -> None:
        """Fan an event out to every open stream."""
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass                        # a stalled reader loses events, not the job

    def _emit(self, job: Job, **extra) -> None:
        self.publish({"type": "job", **job.snapshot(), **extra})


#: The one manager. Unlike the Catalog it needs no config, so it is built at
#: import time and shared by every handler thread.
JOBS = JobManager()

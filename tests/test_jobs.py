"""The background job registry and its event fan-out.

Nothing here touches the network or a plan — a job is just "run this callable
on a thread and narrate it", and these tests pin that contract down.
"""

import queue
import threading
import unittest

from bookshelf.jobs import JOBS, Busy, Job, JobManager


class JobManagerTest(unittest.TestCase):
    def setUp(self):
        self.jobs = JobManager()

    def drain(self, q):
        out = []
        while True:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                return out

    def run_to_completion(self, kind, fn):
        done = threading.Event()

        def wrapped(report):
            try:
                return fn(report)
            finally:
                done.set()

        job = self.jobs.start(kind, wrapped)
        self.assertTrue(done.wait(5), "job never ran")
        # The thread sets state after fn returns; wait for it to settle.
        for _ in range(500):
            if job.state != "running":
                break
            threading.Event().wait(0.01)
        return job

    # ── lifecycle ────────────────────────────────────────────────────────

    def test_a_job_runs_and_keeps_its_result(self):
        job = self.run_to_completion("shelve", lambda report: {"moved": 3})
        self.assertEqual(job.state, "done")
        self.assertEqual(job.result, {"moved": 3})
        self.assertIsNone(job.error)

    def test_a_job_returning_nothing_still_gets_a_result(self):
        job = self.run_to_completion("dedupe", lambda report: None)
        self.assertEqual(job.result, {})

    def test_reported_lines_are_kept_in_order(self):
        def work(report):
            report("Looking up titles for 3 new file(s)…")
            report("  Batch", 1, "of 2")
            return {}

        job = self.run_to_completion("summaries", work)
        self.assertEqual(job.lines,
                         ["Looking up titles for 3 new file(s)…", "  Batch 1 of 2"])

    def test_blank_reports_are_dropped(self):
        """Plans print spacing blank lines for a terminal; a log doesn't need them."""
        job = self.run_to_completion("shelve", lambda report: report("") or report("   "))
        self.assertEqual(job.lines, [])

    def test_a_raising_job_is_captured_not_propagated(self):
        def boom(report):
            raise RuntimeError("no API key")

        job = self.run_to_completion("shelve", boom)
        self.assertEqual(job.state, "error")
        self.assertEqual(job.error, "RuntimeError: no API key")

    def test_only_one_job_runs_at_a_time(self):
        release = threading.Event()
        self.jobs.start("shelve", lambda report: release.wait(5))
        with self.assertRaises(Busy):
            self.jobs.start("summaries", lambda report: None)
        release.set()

    def test_a_finished_job_frees_the_slot(self):
        self.run_to_completion("shelve", lambda report: {})
        second = self.run_to_completion("summaries", lambda report: {})
        self.assertEqual(second.kind, "summaries")
        self.assertEqual(second.state, "done")

    def test_ids_increment(self):
        first = self.run_to_completion("shelve", lambda report: {})
        second = self.run_to_completion("shelve", lambda report: {})
        self.assertEqual(second.id, first.id + 1)

    def test_snapshot_is_a_copy(self):
        job = Job(id=1, kind="shelve", lines=["a"])
        snap = job.snapshot()
        job.lines.append("b")
        self.assertEqual(snap["lines"], ["a"])

    # ── event fan-out ────────────────────────────────────────────────────

    def test_every_subscriber_sees_a_published_event(self):
        a, b = self.jobs.subscribe(), self.jobs.subscribe()
        self.jobs.publish({"type": "books"})
        self.assertEqual(self.drain(a), [{"type": "books"}])
        self.assertEqual(self.drain(b), [{"type": "books"}])

    def test_unsubscribing_stops_delivery(self):
        q = self.jobs.subscribe()
        self.jobs.unsubscribe(q)
        self.jobs.publish({"type": "books"})
        self.assertEqual(self.drain(q), [])

    def test_unsubscribing_twice_is_harmless(self):
        q = self.jobs.subscribe()
        self.jobs.unsubscribe(q)
        self.jobs.unsubscribe(q)

    def test_a_job_emits_start_progress_and_finish(self):
        q = self.jobs.subscribe()
        self.run_to_completion("shelve", lambda report: report("working") or {})
        events = self.drain(q)
        self.assertTrue(all(e["type"] == "job" for e in events))
        self.assertEqual([e["state"] for e in events], ["running", "running", "done"])
        self.assertEqual(events[1]["line"], "working")

    def test_a_stalled_subscriber_loses_events_but_not_the_job(self):
        q = self.jobs.subscribe()
        for _ in range(q.maxsize + 5):
            self.jobs.publish({"type": "books"})
        job = self.run_to_completion("shelve", lambda report: {"ok": True})
        self.assertEqual(job.state, "done")
        self.assertEqual(q.qsize(), q.maxsize)


class SharedManagerTest(unittest.TestCase):
    def test_the_module_level_manager_exists(self):
        self.assertIsInstance(JOBS, JobManager)


if __name__ == "__main__":
    unittest.main()

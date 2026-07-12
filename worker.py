"""Background worker skeleton — one per thread, owns its checklist post, writes its result to
thread_jobs on completion. STUB: the body is not implemented yet. Gated by CLEAVIS_THREAD_JOBS.

Design (to fill in):
  - run() is invoked out-of-band (async), as the OWNER (runtimeUserId), for a thread.
  - It posts/owns a single Slack checklist message (the only message it ever updates).
  - It does the work (planned tasks), updating that message; on edit-window failure it re-posts and
    re-points its single message reference.
  - On completion it writes a concise result to thread_jobs.set_result(); the responder picks that up
    on the user's next turn (state-aware, no queue).
"""
from __future__ import annotations

import thread_jobs


def run(thread_ts: str, owner: str, spec: str) -> bool:
    """Claim the thread and run the job. Returns False if a worker is already running for it.
    STUB — real execution (plan → tasks → checklist updates → result) is TODO."""
    if not thread_jobs.claim(thread_ts, owner, spec):
        return False                                   # another worker already owns this thread
    try:
        # TODO: plan the task list; post the checklist message; execute (invoke runtime as `owner`,
        #       per-user tools); update the single checklist message; build a concise result.
        result = "(worker stub — background execution not implemented yet)"
        thread_jobs.set_result(thread_ts, result)
        return True
    except Exception as e:  # noqa: BLE001
        thread_jobs.set_result(thread_ts, f"worker failed: {type(e).__name__}: {e}")
        return False

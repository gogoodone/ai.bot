"""Background-thread job state (DynamoDB).

The responder reads this once per turn to know whether a worker is running for the thread or its
result is ready — that one read is the whole coordination (no locks, no queue). The worker writes its
result here on completion. Gated by CLEAVIS_THREAD_JOBS (default off); when off, nothing calls this.

Record (PK = thread_ts):
  {thread_ts, status: "running"|"done", owner, spec, result?, checklist_ts?, updated}
"""
from __future__ import annotations

import time

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

import config

_deser = TypeDeserializer()
_ser = TypeSerializer()  # noqa: F841 — reserved for richer writes later


def _key(thread_ts: str) -> dict:
    return {"thread_ts": {"S": thread_ts}}


def get(thread_ts: str) -> dict | None:
    """The thread's job record, or None. Never raises (coordination must not break a reply)."""
    if not thread_ts:
        return None
    try:
        it = config.ddb.get_item(TableName=config.THREAD_JOBS_TABLE, Key=_key(thread_ts),
                                 ConsistentRead=True).get("Item")
        return {k: _deser.deserialize(v) for k, v in it.items()} if it else None
    except Exception as e:  # noqa: BLE001
        print(f"[thread_jobs] get failed: {e}", flush=True)
        return None


def claim(thread_ts: str, owner: str, spec: str = "") -> bool:
    """Start a worker for this thread IFF one isn't already running — OR the running one is stale
    (older than the lease, i.e. hung/crashed). Atomic. True if claimed."""
    now = time.time()
    stale = now - config.THREAD_JOB_LEASE_SECS
    try:
        config.ddb.update_item(
            TableName=config.THREAD_JOBS_TABLE, Key=_key(thread_ts),
            UpdateExpression="SET #s=:r, #o=:o, spec=:sp, updated=:u REMOVE #res",
            ConditionExpression="attribute_not_exists(#s) OR #s <> :r OR #u < :stale",
            ExpressionAttributeNames={"#s": "status", "#o": "owner", "#res": "result", "#u": "updated"},
            ExpressionAttributeValues={":r": {"S": "running"}, ":o": {"S": owner},
                                       ":sp": {"S": spec[:2000]}, ":u": {"N": f"{now:.0f}"},
                                       ":stale": {"N": f"{stale:.0f}"}})
        return True
    except config.ddb.exceptions.ConditionalCheckFailedException:
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[thread_jobs] claim failed: {e}", flush=True)
        return False


def set_result(thread_ts: str, result: str) -> None:
    """Worker writes its final result + flips status to done."""
    try:
        config.ddb.update_item(
            TableName=config.THREAD_JOBS_TABLE, Key=_key(thread_ts),
            UpdateExpression="SET #s=:d, #res=:r, updated=:u",
            ExpressionAttributeNames={"#s": "status", "#res": "result"},
            ExpressionAttributeValues={":d": {"S": "done"}, ":r": {"S": result[:30000]},
                                       ":u": {"N": f"{time.time():.0f}"}})
    except Exception as e:  # noqa: BLE001
        print(f"[thread_jobs] set_result failed: {e}", flush=True)


def context_note(thread_ts: str) -> str | None:
    """A short line injected into the responder's prompt so it answers state-aware. None if no job."""
    job = get(thread_ts)
    if not job:
        return None
    if job.get("status") == "running":
        age = time.time() - float(job.get("updated", 0))
        if age > config.THREAD_JOB_LEASE_SECS:        # hung/stale worker
            return ("[A background task for this thread has been running unusually long and may be "
                    "stuck — tell the user it's taking longer than expected; a fresh attempt can be "
                    "started if they ask.]")
        return ("[IMPORTANT: the user has an EARLIER task still running in this thread. You MUST begin "
                "your reply by telling them that task is still in progress (one short sentence), THEN "
                "address their new message. Do NOT start another task for it.]")
    if job.get("status") == "done" and job.get("result"):
        return f"[A background task for this thread finished. Use its result:\n{job['result']}\n]"
    return None

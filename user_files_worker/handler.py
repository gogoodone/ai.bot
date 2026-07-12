"""Heavy upload worker (container Lambda) for per-user uploaded files.

Async-invoked by the responder once per upload batch with {user_id, file_ids, channel_id, thread_ts}.
For each staged file it does the slow work the responder offloaded: read the raw from the upload
bucket → markitdown convert → store the md in the user's KB prefix. Then it triggers ONE Bedrock
ingestion job for the batch, waits for it to finish embedding, and posts a single 'searchable now'
confirmation into the upload thread.

It reuses the runtime's user_files module (bundled into the image) so conversion + indexing logic lives
in exactly one place. Identity/trust: it only touches rows under the user_id it was handed and posts to
the channel/thread stored on those rows — never takes identity from a model.
"""
import json
import time
import urllib.error
import urllib.request

import config
import user_files

_token = {"v": None}


def _bot_token() -> str:
    if _token["v"] is None:
        _token["v"] = config.ssm.get_parameter(Name=config.BOT_TOKEN_PARAM,
                                                WithDecryption=True)["Parameter"]["Value"]
    return _token["v"]


def _slack(method: str, payload: dict) -> None:
    """Call a Slack chat.* method with a GFM-markdown block. Honors 429 (waits Retry-After). Never raises."""
    payload = {**payload, "text": payload.get("text", "")[:2900],
               "blocks": [{"type": "markdown", "text": payload.get("text", "")[:11000]}]}
    req = urllib.request.Request(f"https://slack.com/api/{method}", data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "Authorization": f"Bearer {_bot_token()}"})
    for attempt in range(4):
        try:
            urllib.request.urlopen(req, timeout=10).read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = int((e.headers.get("Retry-After") or "1")) if e.headers else 1
                time.sleep(min(max(wait, 1), 30))
                continue
            print(f"[worker] slack {method} failed: {e}", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[worker] slack {method} failed: {e}", flush=True)
            return


def _report(channel: str, thread_ts: str, ack_ts: str, text: str) -> None:
    """Edit the responder's ack message in place (the 'indexing…' → final-status transition). Falls back
    to a fresh threaded post if there's no ack to edit (e.g. the upload came with a question)."""
    if not channel:
        return
    if ack_ts:
        _slack("chat.update", {"channel": channel, "ts": ack_ts, "text": text})
    elif thread_ts:
        _slack("chat.postMessage", {"channel": channel, "thread_ts": thread_ts, "text": text})


def handler(event, context):
    user_id = event.get("user_id")
    file_ids = event.get("file_ids") or []
    channel = event.get("channel_id", "")
    thread_ts = event.get("thread_ts", "")
    ack_ts = event.get("ack_ts", "")
    if not (user_id and file_ids):
        print(f"[worker] bad event: {event}", flush=True)
        return {"ok": False}

    new, dup, failed = [], [], []
    for fid in file_ids:
        try:
            r = user_files.process(user_id, fid)         # reads the file ONCE: hash→dedup→convert
        except Exception as e:  # noqa: BLE001
            print(f"[worker] process failed {user_id}/{fid}: {e}", flush=True)
            r = {"status": "failed", "filename": fid}
        {"ingested": new, "duplicate": dup}.get(r.get("status"), failed).append(r.get("filename", fid))

    if not (new or dup):
        _report(channel, thread_ts, ack_ts, "⚠️ I couldn't process your upload — please try again.")
        return {"ok": False, "failed": failed}

    ok = True
    if new:                                              # only index when there's something new
        user_files.trigger_ingest(user_id)
        ok = user_files.wait_indexed(user_id)

    parts = []
    if new:
        pretty = ", ".join(f"*{n}*" for n in new)
        v = "is" if len(new) == 1 else "are"
        it = "it" if len(new) == 1 else "them"
        parts.append(f"✅ {pretty} {v} indexed and searchable now. Ask me anything about {it} — "
                     f"or say \"what files do I have?\" to see everything." if ok else
                     f"⏳ {pretty} {v} taking a little longer to index. Searchable shortly — "
                     f"try your question again in a moment.")
    if dup:
        pretty = ", ".join(f"*{n}*" for n in dup)
        v = "it's" if len(dup) == 1 else "they're"
        parts.append(f"📎 You already had {pretty} — {v} already in your files, nothing to add.")
    _report(channel, thread_ts, ack_ts, "\n\n".join(parts))
    print(f"[worker] {user_id}: new={new} dup={dup} complete={ok} failed={failed}", flush=True)
    return {"ok": True, "indexed": ok, "new": new, "dup": dup}

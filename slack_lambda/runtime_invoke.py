"""InvokeAgentRuntime adapter for the cleavis.ba Strands runtime — same surface as cleavis.ai's
harness_invoke, so the (copied) Slack handler calls it identically.

Difference from harness_invoke: invokes a custom AgentCore Runtime (Strands) via
invoke_agent_runtime with a JSON payload {prompt, session_id, actor_id} and reads back the
agent's {text, usage} (incl. cache tokens). The Strands runtime builds its OWN system prompt
(secret + skills at cold start), so any system_prompt passed here is intentionally ignored.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import ParamValidationError

_CFG = Config(retries={"max_attempts": 2, "mode": "adaptive"}, read_timeout=240, connect_timeout=10)


@dataclass(frozen=True)
class HarnessAnswer:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    files: tuple = ()  # [{key,name}] of files the agent delivered (S3 uploads bucket)


def client(region: str):
    return boto3.client("bedrock-agentcore", region_name=region, config=_CFG)


def make_session_id(user: str, channel: str, thread_ts: str) -> str:
    """Same scheme as harness_invoke so the same Slack thread maps to the same session."""
    raw = re.sub(r"[^a-zA-Z0-9_-]", "-", f"{user}-{channel}-{thread_ts}")
    if len(raw) < 33:
        raw = (raw + "-pad" * 9)[:33]
    return raw[:100]


def invoke(*, client, harness_arn, session_id, user_id, text="", command=None,
           channel_id=None, thread_ts=None, uploads=None):
    # harness_arn carries the runtime ARN here. The runtime owns budget/commands/KB-access and builds
    # its own system prompt — the lambda just forwards the verified actor + text (or a command).
    # channel_id/thread_ts let the runtime set the Slack thread status DIRECTLY per tool (real-time).
    body_obj = {"prompt": text, "session_id": session_id, "actor_id": user_id,
                "channel_id": channel_id or "", "thread_ts": thread_ts or ""}
    if command:
        body_obj["command"] = command                # {"type": "cost"|"forget"|"help", "text": ...}
    if uploads:
        body_obj["uploads"] = uploads                # [{name,key}] staged in UPLOAD_BUCKET → persist to the user KB
    payload = json.dumps(body_obj).encode()
    # runtimeUserId binds the invocation to this Slack user so AgentCore injects a per-user workload
    # access token into the runtime context — required for per-user OAuth (AgentCore Identity) to the
    # external MCPs. The Lambda's bundled botocore can be older and reject runtimeUserId, so fall back
    # without it (per-user MCPs just won't work until botocore is bundled).
    kw = dict(agentRuntimeArn=harness_arn, runtimeSessionId=session_id, payload=payload, qualifier="DEFAULT")
    try:
        resp = client.invoke_agent_runtime(runtimeUserId=user_id, **kw)
    except (TypeError, ParamValidationError):
        print("runtime_invoke: botocore rejected runtimeUserId — invoking without it", flush=True)
        resp = client.invoke_agent_runtime(**kw)
    raw = resp["response"]
    body = raw.read() if hasattr(raw, "read") else b"".join(raw)
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001 — non-JSON body: surface as text
        return HarnessAnswer(text=(body.decode(errors="replace").strip() or "(empty answer)"))
    u = d.get("usage") or {}
    i, o = int(u.get("input", 0)), int(u.get("output", 0))
    # "silent": the runtime already posted to Slack itself (e.g. the upload ack) — return empty so the
    # handler doesn't double-post.
    text = "" if d.get("silent") else (str(d.get("text") or "").strip() or "(empty answer)")
    return HarnessAnswer(
        text=text,
        input_tokens=i, output_tokens=o, total_tokens=i + o,
        cache_read_tokens=int(u.get("cache_read", 0)),
        cache_write_tokens=int(u.get("cache_write", 0)),
        files=tuple(d.get("files") or ()),
    )

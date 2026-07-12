"""Slack -> Runtime adapter Lambda (thin).

Responsibilities (Slack I/O only — all business logic lives in the runtime):
  Stage 1 (HTTP): verify HMAC signature, drop retries/bot/noise, ack within 3s.
  Stage 2 (async relay): cheap silent access-gate, stage attachments, invoke the runtime,
                         post the answer + any files to the thread.
  Slash commands: ack immediately, then invoke the runtime and post the reply to response_url.

The runtime owns budget (caps/charge/report), the /cost /forget /help logic, KB-access, and the
agent. The lambda keeps ONLY: signature verification (the trust boundary), a cheap pre-invoke
gate (so unauthorized channel chatter never reaches the runtime), and Slack plumbing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config

from runtime_invoke import client as runtime_client, invoke, make_session_id

REGION = os.environ.get("AWS_REGION", "eu-central-1")
RUNTIME_ARN = os.environ["CLEAVIS_RUNTIME_ARN"]
BOT_TOKEN_PARAM = os.environ["CLEAVIS_SLACK_BOT_TOKEN_PARAM"]
SIGNING_SECRET_PARAM = os.environ["CLEAVIS_SLACK_SIGNING_SECRET_PARAM"]
BOT_ID = os.environ["CLEAVIS_BOT_ID"]                            # access-registry row key (DynamoDB)
REGISTRY_TABLE = os.environ.get("CLEAVIS_REGISTRY_TABLE", "cleavis_access_registry")
UPLOAD_BUCKET = os.environ.get("CLEAVIS_UPLOAD_BUCKET")
# Voice: a synchronous transcription worker (faster-whisper, model baked into the image) turns an audio
# clip into text HERE, so the runtime stays audio-agnostic. Invoked RequestResponse (the transcript IS
# the prompt). Disabled if the env var is unset.
TRANSCRIBE_FN = os.environ.get("CLEAVIS_TRANSCRIBE_FN", "cleavis-transcribe-worker")
_AUDIO_EXT = (".m4a", ".mp3", ".wav", ".ogg", ".oga", ".opus", ".webm", ".aac", ".flac", ".mp4", ".amr")

_retry = Config(retries={"max_attempts": 4, "mode": "adaptive"}, connect_timeout=10)
_ssm = boto3.client("ssm", region_name=REGION, config=_retry)
_ddb = boto3.client("dynamodb", region_name=REGION, config=_retry)
_deser = TypeDeserializer()
_lambda = boto3.client("lambda", region_name=REGION, config=_retry)
_s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                   config=_retry.merge(Config(s3={"addressing_style": "virtual"}, signature_version="s3v4")))
_rc = runtime_client(REGION)

_ssm_cache: dict[str, str] = {}
_reg_cache: dict[str, Any] = {"ts": 0.0, "val": None}
_bot_uid: dict[str, str | None] = {"v": None}
_REG_TTL = 300
_SLACK_API = "https://slack.com/api"


def _ssm_value(name: str) -> str:
    if name not in _ssm_cache:
        _ssm_cache[name] = _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    return _ssm_cache[name]


def _registry() -> dict[str, Any]:
    """This bot's access row from DynamoDB (cached). Only bot_users + allowed_email_domains are
    needed for the cheap pre-gate; the runtime re-checks authoritatively."""
    now = time.time()
    if _reg_cache["val"] is None or now - _reg_cache["ts"] > _REG_TTL:
        item = _ddb.get_item(TableName=REGISTRY_TABLE, Key={"bot": {"S": BOT_ID}}).get("Item") or {}
        reg = _deser.deserialize(item["config"]) if "config" in item else {}
        reg["bot_users"] = list(item.get("bot_users", {}).get("SS", []))
        _reg_cache["val"] = reg
        _reg_cache["ts"] = now
    return _reg_cache["val"]


def _verify(headers: dict[str, str], body: str) -> bool:
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts or not sig or abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:{body}".encode()
    expected = "v0=" + hmac.new(_ssm_value(SIGNING_SECRET_PARAM).encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _bot_user_id() -> str:
    if _bot_uid["v"] is None:
        try:
            req = urllib.request.Request(f"{_SLACK_API}/auth.test", data=b"", method="POST",
                                         headers={"Authorization": f"Bearer {_ssm_value(BOT_TOKEN_PARAM)}"})
            _bot_uid["v"] = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("user_id") or ""
        except Exception as e:  # noqa: BLE001
            print(f"auth.test failed: {e}", flush=True)
            _bot_uid["v"] = ""
    return _bot_uid["v"]


def _resolve_email(user_id: str) -> str | None:
    qs = urllib.parse.urlencode({"user": user_id})
    req = urllib.request.Request(f"{_SLACK_API}/users.profile.get?{qs}",
                                 headers={"Authorization": f"Bearer {_ssm_value(BOT_TOKEN_PARAM)}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return (json.loads(r.read()).get("profile") or {}).get("email")
    except Exception as e:  # noqa: BLE001
        print(f"resolve_email failed for {user_id}: {e}", flush=True)
        return None


def _gate(user_id: str) -> bool:
    """Cheap silent pre-filter so unauthorized chatter never reaches the runtime. (The runtime
    independently re-checks — this is just to avoid wasted invokes and channel noise.)"""
    reg = _registry()
    if user_id not in (reg.get("bot_users") or []):
        return False
    domains = [d.lower() for d in (reg.get("allowed_email_domains") or [])]
    if domains:
        email = _resolve_email(user_id)
        dom = email.rsplit("@", 1)[1].lower() if email and "@" in email else None
        if dom not in domains:
            return False
    return True


# --- Slack output helpers ---------------------------------------------------
def _tables_to_codeblock(text: str) -> str:
    lines = text.split("\n"); out: list[str] = []; i = 0
    sep = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
    cells = lambda r: [c.strip() for c in r.strip().strip("|").split("|")]  # noqa: E731
    while i < len(lines):
        if (lines[i].strip().startswith("|") and i + 1 < len(lines)
                and sep.match(lines[i + 1]) and "|" in lines[i + 1]):
            header = cells(lines[i]); i += 2; rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(cells(lines[i])); i += 1
            allr = [header] + rows
            n = max(len(r) for r in allr)
            allr = [r + [""] * (n - len(r)) for r in allr]
            w = [max(len(r[c]) for r in allr) for c in range(n)]
            _r = lambda cs: "│ " + " │ ".join(cs[c].ljust(w[c]) for c in range(n)) + " │"  # noqa: E731
            bar = lambda l, m, r: l + m.join("─" * (w[c] + 2) for c in range(n)) + r  # noqa: E731
            out += ["```", bar("┌", "┬", "┐"), _r(header), bar("├", "┼", "┤")]
            out += [_r(row) for row in rows] + [bar("└", "┴", "┘"), "```"]
        else:
            out.append(lines[i]); i += 1
    return "\n".join(out)


def _md_to_mrkdwn(t: str) -> str:
    """Deterministic GitHub-markdown -> Slack mrkdwn (plain-text fallback only)."""
    if not t:
        return t
    parts = re.split(r"(```.*?```)", t, flags=re.S)
    for i in range(0, len(parts), 2):
        s = parts[i]
        s = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*$", r"*\1*", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"*\1*", s)
        s = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", s)
        parts[i] = _tables_to_codeblock(s)
    return "".join(parts)


def _slack(method: str, payload: dict[str, Any]) -> None:
    req = urllib.request.Request(f"{_SLACK_API}/{method}", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "Authorization": f"Bearer {_ssm_value(BOT_TOKEN_PARAM)}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def _slack_post(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        req = urllib.request.Request(f"{_SLACK_API}/chat.postMessage", data=json.dumps(payload).encode(),
                                     method="POST", headers={"Content-Type": "application/json; charset=utf-8",
                                     "Authorization": f"Bearer {_ssm_value(BOT_TOKEN_PARAM)}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


_MD_LIMIT = 11800


def _md_chunks(md: str) -> list[str]:
    md = (md or "").strip()
    if len(md) <= _MD_LIMIT:
        return [md] if md else []
    units, parts = [], re.split(r"(```.*?```)", md, flags=re.S)
    for i, p in enumerate(parts):
        if i % 2 == 1:
            units.append(p)
        else:
            units += [u for u in re.split(r"(\n\s*\n)", p) if u]
    chunks, cur = [], ""
    for u in units:
        if len(u) > _MD_LIMIT:
            if cur.strip():
                chunks.append(cur); cur = ""
            chunks += [u[j:j + _MD_LIMIT] for j in range(0, len(u), _MD_LIMIT)]
        elif len(cur) + len(u) > _MD_LIMIT:
            chunks.append(cur); cur = u
        else:
            cur += u
    if cur.strip():
        chunks.append(cur)
    return [c.strip("\n") for c in chunks if c.strip()]


def _post_answer(channel: str, thread_ts: str, md: str) -> None:
    """Post raw GFM as Slack markdown blocks — the block renders real GFM tables (provided each table
    has a blank line before it), code, links, bold. Plain-mrkdwn fallback if the block post fails."""
    for ch in (_md_chunks(md) or [""]):
        resp = _slack_post({"channel": channel, "thread_ts": thread_ts, "text": ch[:2900],
                            "blocks": [{"type": "markdown", "text": ch}],
                            "unfurl_links": False, "unfurl_media": False})
        if not resp.get("ok"):
            _slack("chat.postMessage", {"channel": channel, "thread_ts": thread_ts,
                   "text": _md_to_mrkdwn(ch), "unfurl_links": False, "unfurl_media": False})


def _set_status(channel: str, thread_ts: str, status: str) -> None:
    try:
        _slack("assistant.threads.setStatus", {"channel_id": channel, "thread_ts": thread_ts, "status": status})
    except Exception as e:  # noqa: BLE001
        print(f"setStatus skipped: {e}", flush=True)


def _stage_files(files: list[dict[str, Any]], session_id: str) -> list[tuple[str, str, str]]:
    """Slack attachments -> per-session S3 prefix -> (name, presigned GET URL for the CI, S3 key).
    The URL feeds the in-turn CI (which has no S3 role); the key lets the runtime persist the file into
    the user's personal knowledge base."""
    out: list[tuple[str, str, str]] = []
    if not files or not UPLOAD_BUCKET:
        return out
    tok = _ssm_value(BOT_TOKEN_PARAM)
    for f in files:
        name = f.get("name") or f.get("id")
        url = f.get("url_private_download") or f.get("url_private")
        if not name or not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            key = f"{session_id}/{name}"
            _s3.put_object(Bucket=UPLOAD_BUCKET, Key=key, Body=data)
            out.append((name, _s3.generate_presigned_url("get_object",
                       Params={"Bucket": UPLOAD_BUCKET, "Key": key}, ExpiresIn=900), key))
        except Exception as e:  # noqa: BLE001
            print(f"stage file {name} failed: {e}", flush=True)
    return out


def _slack_upload(channel: str, thread_ts: str, key: str, name: str) -> None:
    try:
        data = _s3.get_object(Bucket=UPLOAD_BUCKET, Key=key)["Body"].read()
        tok = _ssm_value(BOT_TOKEN_PARAM)
        q = urllib.parse.urlencode({"filename": name, "length": len(data)}).encode()
        g = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{_SLACK_API}/files.getUploadURLExternal", data=q, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Bearer {tok}"}),
            timeout=20).read())
        if not g.get("ok"):
            print(f"getUploadURLExternal failed: {g}", flush=True); return
        boundary = "----cleavis" + uuid.uuid4().hex
        mp = ((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
               "Content-Type: application/octet-stream\r\n\r\n").encode() + data
              + f"\r\n--{boundary}--\r\n".encode())
        urllib.request.urlopen(urllib.request.Request(g["upload_url"], data=mp, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}), timeout=120).read()
        body = json.dumps({"files": [{"id": g["file_id"], "title": name}],
                           "channel_id": channel, "thread_ts": thread_ts}).encode()
        c = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{_SLACK_API}/files.completeUploadExternal", data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {tok}"}),
            timeout=30).read())
        if not c.get("ok"):
            print(f"completeUploadExternal failed: {c}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"slack_upload failed for {name}: {e}", flush=True)


def _respond_url(response_url: str, text: str) -> None:
    """Post a delayed slash-command reply to Slack's response_url (ephemeral, GFM markdown block)."""
    try:
        body = json.dumps({"response_type": "ephemeral", "text": text[:2900],
                           "blocks": [{"type": "markdown", "text": text}]}).encode()
        urllib.request.urlopen(urllib.request.Request(response_url, data=body, method="POST",
            headers={"Content-Type": "application/json"}), timeout=10).read()
    except Exception as e:  # noqa: BLE001
        print(f"response_url post failed: {e}", flush=True)


# ---------------------------------------------------------------------------
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("_relay"):
        return _do_work(event)
    if event.get("_slash"):
        return _do_slash(event)

    body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not _verify(headers, body):
        return {"statusCode": 401, "body": "bad signature"}

    # Slash commands: ack immediately, do the work async, post to response_url.
    if "application/x-www-form-urlencoded" in headers.get("content-type", ""):
        form = urllib.parse.parse_qs(body)
        _lambda.invoke(FunctionName=context.function_name, InvocationType="Event",
                       Payload=json.dumps({"_slash": True,
                                           "command": (form.get("command", [""])[0] or "").lstrip("/"),
                                           "text": form.get("text", [""])[0] or "",
                                           "user": form.get("user_id", [""])[0],
                                           "response_url": form.get("response_url", [""])[0]}).encode())
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"response_type": "ephemeral", "text": "On it… ⏳"})}

    payload = json.loads(body) if body else {}
    if payload.get("type") == "url_verification":
        return {"statusCode": 200, "body": payload.get("challenge", "")}
    if headers.get("x-slack-retry-num"):
        return {"statusCode": 200, "body": "retry-ignored"}
    if payload.get("type") != "event_callback":
        return {"statusCode": 200, "body": "ignored"}

    ev = payload.get("event") or {}
    if ev.get("bot_id") or ev.get("subtype") in {
            "message_changed", "message_deleted", "channel_join", "bot_message"}:
        return {"statusCode": 200, "body": "ignored-bot-or-subtype"}
    if ev.get("type") not in {"message", "app_mention"} or not ev.get("user") or not ev.get("channel"):
        return {"statusCode": 200, "body": "ignored"}
    if not ev.get("text") and not ev.get("files"):
        return {"statusCode": 200, "body": "ignored-empty"}

    _lambda.invoke(FunctionName=context.function_name, InvocationType="Event",
                   Payload=json.dumps({"_relay": True, "ev": ev}).encode())
    return {"statusCode": 200, "body": "ack"}


def _do_slash(event: dict[str, Any]) -> dict[str, Any]:
    """Async slash relay: gate, invoke the runtime with the command, post reply to response_url."""
    user, cmd = event.get("user", ""), (event.get("command", "") or "").lower()
    text, response_url = event.get("text", ""), event.get("response_url", "")
    if not _gate(user):
        _respond_url(response_url, "You're not authorised to use this assistant.")
        return {"ok": False}
    sid = make_session_id(user, "slash", cmd)
    try:
        ans = invoke(client=_rc, harness_arn=RUNTIME_ARN, session_id=sid, user_id=user,
                     command={"type": cmd, "text": text})
        _respond_url(response_url, ans.text)
    except Exception as e:  # noqa: BLE001
        print(f"slash invoke failed: {e}", flush=True)
        _respond_url(response_url, "Sorry — couldn't run that just now. Try again in a moment.")
    return {"ok": True}


def _do_work(event: dict[str, Any]) -> dict[str, Any]:
    ev = event["ev"]
    user, channel = ev["user"], ev["channel"]
    text = (ev.get("text") or "").strip()
    bot = _bot_user_id()
    if bot:
        text = re.sub(rf"<@{bot}>", "", text).strip()
    thread_ts = ev.get("thread_ts") or ev["ts"]

    if not _gate(user):                               # cheap silent pre-filter
        print(f"gate denied user={user}", flush=True)
        return {"ok": False, "reason": "gate"}

    sid = make_session_id(user, channel, thread_ts)
    staged = _stage_files(ev.get("files") or [], sid)
    # No inline CI-processing of uploads anymore: the runtime converts them to markdown and indexes
    # them in the user's knowledge base. The model answers questions about them via the my_files tool
    # (vector search), not by reading inline.

    # VOICE: transcribe audio clips synchronously HERE (turn voice→text) and fold into the prompt, so the
    # runtime is audio-agnostic. Audio is dropped from `staged` so it never hits the doc-ingest path.
    _audio = [(n, k) for n, _, k in staged if n.lower().endswith(_AUDIO_EXT)]
    if _audio and TRANSCRIBE_FN:
        _set_status(channel, thread_ts, "transcribing your voice message…")
        _spoken = []
        for _name, _key in _audio:
            try:
                r = _lambda.invoke(FunctionName=TRANSCRIBE_FN, InvocationType="RequestResponse",
                                   Payload=json.dumps({"bucket": UPLOAD_BUCKET, "key": _key}).encode())
                body = json.loads(r["Payload"].read() or b"{}")
                if body.get("text"):
                    _spoken.append(body["text"])
                print(f"[voice] {_name}: {str(body.get('text'))[:120]!r}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[voice] transcribe failed {_name}: {e}", flush=True)
        if _spoken:
            _voice = " ".join(_spoken).strip()
            text = (text + "\n" + _voice).strip() if text else _voice
        staged = [(n, u, k) for n, u, k in staged if not n.lower().endswith(_AUDIO_EXT)]  # docs only

    _set_status(channel, thread_ts, "is thinking…")   # runtime then drives per-tool status
    files: tuple = ()
    try:
        ans = invoke(client=_rc, harness_arn=RUNTIME_ARN, session_id=sid, user_id=user, text=text,
                     channel_id=channel, thread_ts=thread_ts,
                     uploads=[{"name": n, "key": k} for n, _, k in staged])
        answer = ans.text
        files = getattr(ans, "files", ()) or ()
    except Exception as e:  # noqa: BLE001
        print(f"invoke failed: {type(e).__name__}: {e}", flush=True)
        answer = ("Sorry — the system is busy right now (the model didn't respond in time). "
                  "Please try again in a moment.")
    finally:
        _set_status(channel, thread_ts, "")

    if answer.strip():                                # empty = runtime already posted (silent ack)
        _post_answer(channel, thread_ts, answer)
    for f in files:
        if f.get("key") and f.get("name"):
            _slack_upload(channel, thread_ts, f["key"], f["name"])
    return {"ok": True}

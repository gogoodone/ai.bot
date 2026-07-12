"""Thin Slack Web API helpers the runtime needs directly.

The runtime talks to Slack for two things only: live per-tool status (the invoke response is
buffered, so status must be pushed out-of-band) and resolving a user id to an email label for
the admin budget report. Everything else Slack-facing stays in the Lambda adapter.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import config


def _urlopen(req, timeout: int = 10, tries: int = 4) -> bytes:
    """urlopen that honors Slack rate limiting: on HTTP 429 it waits Retry-After and retries a few
    times, so a burst of posts gets throttled (slower), not dropped."""
    for attempt in range(tries):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:
                wait = int((e.headers.get("Retry-After") or "1")) if e.headers else 1
                time.sleep(min(max(wait, 1), 30))
                continue
            raise

_token: dict[str, str | None] = {"v": None}
_labels: dict[str, str] = {}
_roster: dict[str, list] = {"v": None}     # cached users.list (warm-container lifetime)


def bot_token() -> str:
    if _token["v"] is None:
        _token["v"] = config.ssm.get_parameter(
            Name=config.BOT_TOKEN_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return _token["v"]


def _get(method: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{config.SLACK_API}/{method}?{qs}",
                                 headers={"Authorization": f"Bearer {bot_token()}"})
    return json.loads(_urlopen(req) or b"{}")


def _post(method: str, payload: dict) -> dict:
    req = urllib.request.Request(f"{config.SLACK_API}/{method}", data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "Authorization": f"Bearer {bot_token()}"})
    return json.loads(_urlopen(req) or b"{}")


def post(channel: str, text: str) -> None:
    """Best-effort post to a channel as a markdown block (used for the error-report channel)."""
    try:
        _post("chat.postMessage", {"channel": channel, "text": text[:2900],
                                   "blocks": [{"type": "markdown", "text": text}]})
    except Exception as e:  # noqa: BLE001
        print(f"post to {channel} failed: {e}", flush=True)


def post_message(channel: str, text: str, thread_ts: str = "") -> str | None:
    """Post a markdown-block message and return its ts (so it can be edited via update_message)."""
    payload = {"channel": channel, "text": text[:2900], "blocks": [{"type": "markdown", "text": text}]}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        return (_post("chat.postMessage", payload) or {}).get("ts")
    except Exception as e:  # noqa: BLE001
        print(f"post_message to {channel} failed: {e}", flush=True)
        return None


def update_message(channel: str, ts: str, text: str) -> bool:
    """Edit a previously posted message in place. False on failure (e.g. edit window closed)."""
    try:
        r = _post("chat.update", {"channel": channel, "ts": ts, "text": text[:2900],
                                  "blocks": [{"type": "markdown", "text": text}]})
        return bool((r or {}).get("ok"))
    except Exception as e:  # noqa: BLE001
        print(f"update_message {ts} failed: {e}", flush=True)
        return False


def set_status(channel: str, thread_ts: str, status: str) -> None:
    """Best-effort assistant thread status bar. Never raises (status is non-critical)."""
    if not (channel and thread_ts and status):
        return
    try:
        body = json.dumps({"channel_id": channel, "thread_ts": thread_ts, "status": status}).encode()
        req = urllib.request.Request(f"{config.SLACK_API}/assistant.threads.setStatus",
                                     data=body, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8",
                                              "Authorization": f"Bearer {bot_token()}"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:  # noqa: BLE001
        pass


def _all_users() -> list:
    """Active human members (cached for the container's life). Needs the users:read scope."""
    if _roster["v"] is None:
        members, cursor = [], None
        try:
            while True:
                p = {"limit": 200}
                if cursor:
                    p["cursor"] = cursor
                r = _get("users.list", p)
                members += r.get("members", []) or []
                cursor = ((r.get("response_metadata") or {}).get("next_cursor") or "").strip()
                if not cursor:
                    break
        except Exception as e:  # noqa: BLE001
            print(f"users.list failed: {e}", flush=True)
        _roster["v"] = members
    return _roster["v"]


def resolve_candidates(name: str) -> list[tuple[str, str]]:
    """All workspace members matching a plain name / email / local-part → [(user_id, label)…].
    Exact matches (on email/local-part/display/real name/handle) take precedence; otherwise all
    substring matches. Empty = none; len>1 = ambiguous (caller should ask which)."""
    q = (name or "").strip().lower().lstrip("@")
    if not q:
        return []
    exact, subs, seen = [], [], set()
    for m in _all_users():
        if m.get("deleted") or m.get("is_bot") or m.get("id") in seen or m.get("id") == "USLACKBOT":
            continue
        prof = m.get("profile") or {}
        email = (prof.get("email") or "").lower()
        fields = [email, email.split("@")[0], (prof.get("display_name") or "").lower(),
                  (prof.get("real_name") or "").lower(), (m.get("name") or "").lower()]
        label = email or prof.get("real_name") or m["id"]
        if any(q == f for f in fields if f):
            seen.add(m["id"]); exact.append((m["id"], label))
        elif any(q in f for f in fields if f):
            seen.add(m["id"]); subs.append((m["id"], label))
    return exact or subs


def resolve_user(name: str) -> tuple[str, str] | None:
    """A single confident match, or None when there's zero or more-than-one. For set-budget-by-name."""
    c = resolve_candidates(name)
    return c[0] if len(c) == 1 else None


def user_label(user_id: str) -> str:
    """Email for a Slack user id (cached); falls back to real name, then the id. For reports."""
    if user_id in _labels:
        return _labels[user_id]
    label = user_id
    try:
        p = _get("users.profile.get", {"user": user_id}).get("profile") or {}
        label = p.get("email") or p.get("real_name") or user_id
    except Exception:  # noqa: BLE001
        pass
    _labels[user_id] = label
    return label

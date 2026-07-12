"""Jira Cloud REST (v3 read / v2 write) over the user's OAuth bearer.

Strict per-user isolation: the bearer is minted by trusted runtime code (external_mcp, keyed to the
verified Slack actor) and passed in here — the model never sees it and the tools expose NO identity,
token, or cloudId parameters. cloudId is derived server-side from the token's accessible-resources.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.atlassian.com"
_UA = "cleavis-ai-agent/1.0"


def _req(url: str, bearer: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Authorization": f"Bearer {bearer}", "Accept": "application/json", "User-Agent": _UA}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else {})


def accessible_sites(bearer: str) -> list[dict]:
    """Sites the user consented to → used to pick cloudId. Empty on failure (logged)."""
    try:
        _, data = _req(f"{API}/oauth/token/accessible-resources", bearer)
        return data if isinstance(data, list) else []
    except urllib.error.HTTPError as e:
        print(f"[jira] accessible-resources HTTP {e.code}: {e.read().decode(errors='replace')[:200]}", flush=True)
        return []
    except Exception as e:  # noqa: BLE001
        print(f"[jira] accessible-resources failed: {e}", flush=True)
        return []


def myself(bearer: str, cloud_id: str) -> dict:
    """GET /myself — the authenticated user (accountId, displayName, emailAddress). The simplest proof
    that the per-user token + REST path work end-to-end."""
    url = f"{API}/ex/jira/{cloud_id}/rest/api/3/myself"
    try:
        _, data = _req(url, bearer)
        return data
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode(errors="replace")[:400]}
    except Exception as e:  # noqa: BLE001
        return {"error": -1, "body": f"{type(e).__name__}: {e}"}


def search(bearer: str, cloud_id: str, jql: str, limit: int = 10) -> dict:
    """JQL search via the current enhanced endpoint (v3 /search/jql)."""
    qs = urllib.parse.urlencode({"jql": jql, "maxResults": limit,
                                 "fields": "summary,status,priority,assignee,updated"})
    url = f"{API}/ex/jira/{cloud_id}/rest/api/3/search/jql?{qs}"
    try:
        _, data = _req(url, bearer)
        return data
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode(errors="replace")[:400]}
    except Exception as e:  # noqa: BLE001
        return {"error": -1, "body": f"{type(e).__name__}: {e}"}

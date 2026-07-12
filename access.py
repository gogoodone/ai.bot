"""Authorization, decided on the VERIFIED Slack user id the Lambda passes in (never the model).

The Lambda verifies the Slack signature and forwards a trusted actor_id; every check here keys
off that id in plain runtime code. The model can only choose a query — it can't widen its KB
scope, bypass the gate, or grant itself admin. Default-deny throughout.
"""
from __future__ import annotations

import time

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

import config
import slack

_cache: dict[str, object] = {"ts": 0.0, "val": None}
_deser = TypeDeserializer()
_ser = TypeSerializer()
_KEY = {"bot": {"S": config.BOT_ID}}


def _load() -> dict:
    """Assemble the registry dict the rest of the module expects from this bot's DDB row:
    bot_users (SS), groups.agent-admin (from admins SS), plus the flattened `config` map
    (kbs, allowed_email_domains, budget, …)."""
    item = config.ddb.get_item(TableName=config.REGISTRY_TABLE, Key=_KEY,
                               ConsistentRead=True).get("Item") or {}
    reg = _deser.deserialize(item["config"]) if "config" in item else {}
    reg["bot_users"] = list(item.get("bot_users", {}).get("SS", []))
    reg["groups"] = {"agent-admin": list(item.get("admins", {}).get("SS", []))}
    return reg


def registry() -> dict:
    """This bot's access registry (DynamoDB row), cached so it isn't re-read every request; writes
    bust the cache, so admin changes via the in-bot commands take effect immediately."""
    now = time.time()
    if _cache["val"] is None or now - float(_cache["ts"]) > config.REGISTRY_TTL:
        _cache["val"] = _load()
        _cache["ts"] = now
    return _cache["val"]  # type: ignore[return-value]


def _bust() -> None:
    _cache["val"] = None
    _cache["ts"] = 0.0


def add_bot_user(uid: str) -> None:
    """Grant bot access (atomic set-add)."""
    config.ddb.update_item(TableName=config.REGISTRY_TABLE, Key=_KEY,
                           UpdateExpression="ADD bot_users :u",
                           ExpressionAttributeValues={":u": {"SS": [uid]}})
    _bust()


def remove_bot_user(uid: str) -> None:
    """Revoke bot access (atomic set-delete). Refuses at the DB layer if the target is an admin —
    admins are not removable here (defence-in-depth; the command also checks)."""
    config.ddb.update_item(
        TableName=config.REGISTRY_TABLE, Key=_KEY,
        UpdateExpression="DELETE bot_users :u",
        ConditionExpression="attribute_not_exists(admins) OR NOT contains(admins, :one)",
        ExpressionAttributeValues={":u": {"SS": [uid]}, ":one": {"S": uid}})
    _bust()


def add_admin(uid: str) -> None:
    """Promote to admin and ensure bot access — both sets in one atomic write."""
    config.ddb.update_item(TableName=config.REGISTRY_TABLE, Key=_KEY,
                           UpdateExpression="ADD admins :a, bot_users :a",
                           ExpressionAttributeValues={":a": {"SS": [uid]}})
    _bust()


# --- unified resources: every grantable thing (kb, mcp, future jira/hubspot/tool) is a resource
# keyed "type:name" in config.resources, each with a `users` allow-list ("*" = all bot_users). ---

def resources() -> dict[str, dict]:
    return registry().get("resources") or {}


def resource_keys() -> list[str]:
    return list(resources().keys())


def _allowed(meta: dict, user_id: str) -> bool:
    users = meta.get("users") or []
    return "*" in users or user_id in users


def resources_for(user_id: str, rtype: str) -> list[tuple[str, dict]]:
    """(name, meta) of resources of `rtype` (e.g. 'kb', 'mcp') this user may use. Default-deny."""
    pre = f"{rtype}:"
    return [(k[len(pre):], m) for k, m in resources().items()
            if k.startswith(pre) and _allowed(m, user_id)]


def _mutate_config(fn) -> None:
    """Read-modify-write the `config` map (resources live in here, not a top-level set)."""
    item = config.ddb.get_item(TableName=config.REGISTRY_TABLE, Key=_KEY,
                               ConsistentRead=True).get("Item") or {}
    cfg = _deser.deserialize(item["config"]) if "config" in item else {}
    fn(cfg)
    config.ddb.update_item(TableName=config.REGISTRY_TABLE, Key=_KEY,
                           UpdateExpression="SET config = :c",
                           ExpressionAttributeValues={":c": _ser.serialize(cfg)})
    _bust()


def set_resource_access(uid: str, key: str, grant: bool) -> None:
    """Grant/revoke a user's access to an existing resource (config.resources[key].users).
    KeyError if no such resource."""
    def f(cfg):
        res = cfg.get("resources") or {}
        if key not in res:
            raise KeyError(key)
        users = res[key].get("users") or []
        if grant:
            if uid not in users and "*" not in users:
                users.append(uid)
        else:
            users = [u for u in users if u != uid]
        res[key]["users"] = users
        cfg["resources"] = res
    _mutate_config(f)


def _email(user_id: str) -> str | None:
    label = slack.user_label(user_id)
    return label if "@" in label else None


def gate(user_id: str) -> tuple[bool, str]:
    """Whether this user may use the bot: must be in bot_users and (if configured) match an
    allowed email domain. Returns (ok, reason)."""
    reg = registry()
    if user_id not in (reg.get("bot_users") or []):
        return False, "not-in-bot_users"
    domains = [d.lower() for d in (reg.get("allowed_email_domains") or [])]
    if domains:
        email = _email(user_id)
        dom = email.rsplit("@", 1)[1].lower() if email else None
        if dom not in domains:
            return False, f"email-domain-not-allowed:{dom}"
    return True, "ok"


def allowed_kbs(user_id: str) -> list[dict[str, str]]:
    """KBs this user may query — the 'kb:' resources they're allowed, with an `id`. Default-deny."""
    return [{"name": name, "id": meta["id"], "description": meta.get("description", "")}
            for name, meta in resources_for(user_id, "kb") if meta.get("id")]


def admins() -> list[str]:
    return list((registry().get("groups") or {}).get("agent-admin") or [])


def is_admin(user_id: str) -> bool:
    return user_id in admins()

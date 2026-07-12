"""User commands handled in the runtime (not the model): /cost, /forget, /help, and the
in-thread "cost/usage/budget …" shortcut. All authz keys off the verified actor_id.

Replies are GitHub-flavored markdown; the Lambda renders them as Slack markdown blocks.
"""
from __future__ import annotations

import re

import access
import budget
import config
from access import is_admin

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)")
_BAREID_RE = re.compile(r"\b([UW][A-Z0-9]{7,})\b")
# Access AND budget are NLU-driven: the model parses intent and calls the manage_access / manage_budget
# tools (see agent.py); the code below only resolves/executes/authorizes — no phrasing parsing. The
# /cost slash command is the one deterministic exception (handled in cost_reply).


def help_text() -> str:
    return ("**cleavis.eu** — just ask in plain language. I can:\n"
            "- **Knowledge base** — HR policies, holidays, perks, processes, internal docs (with links)\n"
            "- **Web** — current information from the web\n"
            "- **Files** — read & create PDF, Word, Excel, PowerPoint\n"
            "- **Calculations** — aggregations, ratios, stats — computed exactly, not guessed\n"
            "Commands: `/cost` — your usage & budget · `/forget` — wipe your memory & uploaded files")


def _clean(label: str) -> str:
    """Display label without the email domain, so Slack doesn't auto-render a `mailto:` link."""
    return label.split("@")[0] if "@" in label else label


def _resolve_named(targets: list[str]) -> tuple[list[tuple[str, str]], list[str], list[tuple[str, list[str]]]]:
    """Resolve target strings (names / emails / <@ID> mentions) → (resolved [(id,label)], missing,
    ambiguous [(query, [candidate labels])]). Ambiguous = a query that matches more than one person."""
    import slack
    resolved: list[tuple[str, str]] = []
    seen: set[str] = set()
    missing: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []
    for tok in targets:
        tok = (tok or "").strip()
        if not tok:
            continue
        m = _MENTION_RE.search(tok) or _BAREID_RE.search(tok)
        if m:
            uid = m.group(1)
            if uid not in seen:
                seen.add(uid)
                resolved.append((uid, slack.user_label(uid)))
            continue
        cands = slack.resolve_candidates(tok)
        if len(cands) == 1:
            uid, lbl = cands[0]
            if uid not in seen:
                seen.add(uid)
                resolved.append((uid, lbl))
        elif not cands:
            missing.append(tok)
        else:
            ambiguous.append((tok, [lbl for _, lbl in cands]))
    return resolved, missing, ambiguous


def _resolve_resource_key(resource: str) -> tuple[str | None, str | None]:
    """(key, error). Match a resource by name across all configured resources; (None, message) when
    there are none, the name is ambiguous, or it's omitted with more than one resource available."""
    keys = access.resource_keys()
    if not keys:
        return None, "No resources are configured for this bot yet."
    if not resource:
        return (keys[0], None) if len(keys) == 1 else (
            None, "Which resource? Available: " + ", ".join(_res_name(k) for k in keys))
    rl = resource.strip().lower().lstrip("#")
    hits = [k for k in keys if _res_name(k).lower() == rl] or \
           [k for k in keys if rl in _res_name(k).lower() or rl in k.lower()]
    hits = list(dict.fromkeys(hits))
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"No resource named '{resource}'. Available: " + ", ".join(_res_name(k) for k in keys)
    return None, "Which one? " + ", ".join(_res_name(k) for k in hits)


def _join(targets: list[tuple[str, str]]) -> str:
    return ", ".join(_clean(lbl) for _, lbl in targets)


_RES_LABELS = {"kb": "Knowledge bases", "mcp": "Integrations (MCP)"}


def _rtype_label(rtype: str) -> str:
    return _RES_LABELS.get(rtype, rtype.upper())


def _res_name(key: str) -> str:
    return key.split(":", 1)[1] if ":" in key else key


def _need_user(name: str | None) -> str:
    return (f"Couldn't identify a user from '{name}' — try the full name, email, or an @mention."
            if name else "Who is this for? Name the user(s) by name, email, or @mention.")


def _registry_view() -> str:
    """A GFM access matrix: one row per user, an Admin column, and one column per resource (✓ = may
    use it, via explicit grant or '*'). Describes who has access to what at a glance."""
    import slack
    reg = access.registry()
    admins = set((reg.get("groups") or {}).get("agent-admin") or [])
    users = reg.get("bot_users") or []
    res = access.resources()
    rkeys = list(res.keys())
    header = ["User", "Admin"] + [_res_name(k) for k in rkeys]
    sep = ["---", ":--:"] + [":--:"] * len(rkeys)
    rows = []
    for u in sorted(users, key=lambda x: (x not in admins, slack.user_label(x).lower())):
        cells = [_clean(slack.user_label(u)), "✓" if u in admins else ""]
        for k in rkeys:
            who = res[k].get("users") or []
            cells.append("✓" if ("*" in who or u in who) else "")
        rows.append("| " + " | ".join(cells) + " |")
    star = " · ✓ on an all-access resource means everyone" if any(
        "*" in (res[k].get("users") or []) for k in rkeys) else ""
    title = f"**Access — {len(users)} users, {len(admins)} admins{star}**"
    table = "| " + " | ".join(header) + " |\n| " + " | ".join(sep) + " |\n" + "\n".join(rows)
    return f"{title}\n\n{table}"          # blank line: GFM needs it to parse a table under a paragraph


def manage_access(actor: str, action: str, targets: list[str], resource: str = "") -> str:
    """Execute an access-management action for an admin. The MODEL supplies the parsed
    action/targets/resource (NLU); authorization (is_admin, on the verified actor) and execution
    live HERE in code — never the model. Asks for clarification when a name or resource is ambiguous.

    action ∈ {add_user, remove_user, add_admin, grant, revoke, list, list_admins}."""
    if not is_admin(actor):
        return "Only admins can manage access."
    action = (action or "").strip().lower().replace("-", "_").replace(" ", "_")

    if action in ("list", "list_users", "list_access", "show", "access"):
        return _registry_view()
    if action in ("list_admins", "show_admins"):
        import slack
        ids = sorted(set((access.registry().get("groups") or {}).get("agent-admin") or []),
                     key=lambda u: slack.user_label(u).lower())
        body = "\n".join(f"- {_clean(slack.user_label(u))}" for u in ids) or "_none_"
        return f"**Admins — {len(ids)}:**\n{body}"

    resolved, missing, ambiguous = _resolve_named(targets or [])
    if ambiguous:
        lines = "\n".join(f"- **{q}** → {', '.join(_clean(l) for l in labels)}" for q, labels in ambiguous)
        tail = f"\n(couldn't find: {', '.join(missing)})" if missing else ""
        return "Several people match — tell me which (use the email or an @mention):\n" + lines + tail
    if not resolved:
        return _need_user(missing[0] if missing else None)
    miss = f" (couldn't identify: {', '.join(missing)})" if missing else ""

    if action in ("add_admin", "make_admin", "promote"):
        for uid, _ in resolved:
            access.add_admin(uid)
        be = "is" if len(resolved) == 1 else "are"
        return f"✅ {_join(resolved)} {be} now admin(s) and can use the bot.{miss}"

    if action in ("grant", "grant_access", "add_access"):
        key, err = _resolve_resource_key(resource)
        if err:
            return err
        for uid, _ in resolved:
            access.add_bot_user(uid)                                # access implies bot use
            access.set_resource_access(uid, key, grant=True)
        return f"✅ Granted {_join(resolved)} access to **{_res_name(key)}**.{miss}"

    if action in ("revoke", "revoke_access", "remove_access"):
        key, err = _resolve_resource_key(resource)
        if err:
            return err
        for uid, _ in resolved:
            access.set_resource_access(uid, key, grant=False)
        return f"✅ Revoked {_join(resolved)} from **{_res_name(key)}**.{miss}"

    if action in ("remove_user", "remove", "delete_user", "revoke_user"):
        removed, skipped = [], []
        for uid, lbl in resolved:
            if access.is_admin(uid):
                skipped.append(_clean(lbl))
                continue
            try:
                access.remove_bot_user(uid)
                removed.append(_clean(lbl))
            except config.ddb.exceptions.ConditionalCheckFailedException:
                skipped.append(_clean(lbl))
        parts = []
        if removed:
            parts.append(f"✅ Removed {', '.join(removed)} — they can no longer use the bot.")
        if skipped:
            be = "is an admin" if len(skipped) == 1 else "are admins"
            parts.append(f"{', '.join(skipped)} {be} — not removed.")
        return (" ".join(parts) or "Nothing to remove.") + miss

    if action in ("add_user", "add", "invite", "grant_user"):
        for uid, _ in resolved:
            access.add_bot_user(uid)
        keys = access.resource_keys()
        if resource:
            key, err = _resolve_resource_key(resource)
            if err:
                return err
            for uid, _ in resolved:
                access.set_resource_access(uid, key, grant=True)
            return f"✅ Added {_join(resolved)} — access to **{_res_name(key)}**.{miss}"
        if not keys:
            return f"✅ Added {_join(resolved)} — they can now use the bot.{miss}"
        if len(keys) == 1:
            for uid, _ in resolved:
                access.set_resource_access(uid, keys[0], grant=True)
            return f"✅ Added {_join(resolved)} — access to **{_res_name(keys[0])}**.{miss}"
        opts = ", ".join(_res_name(k) for k in keys)
        return (f"✅ Added {_join(resolved)} to the bot. Which should they access? {opts} "
                f"(say e.g. “grant {_join(resolved)} access to {_res_name(keys[0])}”).{miss}")

    return "Unknown action — use add_user, remove_user, add_admin, grant, revoke, or list."


def cost_reply(actor: str, raw: str) -> str:
    """The deterministic /cost slash command: admins → top-10, everyone → their own. (Setting caps
    and viewing a specific user are NLU — ask in plain language; the model uses manage_budget.)"""
    low = (raw or "").strip().lower()
    if low.startswith("set"):
        return "Just ask, e.g. “set budget for @user to 10/100” (daily/monthly)."
    wants_self = low.startswith(("me", "mine", "my"))
    if is_admin(actor) and not wants_self:
        return budget.report_top()
    return budget.report_self(actor)


def manage_budget(actor: str, action: str, target: str = "",
                  daily: float | None = None, monthly: float | None = None) -> str:
    """Usage/budget. Self-service for everyone (view your own); admin actions (others, top, set) are
    gated in code on the verified actor. The MODEL supplies the parsed action/target/amounts (NLU)."""
    action = (action or "").strip().lower().replace("-", "_").replace(" ", "_")

    if action in ("", "view_self", "self", "me", "mine", "my_cost", "cost", "usage", "view"):
        return budget.report_self(actor)
    if action in ("view_top", "top", "report", "all", "leaderboard", "everyone"):
        if not is_admin(actor):
            return "Only admins can view everyone's spend — ask for your own with “my cost”."
        return budget.report_top()
    if action in ("view_user", "show_user", "view_other"):
        if not is_admin(actor):
            return "Only admins can view another user's spend."
        resolved, missing, ambiguous = _resolve_named([target] if target else [])
        if ambiguous:
            q, labels = ambiguous[0]
            return f"Several people match '{q}': {', '.join(_clean(l) for l in labels)} — which?"
        if not resolved:
            return _need_user(missing[0] if missing else None)
        uid, lbl = resolved[0]
        return budget.report_self(uid, who=f"{_clean(lbl)}'s")
    if action in ("set", "set_cap", "set_caps", "set_budget", "cap", "limit"):
        if not is_admin(actor):
            return "Only admins can set budgets."
        if daily is None and monthly is None:
            return "Specify a daily and/or monthly cap, e.g. 10/100 (daily $10, monthly $100)."
        resolved, missing, ambiguous = _resolve_named([target] if target else [])
        if ambiguous:
            q, labels = ambiguous[0]
            return f"Several people match '{q}': {', '.join(_clean(l) for l in labels)} — which?"
        if not resolved:
            return _need_user(missing[0] if missing else None)
        parts = ([f"daily ${daily:.0f}"] if daily is not None else []) + \
                ([f"monthly ${monthly:.0f}"] if monthly is not None else [])
        out = []
        for uid, lbl in resolved:
            budget.set_caps(uid, daily, monthly)
            out.append(f"**{_clean(lbl)}** → {', '.join(parts)}")
        return "✅ Set budget: " + "; ".join(out) + "."
    return "Unknown action — use view_self, view_top, view_user, or set."


def forget_actor(actor: str) -> int:
    """Wipe both memories for the user: short-term session events + long-term summary records."""
    if not config.MEMORY_ID:
        return -1
    ac, mid = config.agentcore, config.MEMORY_ID
    deleted = 0
    sessions, tok = [], None
    while True:
        kw = {"memoryId": mid, "actorId": actor, "maxResults": 100}
        if tok:
            kw["nextToken"] = tok
        r = ac.list_sessions(**kw)
        sessions += [s.get("sessionId") for s in (r.get("sessionSummaries") or r.get("sessions") or []) if s.get("sessionId")]
        tok = r.get("nextToken")
        if not tok:
            break
    for sid in sessions:
        etok = None
        while True:                                # short-term events
            kw = {"memoryId": mid, "sessionId": sid, "actorId": actor, "maxResults": 100}
            if etok:
                kw["nextToken"] = etok
            er = ac.list_events(**kw)
            for e in (er.get("events") or er.get("eventSummaries") or []):
                try:
                    ac.delete_event(memoryId=mid, sessionId=sid, eventId=e["eventId"], actorId=actor)
                    deleted += 1
                except Exception:  # noqa: BLE001
                    pass
            etok = er.get("nextToken")
            if not etok:
                break
        rtok = None
        while True:                                # long-term summary records for this session
            kw = {"memoryId": mid, "namespace": f"/summaries/{actor}/{sid}/", "maxResults": 100}
            if rtok:
                kw["nextToken"] = rtok
            try:
                rr = ac.list_memory_records(**kw)
            except Exception:  # noqa: BLE001
                break
            for rec in (rr.get("memoryRecordSummaries") or []):
                try:
                    ac.delete_memory_record(memoryId=mid, memoryRecordId=rec["memoryRecordId"])
                    deleted += 1
                except Exception:  # noqa: BLE001
                    pass
            rtok = rr.get("nextToken")
            if not rtok:
                break
    # Full forget-me: also wipe the user's uploaded files + their personal KB (not just memory).
    if config.USER_FILES:
        try:
            import user_files
            files = user_files.purge_all(actor)
            print(f"[forget] purged {files} file(s) + KB for {actor}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[forget] file purge failed for {actor}: {e}", flush=True)
    return deleted


def handle(actor: str, cmd: str, text: str) -> str:
    """Dispatch an explicit slash command (cmd without leading '/')."""
    cmd = (cmd or "").lower().lstrip("/")
    if cmd == "cost":
        return cost_reply(actor, text)
    if cmd == "forget":
        n = forget_actor(actor)
        if n < 0:
            return "Memory clear isn't configured."
        extra = " and removed your uploaded files" if config.USER_FILES else ""
        return (f"🧹 Fresh start — cleared your conversation memory ({n} item(s), short & long term)"
                f"{extra}.")
    if cmd == "help":
        return help_text()
    return f"Unknown command `/{cmd}`."

"""Per-user budget: spend metering, pre-flight caps, and the /cost report.

Owned entirely by the runtime now (the Lambda no longer touches the budget table). The runtime
holds the exact token usage it just produced, so charging here is both correct and the single
source of budget truth. All functions key off the verified actor_id.
"""
from __future__ import annotations

import time

import config
import slack
from access import is_admin, registry


def _today_month() -> tuple[str, str]:
    t = time.gmtime()
    return time.strftime("%Y-%m-%d", t), time.strftime("%Y-%m", t)


def _prices() -> tuple[float, float]:
    b = registry().get("budget") or {}
    return (float(b.get("price_in_usd_per_mtok", config.PRICE_IN_USD_PER_MTOK)),
            float(b.get("price_out_usd_per_mtok", config.PRICE_OUT_USD_PER_MTOK)))


def cost_usd(usage: dict) -> float:
    pin, pout = _prices()
    return usage.get("input", 0) / 1e6 * pin + usage.get("output", 0) / 1e6 * pout


def view(actor: str) -> dict[str, float]:
    """Spend + monthly token/cache counters (rollover applied) + effective caps."""
    day, month = _today_month()
    it = config.ddb.get_item(TableName=config.BUDGET_TABLE, Key={"actor_id": {"S": actor}}).get("Item") or {}
    b = registry().get("budget") or {}
    same_month = it.get("month", {}).get("S") == month
    daily = float(it["daily_usd"]["N"]) if it.get("day", {}).get("S") == day and "daily_usd" in it else 0.0
    monthly = float(it["monthly_usd"]["N"]) if same_month and "monthly_usd" in it else 0.0
    cap_d = float(it["cap_daily_usd"]["N"]) if "cap_daily_usd" in it else float(b.get("default_daily_usd", config.DEFAULT_DAILY_USD))
    cap_m = float(it["cap_monthly_usd"]["N"]) if "cap_monthly_usd" in it else float(b.get("default_monthly_usd", config.DEFAULT_MONTHLY_USD))
    tok = lambda k: int(float(it[k]["N"])) if same_month and k in it else 0  # noqa: E731
    return {"daily": daily, "monthly": monthly, "cap_d": cap_d, "cap_m": cap_m,
            "m_in": tok("monthly_in"), "m_out": tok("monthly_out"),
            "m_cr": tok("monthly_cr"), "m_cw": tok("monthly_cw")}


def preflight(actor: str) -> str | None:
    """Return a user-facing block message if the actor is at/over a cap, else None."""
    v = view(actor)
    if v["daily"] >= v["cap_d"] or v["monthly"] >= v["cap_m"]:
        which = "daily" if v["daily"] >= v["cap_d"] else "monthly"
        cap = v["cap_d"] if which == "daily" else v["cap_m"]
        resets = "tomorrow" if which == "daily" else "next month"
        return (f"You've reached your {which} usage limit (${cap:.0f}). "
                f"It resets {resets} — ping an admin if you need more.")
    return None


def charge(actor: str, usage: dict) -> None:
    """Add this turn's cost + token/cache usage to running daily+monthly counters (rollover-aware).

    Fast path (same UTC day AND month as the stored record): an ATOMIC DynamoDB ADD, so concurrent
    turns can't read-modify-write over each other (which made the counter drift down). The cap_*
    overrides are separate attributes, untouched. Slow path runs only at a day/month rollover (or the
    very first charge): reset the rolled-over counters with a SET — rare, so the small race there is
    negligible."""
    day, month = _today_month()
    cost = cost_usd(usage)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    i, o = usage.get("input", 0), usage.get("output", 0)
    cr, cw = usage.get("cache_read", 0), usage.get("cache_write", 0)
    try:
        config.ddb.update_item(
            TableName=config.BUDGET_TABLE, Key={"actor_id": {"S": actor}},
            UpdateExpression=("ADD daily_usd :d, monthly_usd :d, monthly_in :i, monthly_out :o, "
                              "monthly_cr :cr, monthly_cw :cw SET updated_at=:u"),
            ConditionExpression="#dy = :dy AND #mo = :mo",
            ExpressionAttributeNames={"#dy": "day", "#mo": "month"},
            ExpressionAttributeValues={
                ":d": {"N": f"{cost:.6f}"}, ":i": {"N": str(i)}, ":o": {"N": str(o)},
                ":cr": {"N": str(cr)}, ":cw": {"N": str(cw)}, ":u": {"S": now},
                ":dy": {"S": day}, ":mo": {"S": month}},
        )
    except config.ddb.exceptions.ConditionalCheckFailedException:
        v = view(actor)        # applies rollover: daily=0 if day changed, monthly_*=0 if month changed
        config.ddb.update_item(
            TableName=config.BUDGET_TABLE, Key={"actor_id": {"S": actor}},
            UpdateExpression=("SET #dy=:dy, daily_usd=:d, #mo=:mo, monthly_usd=:m, "
                              "monthly_in=:i, monthly_out=:o, monthly_cr=:cr, monthly_cw=:cw, updated_at=:u"),
            ExpressionAttributeNames={"#dy": "day", "#mo": "month"},
            ExpressionAttributeValues={
                ":dy": {"S": day}, ":d": {"N": f"{v['daily'] + cost:.6f}"},
                ":mo": {"S": month}, ":m": {"N": f"{v['monthly'] + cost:.6f}"},
                ":i": {"N": str(v["m_in"] + i)}, ":o": {"N": str(v["m_out"] + o)},
                ":cr": {"N": str(v["m_cr"] + cr)}, ":cw": {"N": str(v["m_cw"] + cw)},
                ":u": {"S": now}},
        )


def set_caps(actor: str, daily: float | None, monthly: float | None) -> None:
    sets, vals = [], {}
    if daily is not None:
        sets.append("cap_daily_usd=:cd"); vals[":cd"] = {"N": f"{daily:.6f}"}
    if monthly is not None:
        sets.append("cap_monthly_usd=:cm"); vals[":cm"] = {"N": f"{monthly:.6f}"}
    if sets:
        config.ddb.update_item(TableName=config.BUDGET_TABLE, Key={"actor_id": {"S": actor}},
                               UpdateExpression="SET " + ", ".join(sets), ExpressionAttributeValues=vals)


def _fmt_tok(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else (f"{n / 1e3:.0f}k" if n >= 1000 else str(n))


def _cache_pct(in_t: int, cr: int, cw: int) -> float:
    """Cache-hit rate = cache-read tokens / all input tokens (uncached + read + write)."""
    tot = in_t + cr + cw
    return (cr / tot * 100) if tot else 0.0


def report_top(limit: int = 10) -> str:
    """Admin view: top spenders as a GFM table — today's + month's spend, each against its cap."""
    day, month = _today_month()
    b = registry().get("budget") or {}
    def_d = float(b.get("default_daily_usd", config.DEFAULT_DAILY_USD))
    def_m = float(b.get("default_monthly_usd", config.DEFAULT_MONTHLY_USD))
    rows = []
    for it in config.ddb.scan(TableName=config.BUDGET_TABLE).get("Items", []):
        same = it.get("month", {}).get("S") == month
        sday = it.get("day", {}).get("S") == day
        g = lambda k: int(float(it[k]["N"])) if same and k in it else 0  # noqa: E731
        mu = float(it["monthly_usd"]["N"]) if same and "monthly_usd" in it else 0.0
        du = float(it["daily_usd"]["N"]) if sday and "daily_usd" in it else 0.0
        cap_d = float(it["cap_daily_usd"]["N"]) if "cap_daily_usd" in it else def_d
        cap_m = float(it["cap_monthly_usd"]["N"]) if "cap_monthly_usd" in it else def_m
        ci, cr, cw = g("monthly_in"), g("monthly_cr"), g("monthly_cw")
        rows.append({"a": it["actor_id"]["S"], "usd": mu, "today": du, "cap_d": cap_d, "cap_m": cap_m,
                     "in": ci, "out": g("monthly_out"), "cache": cr + cw, "hit": _cache_pct(ci, cr, cw)})
    rows = sorted([r for r in rows if r["usd"] > 0 or r["in"] > 0], key=lambda r: r["usd"], reverse=True)[:limit]
    if not rows:
        return "No usage recorded this month yet."
    def _who(actor):  # name only (strip @domain) so Slack doesn't auto-link emails — no backticks
        lbl = slack.user_label(actor)
        return lbl.split("@")[0] if "@" in lbl else lbl
    tbl = ["| User | In | Out | Cache | Cache hit | Today ($/cap) | Month ($/cap) |",
           "|---|---:|---:|---:|---:|---:|---:|"] + [
        f"| {_who(r['a'])} | {_fmt_tok(r['in'])} | {_fmt_tok(r['out'])} | {_fmt_tok(r['cache'])} | "
        f"{r['hit']:.0f}% | ${r['today']:.2f} / ${r['cap_d']:.0f} | ${r['usd']:.2f} / ${r['cap_m']:.0f} |"
        for r in rows]
    return "**Top users — this month**\n\n" + "\n".join(tbl)


def report_self(actor: str, who: str = "Your") -> str:
    """A user's usage this month as a GFM table. `who` titles it ("Your" for self, "<name>'s" when
    an admin views someone else)."""
    v = view(actor)
    hit = _cache_pct(int(v["m_in"]), int(v["m_cr"]), int(v["m_cw"]))
    tbl = ["| In | Out | Cache | Cache hit | $ Month | $ Today |", "|---:|---:|---:|---:|---:|---:|",
           f"| {_fmt_tok(int(v['m_in']))} | {_fmt_tok(int(v['m_out']))} | {_fmt_tok(int(v['m_cr'] + v['m_cw']))} | "
           f"{hit:.0f}% | ${v['monthly']:.2f} / ${v['cap_m']:.0f} | ${v['daily']:.2f} / ${v['cap_d']:.0f} |"]
    return f"**{who} usage — this month**\n\n" + "\n".join(tbl)

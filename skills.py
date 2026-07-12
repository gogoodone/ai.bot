"""Progressive-disclosure skills: only names+descriptions go in the (cached) system prompt;
the model calls load_skill(name) to pull a skill's full SKILL.md when it actually needs it.
"""
from __future__ import annotations

import json
import re
import time

from strands import tool

import config

_SKILL_TTL = 3600     # 1 hour: cache the S3 body so repeated loads (and the cached
_skill_cache: dict[str, tuple[float, str]] = {}   # prompt prefix) stay stable; edits propagate <=1h.


def read_skill(name: str) -> str:
    config.dlog(f"[skills] load_skill {name}")              # gated observability: did the model load it?
    hit = _skill_cache.get(name)
    if hit and time.time() - hit[0] < _SKILL_TTL:
        return hit[1]
    body = config.s3.get_object(Bucket=config.SKILLS_BUCKET, Key=f"{name}/SKILL.md")["Body"].read().decode()
    _skill_cache[name] = (time.time(), body)
    return body


def _description(md: str) -> str:
    m = re.search(r"^description:\s*(.+)$", md, re.MULTILINE)
    return (m.group(1).strip() if m else "")[:300]


def discover() -> dict[str, str]:
    """Enabled skills from the S3 manifest -> {name: description}; falls back to scanning the bucket."""
    try:
        names = json.loads(config.s3.get_object(Bucket=config.SKILLS_BUCKET, Key=config.SKILLS_MANIFEST)["Body"].read())
    except Exception:  # noqa: BLE001
        names = sorted({o["Key"].split("/")[0] for o in
                        config.s3.list_objects_v2(Bucket=config.SKILLS_BUCKET).get("Contents", [])
                        if o["Key"].endswith("/SKILL.md")})
    out = {}
    for n in names:
        try:
            out[n] = _description(read_skill(n))
        except Exception:  # noqa: BLE001
            pass
    return out


SKILLS = discover()
CATALOG = "\n".join(f"- {n}: {d}" for n, d in SKILLS.items()) or "(none)"

# Admin-only skills: NOT in the public manifest/catalog. The agent appends ADMIN_CATALOG to the
# system prompt and allows loading these ONLY when the verified user is an admin (see agent.py).
ADMIN_SKILLS = {
    "access": "(admin) manage who can use this bot — add/remove users, make admins, "
              "grant/revoke resource access, list access",
}
ADMIN_CATALOG = "\n" + "\n".join(f"- {n}: {d}" for n, d in ADMIN_SKILLS.items())


@tool
def load_skill(name: str) -> str:
    """Load a skill's full instructions (its SKILL.md) before doing that kind of task. Valid names
    are in the AVAILABLE SKILLS catalog in the system prompt (e.g. pdf, docx, xlsx, pptx)."""
    if name not in SKILLS:
        return f"Unknown skill '{name}'. Available: {', '.join(SKILLS) or '(none)'}"
    return read_skill(name)

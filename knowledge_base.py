"""Confluence knowledge base access — direct bedrock-agent-runtime Retrieve (the KB is S3_VECTORS,
which the Gateway KB connector doesn't support) plus a full-page fetch from the source bucket.

Tools are built per request, closed over the trusted allowed_kbs list the runtime computed for
the verified user. The model only picks the query; it can't widen scope. Default-deny.
"""
from __future__ import annotations

import re

from strands import tool

import config


def confluence_url(s3_uri: str) -> str:
    """Rebuild a Confluence page URL from an ingested S3 key (fallback when metadata lacks source_url).
    Pages are confluence/<SPACE>/<id>.md, attachments confluence/<SPACE>/attachments/<id>_<file>.md —
    the leading number is the (parent) page id in both."""
    m = re.search(r"/confluence/[^/]+/(?:attachments/)?(\d+)", s3_uri or "")
    return f"{config.CONFLUENCE_BASE}/pages/viewpage.action?pageId={m.group(1)}" if m else ""


def retrieve(kb_id: str, query: str, n: int) -> list[tuple[str, str]]:
    """Raw KB retrieve -> [(url, text)]. url = crawler metadata source_url when present, else rebuilt."""
    r = config.kb_runtime.retrieve(
        knowledgeBaseId=kb_id, retrievalQuery={"text": query[:1000]},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": n}})
    rows = []
    for x in r.get("retrievalResults", []) or []:
        loc = x.get("location", {}) or {}
        s3_uri = (loc.get("s3Location", {}) or {}).get("uri") or ""
        meta = x.get("metadata", {}) or {}
        src = (meta.get("source_url")
               or (loc.get("confluenceLocation", {}) or {}).get("url")
               or confluence_url(s3_uri) or s3_uri.rsplit("/", 1)[-1] or "Confluence")
        txt = ((x.get("content", {}) or {}).get("text", "") or "")[:2500].replace("\n", " ")
        rows.append((src, txt))
    return rows


def make_tools(allowed_kbs: list[dict]) -> list:
    """Build the [search, get_page] tools for this request, scoped to allowed_kbs."""

    @tool
    def knowledge_base_search(query: str, max_results: int = 5) -> str:
        """Search cleavis's internal Confluence knowledge base(s) — company docs, HR policies, perks,
        runbooks, processes, onboarding, team handbooks, internal pages. Use for "how does cleavis. do
        X" / internal-documentation questions. Each passage comes with its Confluence page URL — ALWAYS
        cite the source pages you used as Slack links. Use the EXACT `source:` URL returned — never
        invent or alter a link. Passages are short ~300-token excerpts; if you need a page's full
        content (a whole table, every holiday, a complete policy), call knowledge_base_get_page."""
        if not allowed_kbs:
            return "You don't have access to any knowledge base."
        n = max(1, min(int(max_results), 10))
        out = []
        for kb in allowed_kbs:
            try:
                for src, txt in retrieve(kb["id"], query, n):
                    tag = f"[{kb['name']}] " if len(allowed_kbs) > 1 else ""
                    out.append(f"- {tag}source: {src}\n  {txt}")
            except Exception as ex:  # noqa: BLE001
                out.append(f"- ({kb.get('name', 'kb')} unavailable: {repr(ex)[:120]})")
        return "\n".join(out) if out else "No relevant pages found in your knowledge base(s)."

    @tool
    def knowledge_base_get_page(page: str) -> str:
        """Fetch the FULL, untruncated text of a knowledge-base page. Search returns only short
        ~300-token passages, so when a snippet is partial or you need the WHOLE page — every row of a
        table (e.g. ALL holidays in a year), a complete policy — call this with the `source:` URL (or
        page id) that knowledge_base_search returned. Cite the same page link in your answer."""
        if not allowed_kbs:
            return "You don't have access to any knowledge base."
        s = (page or "").strip()
        key = None
        m = re.search(r"/spaces/([^/]+)/pages/(\d+)", s)
        if m:
            key = f"confluence/{m.group(1)}/{m.group(2)}.md"
        elif s.startswith("s3://"):
            key = s.split("/", 3)[-1] if s.count("/") >= 3 else None
        else:
            idm = re.search(r"(\d{5,})", s)
            if idm:
                try:
                    for o in config.s3.list_objects_v2(Bucket=config.CONFLUENCE_BUCKET,
                                                       Prefix="confluence/").get("Contents", []):
                        if o["Key"].endswith(f"/{idm.group(1)}.md"):
                            key = o["Key"]
                            break
                except Exception:  # noqa: BLE001
                    pass
        if not key:
            return "Couldn't resolve that page — pass the exact source URL from knowledge_base_search."
        try:
            body = config.s3.get_object(Bucket=config.CONFLUENCE_BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
        except Exception as ex:  # noqa: BLE001
            return f"Couldn't fetch that page: {repr(ex)[:120]}"
        return body[:30000] + ("\n…(truncated)" if len(body) > 30000 else "")

    return [knowledge_base_search, knowledge_base_get_page]

"""Per-user uploaded-file knowledge (experimental, gated by CLEAVIS_USER_FILES).

Mirrors the Confluence crawler, but per user: an upload is hashed (dedup), converted to clean GFM
markdown, stored in the user's personal S3 prefix, and ingested into a PER-USER Bedrock KB (S3
Vectors, provisioned on first upload) so it stays queryable on later turns and across files. Metadata
+ folders live in DynamoDB. Everything is keyed on the VERIFIED user_id — a user only ever touches
their own files (trust boundary, never the model).

Shared by two runtimes (single source of truth for conversion + indexing logic):
  - the RESPONDER (AgentCore runtime) calls the light path — stage() (hash/dedup/version, provision KB,
    write a pending row) plus search/list/move/remove. It never converts, so it carries no markitdown.
  - the WORKER (user_files_worker container Lambda) calls the heavy path — process() (markitdown
    convert → store md), trigger_ingest(), wait_indexed() — off the chat critical path.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config

import config

_deser = TypeDeserializer()
# Adaptive retries so KB control-plane throttling (CreateKnowledgeBase / CreateDataSource / CreateIndex)
# under burst becomes "slower", not "failed" — key for many users provisioning at once.
_cfg = Config(retries={"max_attempts": 8, "mode": "adaptive"})
_ba = boto3.client("bedrock-agent", region_name=config.REGION, config=_cfg)   # KB control plane
_s3v = boto3.client("s3vectors", region_name=config.REGION, config=_cfg)      # vector store

_ACCT = "123456789012"
_VECTOR_BUCKET = "cleavis-user-files-vectors"
_KB_ROLE_ARN = f"arn:aws:iam::{_ACCT}:role/cleavis-user-files-kb-exec"
_EMBED_ARN = "arn:aws:bedrock:eu-central-1::foundation-model/amazon.titan-embed-text-v2:0"
_KB_ROW = "__KB__"                                                    # per-user KB record (not a file)


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _rows(items) -> list[dict]:
    return [{k: _deser.deserialize(v) for k, v in it.items()} for it in items]


def list_files(user_id: str) -> list[dict]:
    try:
        r = config.ddb.query(TableName=config.USER_FILES_TABLE,
                             KeyConditionExpression="user_id = :u",
                             ExpressionAttributeValues={":u": {"S": user_id}})
        return [f for f in _rows(r.get("Items", [])) if f.get("file_id") != _KB_ROW]
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] list failed: {e}", flush=True)
        return []


def _kb_record(user_id: str) -> dict | None:
    """The user's KB provisioning record (kb_id/ds_id/index), or None. Fetched directly (it's hidden
    from list_files so it never shows up as a 'file')."""
    try:
        it = config.ddb.get_item(TableName=config.USER_FILES_TABLE,
                                 Key={"user_id": {"S": user_id}, "file_id": {"S": _KB_ROW}}).get("Item")
        return {k: _deser.deserialize(v) for k, v in it.items()} if it else None
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] kb_record failed: {e}", flush=True)
        return None


def find_by_hash(user_id: str, h: str) -> dict | None:
    return next((f for f in list_files(user_id) if f.get("content_hash") == h), None)


def find_by_name(user_id: str, filename: str) -> dict | None:
    matches = [f for f in list_files(user_id) if f.get("filename") == filename]
    return max(matches, key=lambda f: int(f.get("version", 0))) if matches else None


def _put(user_id: str, row: dict) -> None:
    item = {"user_id": {"S": user_id}}
    for k, v in row.items():
        item[k] = {"N": str(v)} if isinstance(v, (int, float)) else {"S": str(v)}
    config.ddb.put_item(TableName=config.USER_FILES_TABLE, Item=item)


def remove(user_id: str, file_id: str) -> bool:
    """Delete a file end-to-end: DDB row + md object AND purge its vectors from the user's KB. Deleting
    the source md and re-syncing makes Bedrock drop the orphaned embeddings — otherwise a 'removed' file
    keeps showing up in search."""
    row = next((f for f in list_files(user_id) if f.get("file_id") == file_id), None)
    if not row:
        return False
    try:
        config.ddb.delete_item(TableName=config.USER_FILES_TABLE,
                               Key={"user_id": {"S": user_id}, "file_id": {"S": file_id}})
        md_key = row.get("s3_md_key")
        if md_key:
            try:
                config.s3.delete_object(Bucket=config.USER_FILES_BUCKET, Key=md_key)
            except Exception:  # noqa: BLE001
                pass
        trigger_ingest(user_id)   # re-sync the KB so the deleted doc's vectors are removed
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] remove failed: {e}", flush=True)
        return False


def purge_all(user_id: str) -> int:
    """Full wipe for forget-me / offboarding: delete every file row + md, then tear down the KB +
    index. No per-file re-sync — the whole KB is destroyed. Returns files removed."""
    n = 0
    for f in list_files(user_id):
        try:
            config.ddb.delete_item(TableName=config.USER_FILES_TABLE,
                                   Key={"user_id": {"S": user_id}, "file_id": {"S": f["file_id"]}})
            md_key = f.get("s3_md_key")
            if md_key:
                try:
                    config.s3.delete_object(Bucket=config.USER_FILES_BUCKET, Key=md_key)
                except Exception:  # noqa: BLE001
                    pass
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"[user_files] purge_all delete failed: {e}", flush=True)
    delete_kb(user_id)                                 # tear down KB + vector index (safe order)
    return n


def clear_all(user_id: str) -> int:
    """Delete ALL of the user's files: every DDB row + md, then ONE KB re-sync to purge all their
    vectors at once. The (now empty) KB is kept, ready for new uploads. Returns count."""
    files = list_files(user_id)
    n = 0
    for f in files:
        try:
            config.ddb.delete_item(TableName=config.USER_FILES_TABLE,
                                   Key={"user_id": {"S": user_id}, "file_id": {"S": f["file_id"]}})
            md_key = f.get("s3_md_key")
            if md_key:
                try:
                    config.s3.delete_object(Bucket=config.USER_FILES_BUCKET, Key=md_key)
                except Exception:  # noqa: BLE001
                    pass
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"[user_files] clear_all delete failed: {e}", flush=True)
    if n:
        trigger_ingest(user_id)       # one re-sync drops every removed doc's vectors
    return n


def recategorize(user_id: str, file_id: str, category: str) -> bool:
    try:
        config.ddb.update_item(TableName=config.USER_FILES_TABLE,
                               Key={"user_id": {"S": user_id}, "file_id": {"S": file_id}},
                               UpdateExpression="SET category = :c",
                               ExpressionAttributeValues={":c": {"S": category}})
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] recategorize failed: {e}", flush=True)
        return False


# --- ingest (AWS-heavy bits stubbed for the next increment) -----------------
_TEXT_EXT = ("md", "markdown", "txt", "text", "csv", "tsv", "json", "log", "yaml", "yml")
_DATA_URI_IMG = re.compile(r"!\[[^\]]*\]\(data:[^)]+\)")   # base64-inlined images = noise
_BLANKS = re.compile(r"\n{3,}")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]+\)")            # [text](url) — keep text, drop the URL
_NAV_RESIDUE = re.compile(r"[\s\d.,;:>\-–—_|()\[\]#*•]+")  # what's left of a pure nav/index line after links


def _strip_nav(md: str) -> str:
    """Strip the table-of-contents / index / cross-reference machinery that converters (esp. EPUB→md)
    leave behind, because it pollutes chunks + embeddings and outranks real prose. Two passes per line:
    flatten [text](url) → text (kills the noise anchor URLs), and DROP lines that were pure navigation —
    a line whose only real content was link(s) + page refs (nothing left but whitespace/digits/punct once
    links are removed). Prose that merely contains a link is kept (just flattened)."""
    out = []
    for line in md.split("\n"):
        if _MD_LINK.search(line) and not _NAV_RESIDUE.sub("", _MD_LINK.sub("", line)):
            continue                                       # pure TOC/index/page-ref line → drop entirely
        out.append(_MD_LINK.sub(r"\1", line))              # keep the line, flatten any links to plain text
    return "\n".join(out)


def _to_markdown(raw: bytes, filename: str) -> str:
    """Uploaded file → clean GFM, the SAME way the Confluence crawler converts attachments: text-like
    files pass through; everything else (PDF/DOCX/XLSX/PPTX/HTML/EPUB/…) goes through markitdown — broad
    format coverage, no LLM. Base64-inlined images are dropped; blank runs collapsed."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in _TEXT_EXT:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    try:
        import io
        from markitdown import MarkItDown
        try:
            _md = MarkItDown(enable_plugins=False)
        except TypeError:                              # older markitdown without enable_plugins
            _md = MarkItDown()
        res = _md.convert_stream(io.BytesIO(raw), file_extension=("." + ext if ext else None))
        md = _BLANKS.sub("\n\n", _strip_nav(_DATA_URI_IMG.sub("", res.text_content or ""))).strip()
        return md or f"(no extractable text in {filename})"
    except Exception as e:  # noqa: BLE001 — unsupported/garbled file: keep a record, don't crash ingest
        print(f"[user_files] markitdown failed for {filename}: {e}", flush=True)
        return f"(could not convert {filename}: {type(e).__name__})"


def _safe_name(user_id: str) -> str:
    """A KB/index name for a user id. S3 Vectors index names are strict: lowercase letters/digits/
    hyphens, 3-63 chars, start/end alphanumeric. Slack ids are uppercase + unique case-insensitively,
    so lowercasing is safe."""
    s = re.sub(r"[^a-z0-9-]", "-", user_id.lower()).strip("-")
    return ("uf-" + s)[:60]


def ensure_kb(user_id: str) -> str | None:
    """Provision (once, idempotent) the user's own Bedrock KB on S3 Vectors over their S3 prefix and
    cache kb_id/ds_id. Mirrors the Confluence KB exactly: a per-user vector index in the shared vector
    bucket, a VECTOR KB on the shared exec role + Titan v2 embeddings, and an S3 data source scoped to
    `<user_id>/`. Returns the kb_id (or None if provisioning failed)."""
    rec = _kb_record(user_id)
    if rec and rec.get("kb_id"):
        return rec["kb_id"]
    name = _safe_name(user_id)
    bucket_arn = f"arn:aws:s3vectors:{config.REGION}:{_ACCT}:bucket/{_VECTOR_BUCKET}"
    index_arn = f"{bucket_arn}/index/{name}"
    try:
        # 1. per-user vector index (same spec as confluence-pages: float32 / 1024 / cosine)
        try:
            _s3v.create_index(vectorBucketName=_VECTOR_BUCKET, indexName=name, dataType="float32",
                              dimension=1024, distanceMetric="cosine",
                              metadataConfiguration={"nonFilterableMetadataKeys": [
                                  "AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]})
        except _s3v.exceptions.ConflictException:
            pass  # already created on an earlier (partial) provision
        # 2. the knowledge base — adopt an existing one on conflict (recovers a lost __KB__ record)
        try:
            kb_id = _ba.create_knowledge_base(
                name=name, roleArn=_KB_ROLE_ARN,
                knowledgeBaseConfiguration={"type": "VECTOR",
                    "vectorKnowledgeBaseConfiguration": {"embeddingModelArn": _EMBED_ARN}},
                storageConfiguration={"type": "S3_VECTORS",
                    "s3VectorsConfiguration": {"vectorBucketArn": bucket_arn, "indexArn": index_arn}},
            )["knowledgeBase"]["knowledgeBaseId"]
        except _ba.exceptions.ConflictException:
            kb_id = next((k["knowledgeBaseId"] for k in
                          _ba.list_knowledge_bases(maxResults=100)["knowledgeBaseSummaries"]
                          if k["name"] == name), None)
            if not kb_id:
                raise
            print(f"[user_files] adopting existing KB {kb_id} for {user_id}", flush=True)
        for _ in range(30):  # data source needs the KB out of CREATING
            if _ba.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]["status"] == "ACTIVE":
                break
            time.sleep(2)
        # 3. data source scoped to THIS user's prefix — reuse it if the KB already had one.
        # Hierarchical chunking: search matches the precise ~300-tok CHILD, but Bedrock returns its
        # larger ~1500-tok PARENT section to the model — precision of small + context of large.
        existing = _ba.list_data_sources(knowledgeBaseId=kb_id).get("dataSourceSummaries", [])
        if existing:
            ds_id = existing[0]["dataSourceId"]
        else:
            ds_id = _ba.create_data_source(
                knowledgeBaseId=kb_id, name=name,
                dataSourceConfiguration={"type": "S3", "s3Configuration": {
                    "bucketArn": f"arn:aws:s3:::{config.USER_FILES_BUCKET}",
                    "inclusionPrefixes": [f"{user_id}/"]}},
                vectorIngestionConfiguration={"chunkingConfiguration": {
                    "chunkingStrategy": "HIERARCHICAL",
                    "hierarchicalChunkingConfiguration": {
                        "levelConfigurations": [{"maxTokens": 1500}, {"maxTokens": 300}],
                        "overlapTokens": 60}}},
            )["dataSource"]["dataSourceId"]
        _put(user_id, {"file_id": _KB_ROW, "kb_id": kb_id, "ds_id": ds_id, "index": name,
                       "created": int(time.time())})
        return kb_id
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] ensure_kb failed for {user_id}: {e}", flush=True)
        return None


def delete_kb(user_id: str) -> bool:
    """Tear down the user's KB entirely: knowledge base (+ its data source) and the vector index, then
    drop the cached record. For offboarding / forget-me. Best-effort per resource."""
    rec = _kb_record(user_id)
    if not rec:
        return False
    if rec.get("kb_id"):
        try:
            _ba.delete_knowledge_base(knowledgeBaseId=rec["kb_id"])
            # Wait for the KB to FULLY delete before removing its vector index. Deleting the index
            # first makes the KB deletion fail (status DELETE_UNSUCCESSFUL) because its vector store
            # vanished mid-delete — which then blocks recreating a KB with the same name.
            for _ in range(30):
                try:
                    _ba.get_knowledge_base(knowledgeBaseId=rec["kb_id"])
                    time.sleep(2)
                except _ba.exceptions.ResourceNotFoundException:
                    break
        except Exception as e:  # noqa: BLE001
            print(f"[user_files] delete_knowledge_base failed: {e}", flush=True)
    if rec.get("index"):
        try:
            _s3v.delete_index(vectorBucketName=_VECTOR_BUCKET, indexName=rec["index"])
        except Exception as e:  # noqa: BLE001
            print(f"[user_files] delete_index failed: {e}", flush=True)
    try:
        config.ddb.delete_item(TableName=config.USER_FILES_TABLE,
                               Key={"user_id": {"S": user_id}, "file_id": {"S": _KB_ROW}})
    except Exception:  # noqa: BLE001
        pass
    return True


def trigger_ingest(user_id: str) -> str | None:
    """Start ONE Bedrock KB ingestion job for the user's data source — it syncs every new/changed md in
    their prefix, so a whole upload batch is covered by a single job. Call it ONCE after all of a batch's
    md is stored (not per file) to avoid the one-job-per-data-source ConflictException. On conflict (a job
    is already running) we treat it as success — that running job will pick up the just-written md on its
    scan. Returns the job id (or the running one on conflict), else None."""
    rec = _kb_record(user_id)
    if not (rec and rec.get("kb_id") and rec.get("ds_id")):
        return None
    try:
        return _ba.start_ingestion_job(knowledgeBaseId=rec["kb_id"],
                                       dataSourceId=rec["ds_id"])["ingestionJob"]["ingestionJobId"]
    except _ba.exceptions.ConflictException:
        jobs = _ba.list_ingestion_jobs(knowledgeBaseId=rec["kb_id"], dataSourceId=rec["ds_id"],
                                       maxResults=5, sortBy={"attribute": "STARTED_AT",
                                                             "order": "DESCENDING"}).get("ingestionJobSummaries", [])
        running = next((j["ingestionJobId"] for j in jobs if j["status"] in ("STARTING", "IN_PROGRESS")), None)
        print(f"[user_files] ingest already running for {user_id} (job {running}); batch covered", flush=True)
        return running
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] ingest trigger failed for {user_id}: {e}", flush=True)
        return None


def retrieve(user_id: str, query: str, k: int = 12) -> list[dict]:
    """Semantic search over ONLY this user's KB → [{text, score, source}]. Empty if no KB yet.
    k=12 (not 6): more candidate chunks reach the model so borderline-but-relevant passages aren't
    cut off at the top — the model filters what's actually relevant from the returned set."""
    rec = _kb_record(user_id)
    if not (rec and rec.get("kb_id")):
        return []
    try:
        r = config.kb_runtime.retrieve(
            knowledgeBaseId=rec["kb_id"], retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": k}})
        out = []
        for it in r.get("retrievalResults", []):
            out.append({"text": (it.get("content") or {}).get("text", ""),
                        "score": it.get("score"),
                        "source": ((it.get("location") or {}).get("s3Location") or {}).get("uri", "")})
        # Observability (gated): the exact query + what came back (source, score, snippet) so we can see
        # whether a "not found" is a retrieval miss or the model ignoring a returned passage.
        if config.DEBUG_TOOLS:
            _hits = [(((h.get("source") or "").rsplit("/", 1)[-1]), round(h.get("score") or 0, 3),
                      " ".join((h.get("text") or "")[:90].split())) for h in out]
            config.dlog(f"[user_files] retrieve q={query!r} k={k} -> {len(out)} hits {_hits}")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] retrieve failed for {user_id}: {e}", flush=True)
        return []


def stage_pending(user_id: str, filename: str, raw_bucket: str, raw_key: str, folder: str = "/",
                  channel_id: str = "", thread_ts: str = "") -> dict:
    """Responder side (ZERO-TOUCH): write a PENDING row pointing at the staged raw file — no file read,
    no hash, no convert. The whole point: the chat path never pulls the bytes into memory. The worker
    (async-invoked) reads the file exactly ONCE and does hash → dedup → version → convert → index.
    Provisions the user's KB on first upload (cheap metadata op after). Returns {file_id, filename}."""
    file_id = uuid.uuid4().hex
    ensure_kb(user_id)                                 # provision KB on first upload (idempotent, no file bytes)
    _put(user_id, {"file_id": file_id, "filename": filename, "category": "uncategorized",
                   "folder": folder or "/", "s3_md_key": f"{user_id}/{file_id}.md",
                   "s3_raw_bucket": raw_bucket, "s3_raw_key": raw_key, "kb_status": "staged",
                   "created": int(time.time()), "channel_id": channel_id or "", "thread_ts": thread_ts or ""})
    return {"file_id": file_id, "filename": filename}


def process(user_id: str, file_id: str) -> dict:
    """Worker side (heavy): read the staged raw ONCE → hash → dedup → version → convert to markdown →
    store md → classify. Returns a status dict {status, filename, file_id}:
      'ingested'  — new file, md stored, ready for the batch ingestion job.
      'duplicate' — same content already present; the redundant pending row is deleted here.
      'failed'    — could not read/convert; row left as-is for retry."""
    row = next((f for f in list_files(user_id) if f.get("file_id") == file_id), None)
    filename = (row or {}).get("filename", file_id)
    if not row or not row.get("s3_raw_key"):
        print(f"[user_files] process: no row/raw for {user_id}/{file_id}", flush=True)
        return {"status": "failed", "file_id": file_id, "filename": filename}
    raw_bucket = row.get("s3_raw_bucket") or config.UPLOAD_BUCKET
    raw_key, md_key = row["s3_raw_key"], row.get("s3_md_key")
    try:
        raw = config.s3.get_object(Bucket=raw_bucket, Key=raw_key)["Body"].read()
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] process: raw read failed {raw_key}: {e}", flush=True)
        return {"status": "failed", "file_id": file_id, "filename": filename}
    # Dedup + version now live here (off the chat path). Compare against the user's OTHER files.
    h = _hash(raw)
    others = [f for f in list_files(user_id) if f.get("file_id") != file_id]
    if any(f.get("content_hash") == h for f in others):
        config.ddb.delete_item(TableName=config.USER_FILES_TABLE,
                               Key={"user_id": {"S": user_id}, "file_id": {"S": file_id}})  # drop redundant row
        return {"status": "duplicate", "file_id": file_id, "filename": filename}
    version = max([int(f.get("version", 0)) for f in others if f.get("filename") == filename] or [0]) + 1
    md = _to_markdown(raw, filename)
    try:
        config.s3.put_object(Bucket=config.USER_FILES_BUCKET, Key=md_key,
                             Body=md.encode("utf-8"), ContentType="text/markdown; charset=utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] process: md put failed {md_key}: {e}", flush=True)
        return {"status": "failed", "file_id": file_id, "filename": filename}
    try:
        config.ddb.update_item(TableName=config.USER_FILES_TABLE,
                               Key={"user_id": {"S": user_id}, "file_id": {"S": file_id}},
                               UpdateExpression="SET kb_status = :s, content_hash = :h, version = :v",
                               ExpressionAttributeValues={":s": {"S": "indexing"}, ":h": {"S": h},
                                                          ":v": {"N": str(version)}})
    except Exception:  # noqa: BLE001
        pass
    classify(user_id, file_id, md)                     # lightweight auto-file: category + folder (no digest)
    return {"status": "ingested", "file_id": file_id, "filename": filename}


def classify(user_id: str, file_id: str, md: str) -> None:
    """Worker side: ask a cheap model for a {category, folder} and set them on the row — the lightweight
    replacement for the old digest's auto-filing (labels only, NO summary). Best-effort: a failure leaves
    the file at its staged default (uncategorized, root). Never overrides a folder the user already moved
    off root (so re-running won't undo a manual move). This is an internal classifier prompt, not
    user-facing assistant behavior, so it lives here rather than in a skill."""
    prompt = ("Classify this uploaded document for filing. Return ONLY one line of JSON, nothing else:\n"
              '{"category": "<1-2 word lowercase tag, e.g. finance, fitness, contract, invoice, legal>", '
              '"folder": "<short path starting with /, e.g. /finance/receipts or /health/fitness>"}\n'
              "Base it on the content and the filename. Content (start):\n" + (md or "")[:8000])
    try:
        r = config.bedrock_runtime.converse(
            modelId=config.CODE_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 200, "temperature": 0})
        out = "".join(b.get("text", "") for b in r["output"]["message"]["content"]).strip()
        m = re.search(r"\{.*\}", out, re.S)
        d = json.loads(m.group(0)) if m else {}
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] classify failed for {user_id}/{file_id}: {e}", flush=True)
        return
    category = (str(d.get("category", "") or "uncategorized")[:40]).lower()
    folder = _norm_folder(str(d.get("folder", "/")))
    row = next((f for f in list_files(user_id) if f.get("file_id") == file_id), None)
    cur_folder = (row or {}).get("folder", "/")
    expr, vals = "SET category = :c", {":c": {"S": category}}
    if cur_folder in ("", "/"):                         # only auto-file if the user hasn't moved it
        expr += ", folder = :f"
        vals[":f"] = {"S": folder}
    try:
        config.ddb.update_item(TableName=config.USER_FILES_TABLE,
                               Key={"user_id": {"S": user_id}, "file_id": {"S": file_id}},
                               UpdateExpression=expr, ExpressionAttributeValues=vals)
        config.dlog(f"[user_files] classify {user_id}/{file_id}: cat={category} folder={folder}")
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] classify update failed {user_id}/{file_id}: {e}", flush=True)


def wait_indexed(user_id: str, timeout: int = 240) -> bool:
    """Poll the user's data source until its latest ingestion job is COMPLETE (embeddings queryable).
    The batch trigger fires one job covering every new md, so the latest job is the one to watch.
    Returns True on COMPLETE, False on FAILED/timeout."""
    rec = _kb_record(user_id)
    if not (rec and rec.get("kb_id") and rec.get("ds_id")):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            jobs = _ba.list_ingestion_jobs(knowledgeBaseId=rec["kb_id"], dataSourceId=rec["ds_id"],
                                           maxResults=5, sortBy={"attribute": "STARTED_AT",
                                                                 "order": "DESCENDING"}).get("ingestionJobSummaries", [])
        except Exception as e:  # noqa: BLE001
            print(f"[user_files] wait_indexed list failed for {user_id}: {e}", flush=True)
            return False
        if jobs:
            st = jobs[0]["status"]
            if st == "COMPLETE":
                return True
            if st in ("FAILED", "STOPPED"):
                print(f"[user_files] ingestion ended {st} for {user_id}", flush=True)
                return False
        time.sleep(4)
    print(f"[user_files] ingestion still running after {timeout}s for {user_id}", flush=True)
    return False


def _find(user_id: str, name: str) -> list[dict]:
    """Files matching a name — exact first, else substring (case-insensitive)."""
    files = list_files(user_id)
    exact = [f for f in files if f.get("filename") == name]
    return exact or [f for f in files if name.lower() in (f.get("filename") or "").lower()]


def manage(user_id: str, action: str, query: str = "", file: str = "",
           folder: str = "", category: str = "") -> str:
    """Model-facing dispatcher for the user's own file knowledge. Always keyed on the VERIFIED user_id
    (the model never supplies identity). Returns a string to relay."""
    action = (action or "").lower().strip()
    config.dlog(f"[user_files] manage action={action!r} query={query!r} file={file!r} "
                f"folder={folder!r} category={category!r}")
    if action == "search":
        hits = retrieve(user_id, query or file)
        if not hits:
            return "No matches in your files."
        # Group the matched passages by source file so the model sees which file each came from.
        meta = {f.get("s3_md_key"): f for f in list_files(user_id)}
        groups: dict[str, list[str]] = {}
        for h in hits:
            key = (h.get("source") or "").split(f"{config.USER_FILES_BUCKET}/", 1)[-1]
            groups.setdefault(key, []).append(h["text"][:500])
        out = []
        for key, passages in groups.items():
            f = meta.get(key, {})
            name = f.get("filename") or key.rsplit("/", 1)[-1]
            out.append(f"**{name}**\n" + "\n".join(f"  • {p}" for p in passages))
        return "\n\n".join(out)
    if action == "list":
        return tree(user_id) or "You haven't uploaded any files yet."
    if action in ("remove_all", "clear", "clear_all", "delete_all"):
        n = clear_all(user_id)
        return f"Removed all {n} of your files (and cleared them from search)." if n else \
            "You have no files to remove."
    # move / recategorize / remove all act on one named file
    matches = _find(user_id, file)
    if not matches:
        return f"No file matching '{file}'." if file else "Which file? (give its name)"
    if len(matches) > 1:
        return "More than one file matches — which? " + ", ".join(sorted({m["filename"] for m in matches}))
    target = matches[0]
    fid, fname = target["file_id"], target.get("filename")
    if action == "move":
        return (f"Moved **{fname}** to `{_norm_folder(folder)}`." if move(user_id, fid, folder)
                else "Couldn't move it.")
    if action == "recategorize":
        return (f"Set **{fname}** category to _{category}_." if recategorize(user_id, fid, category)
                else "Couldn't recategorize it.")
    if action == "remove":
        return f"Removed **{fname}**." if remove(user_id, fid) else "Couldn't remove it."
    return f"Unknown action '{action}'. Use: search, list, move, recategorize, remove."


def _norm_folder(path: str) -> str:
    """Normalize a user-facing folder path to a canonical '/a/b' form ('' / '.' → root '/')."""
    parts = [p for p in re.split(r"[\\/]+", (path or "").strip()) if p not in ("", ".")]
    return "/" + "/".join(parts) if parts else "/"


def move(user_id: str, file_id: str, folder: str) -> bool:
    """Move a file into a (virtual) folder/subfolder — just its metadata path; the md object and the
    KB are untouched. Folders are organizational, so the user (and the agent) can reason by layout."""
    try:
        config.ddb.update_item(TableName=config.USER_FILES_TABLE,
                               Key={"user_id": {"S": user_id}, "file_id": {"S": file_id}},
                               UpdateExpression="SET folder = :f",
                               ConditionExpression="attribute_exists(file_id)",
                               ExpressionAttributeValues={":f": {"S": _norm_folder(folder)}})
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[user_files] move failed: {e}", flush=True)
        return False


def tree(user_id: str) -> str:
    """A compact GFM view of the user's folder/file layout, grouped by folder — filenames + categories.
    Cheap; tells the model WHICH files exist so it knows when to search. Used both as the always-on
    context index and as the my_files `list` action. Empty string when the user has no files."""
    files = list_files(user_id)
    if not files:
        return ""
    by_folder: dict[str, list[dict]] = {}
    for f in files:
        by_folder.setdefault(f.get("folder") or "/", []).append(f)
    lines = ["The user's personal files (their own knowledge base), by folder:"]
    for folder in sorted(by_folder):
        lines.append(f"- **{folder}**")
        for f in sorted(by_folder[folder], key=lambda x: x.get("filename", "")):
            cat = f.get("category") or "uncategorized"
            ver = f.get("version")
            tag = f" · {cat}" if cat and cat != "uncategorized" else ""
            vtag = f" v{ver}" if ver and int(ver) > 1 else ""
            lines.append(f"  - {f.get('filename')}{vtag}{tag}")
    return "\n".join(lines)

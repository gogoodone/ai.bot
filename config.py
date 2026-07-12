"""Central configuration for the cleavis.eu runtime: every constant and shared AWS client
lives here so the rest of the package has one source of truth (no scattered os.environ reads).

Defaults target the dev stack; override via env vars if ever run elsewhere.
"""
from __future__ import annotations

import os

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "eu-central-1")
MODEL_ID = os.environ.get("CLEAVIS_MODEL_ID", "eu.anthropic.claude-sonnet-4-6")
# Fast, cheap code writer the orchestrator delegates code-gen to (Haiku).
CODE_MODEL_ID = os.environ.get("CLEAVIS_CODE_MODEL_ID", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")

# Compute / sandbox
CI_ID = os.environ.get("CLEAVIS_CI_ID", "cleavis_ai_ci-ClbOKBk2fk")            # shared VPC CI
MAX_DELIVER_BYTES = 25 * 1024 * 1024                                          # Slack file limit

# Buckets
UPLOAD_BUCKET = os.environ.get("CLEAVIS_UPLOAD_BUCKET", "cleavis-uploads-123456789012")
SKILLS_BUCKET = os.environ.get("CLEAVIS_SKILLS_BUCKET", "cleavis-skills-123456789012")  # shared across bots
SKILLS_MANIFEST = os.environ.get("CLEAVIS_SKILLS_MANIFEST", "index_cleavis_ai.json")
CONFLUENCE_BUCKET = os.environ.get("CLEAVIS_CONFLUENCE_BUCKET", "cleavis-confluence-kb")

# Access registry — a DynamoDB item, one row per bot (key=BOT_ID). bot_users/admins are String Sets
# (atomic add/remove); the rest is a `config` map. Admins manage it in-bot via NLU commands.
BOT_ID = os.environ.get("CLEAVIS_BOT_ID", "cleavis.eu")
REGISTRY_TABLE = os.environ.get("CLEAVIS_REGISTRY_TABLE", "cleavis_access_registry")

# System prompt — now an S3 object in the skills bucket, under the bot name (same store as skills).
SYSTEM_PROMPT_KEY = os.environ.get("CLEAVIS_SYSTEM_PROMPT_KEY", f"{BOT_ID}/SYSTEM.md")
# Params (bot token in SSM; runtime no longer reads any Secrets Manager secret)
BOT_TOKEN_PARAM = os.environ.get("CLEAVIS_BOT_TOKEN_PARAM", "/slack/cleavis.eu/bot_token")

# Memory + budget store — SHARED across the dev & prod bots so a user keeps the same spend and
# preferences whichever bot they talk to.
MEMORY_ID = os.environ.get("CLEAVIS_MEMORY_ID", "cleavis_memory-JTPPAI4MsH")
BUDGET_TABLE = os.environ.get("CLEAVIS_BUDGET_TABLE", "cleavis_user_budgets")

# Knowledge base (direct bedrock-agent-runtime Retrieve; S3_VECTORS KB)
KB_ID = os.environ.get("CLEAVIS_CONFLUENCE_KB_ID", "74ZTF3VBJJ")
CONFLUENCE_BASE = os.environ.get("CLEAVIS_CONFLUENCE_BASE", "https://cleavis-one.atlassian.net/wiki")

# AgentCore Gateway (us-east-1 only; web search lives here, auto-discovered via tools/list)
GW_REGION = os.environ.get("CLEAVIS_GATEWAY_REGION", "us-east-1")
GW_URL = os.environ.get(
    "CLEAVIS_GATEWAY_URL",
    "https://cleavis-web-search-naos9w4olo.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")

# Budget economics — conservative meter (ignores cache discount → never under-charges).
# registry.budget.* can override these per deployment without a redeploy.
PRICE_IN_USD_PER_MTOK = 3.0
PRICE_OUT_USD_PER_MTOK = 15.0
DEFAULT_DAILY_USD = 5.0
DEFAULT_MONTHLY_USD = 50.0
REGISTRY_TTL = 3600  # 1 hour
SLOW_TURN_SECS = int(os.environ.get("CLEAVIS_SLOW_TURN_SECS", "60"))  # flag turns slower than this to the error channel

# Background thread jobs (experimental, OFF by default) — responder reads thread state each turn to
# answer state-aware (worker running vs result ready). Flip CLEAVIS_THREAD_JOBS=on to enable; the
# stable path is untouched when off.
THREAD_JOBS = os.environ.get("CLEAVIS_THREAD_JOBS", "on").lower() == "on"  # ON for dev (experimental); prod default stays off
THREAD_JOBS_TABLE = os.environ.get("CLEAVIS_THREAD_JOBS_TABLE", "cleavis_thread_jobs")
# A "running" job older than this is treated as hung/stale — reclaimable by a new worker, and the
# responder warns the user. Keeps a crashed/hung worker from locking the thread forever.
THREAD_JOB_LEASE_SECS = int(os.environ.get("CLEAVIS_THREAD_JOB_LEASE_SECS", "300"))  # 5 min

# Live-checklist demo: when this exact trigger string appears in a message, the bot posts a checklist
# and ticks the boxes one-by-one (15s apart) by editing the message in place. Self-gating (only this
# string triggers it). Proves the post→update mechanism before the real worker exists.
CHECKLIST_DEMO_TRIGGER = os.environ.get("CLEAVIS_CHECKLIST_DEMO_TRIGGER", "2ocasp0w72sp7sl8ujichephonahe5wl")

# Per-user uploaded-file knowledge (experimental, OFF by default). Each user's files → clean md in
# their S3 prefix + a per-user Bedrock KB (S3 Vectors), queryable on later turns. Keyed on verified user.
USER_FILES = os.environ.get("CLEAVIS_USER_FILES", "on").lower() == "on"  # ON for dev (experimental); prod default stays off
USER_FILES_TABLE = os.environ.get("CLEAVIS_USER_FILES_TABLE", "cleavis_user_files")
USER_FILES_BUCKET = os.environ.get("CLEAVIS_USER_FILES_BUCKET", "cleavis-user-files-123456789012")
# Heavy upload worker (container Lambda): converts staged raw → md, indexes, posts 'searchable now'.
USER_FILES_WORKER = os.environ.get("CLEAVIS_USER_FILES_WORKER", "cleavis-user-files-worker")
# Per-user Jira REST (read) as the user via 3LO OAuth — separate from the DCR/MCP Atlassian provider
# (whose token is MCP-audience-bound). ON for dev. `cleavis-jira-rest` = AtlassianOauth2 provider.
JIRA_REST = os.environ.get("CLEAVIS_JIRA_REST", "on").lower() == "on"
JIRA_PROVIDER = os.environ.get("CLEAVIS_JIRA_PROVIDER", "cleavis-jira-rest")
# External MCP servers (Atlassian/Google/HubSpot/GitHub) per-user. OBSOLETE — superseded by per-user
# REST (jira tool + jira_code). OFF by default; flip on to re-mount (e.g. MCP writes before REST writes).
EXTERNAL_MCP = os.environ.get("CLEAVIS_EXTERNAL_MCP", "off").lower() == "on"

# Verbose tool tracing (tool calls, search queries + returned chunks, skill loads) — OFF by default.
# Flip CLEAVIS_DEBUG_TOOLS=on (runtime + worker env) to diagnose retrieval/tooling, then back off. Errors
# always log regardless; this only gates the high-volume diagnostic lines via dlog().
DEBUG_TOOLS = os.environ.get("CLEAVIS_DEBUG_TOOLS", "off").lower() in ("on", "1", "true")


def dlog(msg: str) -> None:
    """Print a diagnostic line only when CLEAVIS_DEBUG_TOOLS is on. Central switch for all tool tracing."""
    if DEBUG_TOOLS:
        print(msg, flush=True)


SLACK_API = "https://slack.com/api"
# Private Slack channel for runtime error reports (bot must be a member). registry["error_channel"]
# overrides this without a redeploy.
ERROR_CHANNEL = os.environ.get("CLEAVIS_ERROR_CHANNEL", "C0BDHEZEX97")

# --- shared AWS clients (created once per warm container) -------------------
_cfg = Config(retries={"max_attempts": 4, "mode": "adaptive"}, connect_timeout=10)
ddb = boto3.client("dynamodb", region_name=REGION, config=_cfg)
ssm = boto3.client("ssm", region_name=REGION, config=_cfg)
agentcore = boto3.client("bedrock-agentcore", region_name=REGION, config=_cfg)
lambda_ = boto3.client("lambda", region_name=REGION, config=_cfg)        # async-invoke the upload worker
kb_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION, config=_cfg)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION, config=_cfg)  # code-writer model
# Regional endpoint + virtual addressing + s3v4 so presigned PUT URLs don't 301-redirect.
s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                  config=_cfg.merge(Config(s3={"addressing_style": "virtual"}, signature_version="s3v4")))

"""cleavis.eu — Strands agent on AgentCore Runtime (general assistant).

Thin orchestrator: the entrypoint authorizes the verified user, handles budget/commands in
runtime code (never the model), then runs the agent. Everything else lives in focused modules:
  config   constants + shared AWS clients          model     optional router (model-agnostic)
  slack    status + user-label helpers             access    gate / KB-access / admin (registry)
  skills   progressive-disclosure skill catalog    budget    metering, caps, /cost report
  knowledge_base   Confluence retrieve + get_page  commands  /cost /forget /help dispatch

Budget + memory point at SHARED stores (see config) so a user keeps the same spend and
preferences across the dev and prod bots.

Invoke: AgentCore InvokeAgentRuntime with {prompt | command, session_id, actor_id, channel_id,
thread_ts[, model]}; returns {text, usage, files}.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
import uuid

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent, tool
from strands.tools.mcp import MCPClient

import access
import budget
import commands
import config
import external_mcp
import knowledge_base
import model
import skills
import slack

SYSTEM_PROMPT = (config.s3.get_object(Bucket=config.SKILLS_BUCKET, Key=config.SYSTEM_PROMPT_KEY)["Body"].read().decode()
                 + "\n\n===== AVAILABLE SKILLS (call load_skill(name) for full instructions before use) =====\n"
                 + skills.CATALOG)

app = BedrockAgentCoreApp()

# Atlassian MCP is mounted WRITE-ONLY: the `jira` REST tool + the Confluence KB own all reads, so we
# drop the MCP's read/search/get tools (no overlap, deterministic routing, smaller tool surface).
# Keep anything that mutates. Read tools that merely contain a write verb (e.g. getTransitions) slipping
# through is harmless — they support writes anyway.
_MCP_WRITE_VERBS = ("create", "edit", "update", "add", "delete", "remove", "transition", "assign",
                    "move", "archive", "comment", "rank", "link", "attach", "publish", "convert", "set")


# --- Atlassian "code execution" path: inject the per-user token into the CI session (server-side, so
# the value never enters the model's context), then generated Python processes data in the sandbox and
# prints only a concise result. Used for COMPLEX/bulk Jira work; simple lookups use the `jira` tool. ---
_ATL_BOOT = (
    "import os, sys, subprocess\n"
    "os.environ['ATL_TOKEN']={token}\n"
    "os.environ['ATL_CLOUD']={cloud}\n"
    "try:\n    import requests\nexcept ImportError:\n"
    "    subprocess.run([sys.executable,'-m','pip','install','-q','requests']); import requests\n"
    "_S = requests.Session(); _S.headers.update({{'Authorization':'Bearer '+os.environ['ATL_TOKEN'],'Accept':'application/json'}})\n"
    "_BASE = 'https://api.atlassian.com/ex/jira/'+os.environ['ATL_CLOUD']\n"
    "def atl_get(path, **params):\n"
    "    r=_S.get(_BASE+path, params=params, timeout=30); r.raise_for_status(); return r.json()\n"
    "try:\n    from atlassian import Jira\nexcept ImportError:\n"
    "    subprocess.run([sys.executable,'-m','pip','install','-q','atlassian-python-api']); from atlassian import Jira\n"
    "try:\n"
    "    jira = Jira(url=_BASE, token=os.environ['ATL_TOKEN'], cloud=True)\n"
    "except Exception as _e:\n    jira=None; print('WRAPPER_INIT_ERR', repr(_e)[:150])\n"
    "print('ATL_READY')\n"
)


def _mcp_tool_name(t) -> str:
    return (getattr(t, "tool_name", None) or getattr(t, "name", None)
            or (getattr(t, "tool_spec", None) or {}).get("name") or "")


def _write_only(tools: list) -> list:
    return [t for t in tools if any(v in _mcp_tool_name(t).lower() for v in _MCP_WRITE_VERBS)]


def _safe_session_id(raw: str) -> str:
    """AgentCore Memory sessionId must match [a-zA-Z0-9][a-zA-Z0-9-_]* — Slack thread_ts has dots, so
    sanitize before any Memory call (ListEvents/CreateEvent) or the whole turn fails validation."""
    s = re.sub(r"[^a-zA-Z0-9_-]", "-", raw or "").lstrip("-_")
    return (s or "session")[:1024]


def _ci_invoke(sid: str, code: str) -> str:
    """Run one code block in an existing CI session; return stdout."""
    resp = config.agentcore.invoke_code_interpreter(
        codeInterpreterIdentifier=config.CI_ID, sessionId=sid,
        name="executeCode", arguments={"code": code, "language": "python"})
    out = [item["text"] for ev in resp.get("stream", [])
           for item in ((ev.get("result") or {}).get("content", []) or []) if item.get("text")]
    return "\n".join(out) or "(no output)"


# Voice input: transcribe a Slack audio clip to text IN THE SANDBOX (no Lambda / no Amazon Transcribe,
# so the near-zero sandbox role stays intact). Deterministic recipe proven in prod: download → decode
# with the bundled imageio_ffmpeg → whisper "base". One CI call instead of the model improvising ~85s.
_AUDIO_EXT = (".m4a", ".mp3", ".wav", ".ogg", ".oga", ".opus", ".webm", ".aac", ".flac", ".mp4", ".amr")
_WHISPER_CODE = r'''
import subprocess, sys, urllib.request, numpy as np, wave
try:
    import whisper
except Exception:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "openai-whisper"], check=True, timeout=240)
    import whisper
import imageio_ffmpeg
urllib.request.urlretrieve(__URL__, "/tmp/in_audio")
ff = imageio_ffmpeg.get_ffmpeg_exe()
subprocess.run([ff, "-i", "/tmp/in_audio", "-ar", "16000", "-ac", "1", "-f", "wav", "/tmp/aud.wav", "-y"],
               capture_output=True, timeout=120)
with wave.open("/tmp/aud.wav", "rb") as wf:
    raw = wf.readframes(wf.getnframes())
audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
res = whisper.load_model("base").transcribe(audio, fp16=False)
print("TRANSCRIPT_START"); print((res.get("text") or "").strip()); print("TRANSCRIPT_END")
'''


def _transcribe_audio(url: str) -> str:
    """Return the transcript of the audio at `url` (empty string on failure). Self-contained CI session."""
    sess = config.agentcore.start_code_interpreter_session(
        codeInterpreterIdentifier=config.CI_ID, sessionTimeoutSeconds=600)["sessionId"]
    try:
        out = _ci_invoke(sess, _WHISPER_CODE.replace("__URL__", repr(url)))
        if "TRANSCRIPT_START" in out and "TRANSCRIPT_END" in out:
            return out.split("TRANSCRIPT_START", 1)[1].split("TRANSCRIPT_END", 1)[0].strip()
        config.dlog(f"[voice] no transcript in output: {out[:200]}")
        return ""
    finally:
        try:
            config.agentcore.stop_code_interpreter_session(
                codeInterpreterIdentifier=config.CI_ID, sessionId=sess)
        except Exception:  # noqa: BLE001
            pass


def _usage(result) -> dict:
    """Pull token usage (incl. cache) out of the Strands result, defensively."""
    try:
        u = result.metrics.accumulated_usage
        g = (lambda k: int(u.get(k, 0))) if isinstance(u, dict) else (lambda k: int(getattr(u, k, 0)))
        return {"input": g("inputTokens"), "output": g("outputTokens"),
                "cache_read": g("cacheReadInputTokens"), "cache_write": g("cacheWriteInputTokens")}
    except Exception:  # noqa: BLE001
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def _status_for(name: str) -> str:
    n = (name or "").lower()
    if "websearch" in n or "web_search" in n or "web-search" in n:
        return "searching the web…"
    if "knowledge_base" in n or "_kb" in n:
        return "searching the knowledge base…"
    if "code_interpreter" in n or "interpreter" in n:
        return "running a query…"
    if "load_skill" in n:
        return "getting up to speed…"
    if "deliver_file" in n:
        return "preparing your file…"
    return "working on it…"


def _gateway_mcp() -> MCPClient:
    """The AgentCore Gateway as an MCP server (SigV4 as the runtime role); tools auto-discovered."""
    return MCPClient(lambda: aws_iam_streamablehttp_client(
        endpoint=config.GW_URL, aws_region=config.GW_REGION, aws_service="bedrock-agentcore"))


def _report_error(actor_id: str, prompt: str, chain: list[str], err) -> None:
    """Post failure details + root cause to the private error channel (user only sees a one-liner)."""
    from datetime import datetime, timezone
    ch = access.registry().get("error_channel") or config.ERROR_CHANNEL
    if not ch:
        return
    detail = ("⚠️ **cleavis.eu — runtime error**\n"
              f"- Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
              f"- User: `{actor_id}`\n"
              f"- Prompt: {prompt[:300]}\n"
              f"- Models tried: {', '.join(chain)}\n"
              f"- Root cause: `{type(err).__name__}: {str(err)[:400]}`")
    try:
        slack.post(ch, detail)
    except Exception as e:  # noqa: BLE001 — reporting is best-effort
        print(f"error report failed: {e}", flush=True)


def _checklist_demo(channel_id: str, thread_ts: str, owner: str) -> dict:
    """Post a checklist and tick the boxes one-by-one (15s apart) by editing the message in place.
    Also records thread state (running → done) so a concurrent question in the same thread sees a
    worker is in progress. Synchronous (4×15s = 60s) — inside the invoke timeout + edit window."""
    import thread_jobs
    steps = ["Gather data", "Crunch numbers", "Format output", "Finalize"]
    if thread_ts:
        thread_jobs.claim(thread_ts, owner, "checklist demo")     # mark running for this thread

    def render(done: int) -> str:
        rows = "\n".join(("✅ " if i < done else "⬜ ") + s for i, s in enumerate(steps))
        return f"**Checklist demo** ({done}/{len(steps)})\n{rows}"

    ts = slack.post_message(channel_id, render(0), thread_ts)
    for i in range(1, len(steps) + 1):
        time.sleep(15)
        if ts:
            slack.update_message(channel_id, ts, render(i))
    if thread_ts:
        thread_jobs.set_result(thread_ts, "Checklist demo finished — all 4 steps complete.")
    return {"text": "Checklist demo complete ✅ (see the list above).", "command": True}


def _report_slow(actor_id: str, prompt: str, secs: float, model: str, usage: dict, calls: dict) -> None:
    """Flag a slow turn (>SLOW_TURN_SECS) to the error channel with a why — tool calls + token mix
    are the practical cause (many round-trips, or heavy generation)."""
    from collections import Counter
    ch = access.registry().get("error_channel") or config.ERROR_CHANNEL
    if not ch:
        return
    tools = ", ".join(f"{n}×{c}" for n, c in Counter(calls.values()).most_common()) or "none"
    detail = ("🐢 **cleavis.eu — slow turn (>%ds)**\n" % config.SLOW_TURN_SECS
              + f"- Duration: {secs:.0f}s\n- User: `{actor_id}`\n- Model: {model}\n"
              f"- Prompt: {prompt[:300]}\n- Tool calls: {tools}\n"
              f"- Tokens: in {usage.get('input',0)}, out {usage.get('output',0)}, "
              f"cache_read {usage.get('cache_read',0)}, cache_write {usage.get('cache_write',0)}")
    try:
        slack.post(ch, detail)
    except Exception as e:  # noqa: BLE001
        print(f"slow report failed: {e}", flush=True)


@app.entrypoint
def invoke(payload):
    p = payload or {}
    actor_id = p.get("actor_id") or "anonymous"
    channel_id = p.get("channel_id") or ""
    thread_ts = p.get("thread_ts") or ""

    # 1) Explicit slash command -> handled in runtime code, no model.
    cmd = p.get("command") or {}
    if cmd.get("type"):
        return {"text": commands.handle(actor_id, cmd["type"], cmd.get("text", "")), "command": True}

    prompt = (p.get("prompt") or "").strip()

    # 2) Access gate (verified Slack identity, never the model).
    ok, reason = access.gate(actor_id)
    if not ok:
        print(f"gate denied user={actor_id}: {reason}", flush=True)
        return {"text": "Sorry — you're not authorised to use this assistant.", "denied": reason}

    # Persist any uploaded files into the user's PERSONAL knowledge base (dedup / version / folder),
    # keyed on the verified actor — done BEFORE the empty-prompt check so a bare file-drop is still
    # ingested. Each file is converted to markdown and indexed for vector search; the model searches
    # it on demand (no inline reading). First upload per user provisions their KB (~3-5s).
    _uploaded: list[str] = []
    if config.USER_FILES and p.get("uploads"):
        import user_files
        _ups = p["uploads"]
        # VOICE: split audio clips off BEFORE the doc worker (markitdown can't handle audio). Transcribe
        # each in the sandbox and fold the text into the prompt, so a voice message becomes a normal turn.
        _audio = [u for u in _ups if (u.get("name") or u["key"]).lower().endswith(_AUDIO_EXT)]
        if _audio:
            slack.set_status(channel_id, thread_ts, "transcribing your voice message…")
            _spoken = []
            for au in _audio:
                try:
                    _url = config.s3.generate_presigned_url(
                        "get_object", Params={"Bucket": config.UPLOAD_BUCKET, "Key": au["key"]},
                        ExpiresIn=900)
                    _t = _transcribe_audio(_url)
                    if _t:
                        _spoken.append(_t)
                    config.dlog(f"[voice] {au.get('name')}: {_t[:120]!r}")
                except Exception as e:  # noqa: BLE001
                    print(f"[voice] transcribe failed {au.get('name')}: {e}", flush=True)
            if _spoken:
                _voice = " ".join(_spoken).strip()
                prompt = (prompt + "\n" + _voice).strip() if prompt else _voice
            _ups = [u for u in _ups if u not in _audio]     # only real docs go to the worker below
        # ZERO-TOUCH responder: never reads the uploaded bytes. Just write a PENDING row per file
        # (pointing at the staged raw in S3) and async-invoke the worker, which reads each file ONCE to
        # hash → dedup → version → convert → index, then edits the ack below to the final status. Keeps
        # the chat path free of file I/O. Every message posts WITH thread_ts (assistant-thread rule).
        _names, _ids = [], []
        for up in _ups:
            _name = up.get("name") or up["key"].rsplit("/", 1)[-1]
            try:
                r = user_files.stage_pending(actor_id, _name, config.UPLOAD_BUCKET, up["key"],
                                             channel_id=channel_id, thread_ts=thread_ts)
                _names.append(_name); _ids.append(r["file_id"]); _uploaded.append(_name)
                config.dlog(f"[user_files] staged {_name}: {r['file_id']}")
            except Exception as e:  # noqa: BLE001
                print(f"[user_files] stage failed {_name}: {e}", flush=True)
        # Post the ack FIRST and capture its ts, so the worker can UPDATE this same message in place
        # (dedup/new is decided in the worker now, so the ack is generic until it reports back).
        _ack_ts = ""
        if not prompt and _uploaded:
            _ack = (f"📄 Got {', '.join('*' + n + '*' for n in _names)} — indexing now. "
                    "I'll update this message the moment it's searchable.")
            _ack_ts = slack.post_message(channel_id, _ack, thread_ts) or ""
        if _ids:                                            # hand the whole batch to the worker
            try:
                config.lambda_.invoke(
                    FunctionName=config.USER_FILES_WORKER, InvocationType="Event",
                    Payload=json.dumps({"user_id": actor_id, "file_ids": _ids,
                                        "channel_id": channel_id, "thread_ts": thread_ts,
                                        "ack_ts": _ack_ts}).encode())
            except Exception as e:  # noqa: BLE001
                print(f"[user_files] worker invoke failed: {e}", flush=True)

    # Empty prompt — the ack (if any) was already posted above; return silently so the handler doesn't
    # double-post. No uploads → a friendly nudge.
    if not prompt:
        if _uploaded:
            return {"text": "", "command": True, "silent": True}
        return {"text": "Hi! How can I help? Ask me a question and I'll get to work.", "command": True}

    # Live-checklist demo (self-gated by an exact trigger string) — proves post→update in place.
    if config.CHECKLIST_DEMO_TRIGGER and config.CHECKLIST_DEMO_TRIGGER in prompt:
        return _checklist_demo(channel_id, thread_ts, actor_id)

    # 3) Budget pre-flight cap.
    blocked = budget.preflight(actor_id)
    if blocked:
        print(f"budget block user={actor_id}", flush=True)
        return {"text": blocked, "blocked": True}

    slack.set_status(channel_id, thread_ts, "getting started…")
    allowed_kbs = access.allowed_kbs(actor_id)              # trusted allow-list; model can't widen
    session_id = _safe_session_id(p.get("session_id") or f"{actor_id}-adhoc-session")

    mem = AgentCoreMemoryConfig(memory_id=config.MEMORY_ID, session_id=session_id, actor_id=actor_id)

    def _new_session_manager():
        # Fresh per attempt: a session manager binds one Agent (agent_id unique per session), so a
        # fallback retry needs its own manager.
        return AgentCoreMemorySessionManager(agentcore_memory_config=mem, region_name=config.REGION)

    # One CI session per turn so files written in one step survive to deliver_file. Tools are
    # per-request closures (identical schemas every request, so cache_tools still caches them).
    _ci = {"sid": None}
    delivered: list[dict] = []

    def _sid():
        if _ci["sid"] is None:
            _ci["sid"] = config.agentcore.start_code_interpreter_session(
                codeInterpreterIdentifier=config.CI_ID, sessionTimeoutSeconds=3600)["sessionId"]
        return _ci["sid"]

    @tool
    def deliver_file(path: str, filename: str = "") -> str:
        """Send a file you created in the sandbox to the user in Slack. Call AFTER writing the file
        (e.g. /tmp/report.xlsx). `path` is the sandbox path; `filename` the name the user sees. Use for
        any document/chart (xlsx, pdf, docx, pptx, csv, png). Max 25 MB."""
        fn = filename or path.rsplit("/", 1)[-1]
        key = f"out/{session_id}/{uuid.uuid4().hex}/{fn}"
        url = config.s3.generate_presigned_url(
            "put_object", Params={"Bucket": config.UPLOAD_BUCKET, "Key": key}, ExpiresIn=900)
        out = _ci_invoke(_sid(), (
            "import os, urllib.request\n"
            f"p={path!r}\n"
            "sz=os.path.getsize(p)\n"
            f"if sz > {config.MAX_DELIVER_BYTES}:\n"
            "    print('TOO_BIG', sz)\n"
            "else:\n"
            "    d=open(p,'rb').read()\n"
            "    try:\n"
            f"        r=urllib.request.urlopen(urllib.request.Request({url!r}, data=d, method='PUT'), timeout=120)\n"
            "        print('OK' if r.status==200 else 'BAD', r.status, sz)\n"
            "    except Exception as ex:\n"
            "        print('PUT_ERR', repr(ex)[:200])\n"))
        if "OK" in out:
            delivered.append({"key": key, "name": fn})
            return f"Delivered '{fn}' to the user."
        if "TOO_BIG" in out:
            return f"'{fn}' is over the 25 MB limit — not delivered."
        return f"Couldn't deliver '{fn}': {out[:200]}"

    @tool
    def current_datetime() -> str:
        """Return the current date and time (UTC). Call this FIRST whenever the question is relative to
        now — "today", "this month", "this year", "current", "now", "upcoming", "last week" — so you
        anchor to the real date instead of guessing. Cheap and instant; prefer it over the CI."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M UTC")

    @tool
    def code_interpreter(code: str) -> str:
        """Run Python in a sandboxed code interpreter and return whatever it prints. Use it for exact
        calculations, data wrangling (pandas/polars), and building files/charts. State persists across
        calls within this turn. Has internet egress. Only stdout returns — print concise results."""
        return _ci_invoke(_sid(), code)

    try:
        with contextlib.ExitStack() as stack:
            gw_tools = []
            try:                                            # gateway optional (web search); degrade gracefully
                gw = _gateway_mcp()
                stack.enter_context(gw)
                gw_tools = gw.list_tools_sync()
            except Exception:  # noqa: BLE001
                gw_tools = []

            # OBSOLETE (but pluggable): per-user external MCP servers (Atlassian/Google/HubSpot/GitHub),
            # each connected with the user's OAuth bearer via AgentCore Identity. Superseded by the per-user
            # REST path — the `jira` tool (simple) + `jira_code` (bulk, code-on-CI) — which is leaner and
            # token-cheaper, and (once write scopes are added) covers writes too. Left here OFF by default;
            # flip CLEAVIS_EXTERNAL_MCP=on to re-mount (e.g. to restore Jira/Confluence writes via the
            # Atlassian MCP before REST write-scopes land). When off, the model never sees these tools.
            ext_tools, consent = [], []
            if config.EXTERNAL_MCP:
                _mcp_res = access.resources_for(actor_id, "mcp")
                config.dlog(f"[mcp] user {actor_id} mcp resources: {[r for r,_ in _mcp_res]}")
                for rname, meta in _mcp_res:
                    provider, url = meta.get("provider"), meta.get("url")
                    if not (provider and url):
                        continue                            # e.g. mcp:athena (no provider) — not a dev MCP
                    bearer, auth_url = external_mcp.bearer_or_consent(provider, meta.get("scopes") or [], actor_id)
                    if bearer:
                        try:
                            mc = external_mcp.mcp_client(url, bearer)
                            stack.enter_context(mc)
                            _toolset = mc.list_tools_sync()
                            if rname == "atlassian":        # REST + KB own reads → keep only MCP writes
                                _toolset = _write_only(_toolset)
                                config.dlog(f"[mcp] atlassian write-only: {[_mcp_tool_name(t) for t in _toolset]}")
                            ext_tools += _toolset
                        except Exception as e:  # noqa: BLE001
                            print(f"[mcp] mount {rname} failed: {type(e).__name__}: {e}", flush=True)
                    elif auth_url:
                        consent.append(f"{rname}: {auth_url}")

            _seen = {"tool": None}
            _calls: dict[str, str] = {}                     # toolUseId -> name (slow-turn reason)

            def _cb(**kwargs):                              # live per-tool Slack status
                tu = kwargs.get("current_tool_use") or {}
                name, tid = tu.get("name"), tu.get("toolUseId")
                if name and tid:
                    _calls[tid] = name
                if name and name != _seen["tool"]:
                    _seen["tool"] = name
                    config.dlog(f"[agent] tool call: {name}")   # gated agent trace
                    slack.set_status(channel_id, thread_ts, _status_for(name))

            is_adm = access.is_admin(actor_id)         # gates the access SKILL + tool (both admin-only)
            allowed_skills = set(skills.SKILLS) | (set(skills.ADMIN_SKILLS) if is_adm else set())

            @tool
            def load_skill(name: str) -> str:
                """Load a skill's full instructions (its SKILL.md) before doing that kind of task.
                Valid names are in the AVAILABLE SKILLS catalog in the system prompt."""
                if name not in allowed_skills:
                    return f"Unknown skill '{name}'. Available: {', '.join(sorted(allowed_skills))}"
                return skills.read_skill(name)

            @tool
            def manage_budget(action: str, target: str = "", daily: float | None = None,
                              monthly: float | None = None) -> str:
                """Check usage/budget, or (admins) set caps and view others. Load the budget skill for
                how to map a request to arguments. action: view_self, view_top, view_user, set.
                target: the user for view_user/set. daily/monthly: USD caps for set ("10/100" →
                daily=10, monthly=100). Relay the reply verbatim; ambiguous name → it returns
                candidates to ask about."""
                return commands.manage_budget(actor_id, action, target, daily, monthly)

            tools = [code_interpreter, load_skill, deliver_file, current_datetime, manage_budget,
                     *knowledge_base.make_tools(allowed_kbs), *gw_tools, *ext_tools]

            if is_adm:                                 # access management — admins only (tool absent otherwise)
                @tool
                def manage_access(action: str, targets: list[str] | None = None, resource: str = "") -> str:
                    """Manage who can use this bot — add/remove users, make admins, grant/revoke
                    resource access, list access. First load the `access` skill for how to map a
                    request to arguments. Returns the result to relay verbatim; if a name/resource is
                    ambiguous it returns options to ask about — relay that and wait."""
                    return commands.manage_access(actor_id, action, targets or [], resource)
                tools.append(manage_access)

            if config.USER_FILES:                      # the user's personal uploaded-file knowledge
                import user_files

                @tool
                def my_files(action: str, query: str = "", file: str = "", folder: str = "",
                             category: str = "") -> str:
                    """The user's PERSONAL uploaded-file knowledge base (private to them). Actions:
                    search (semantic search — pass `query` — whenever their question might be answered by
                    something they uploaded), list, move (`file` + `folder` e.g. /health/fitness),
                    recategorize (`file` + `category`), remove (`file`), remove_all (destructive — confirm
                    first unless they clearly said to delete everything). Load the `my_files` skill for HOW
                    to answer from files. Ambiguous file name → it returns options to ask about."""
                    return user_files.manage(actor_id, action, query, file, folder, category)
                tools.append(my_files)

            if config.JIRA_REST:                       # the user's Jira, queried AS them via 3LO OAuth
                import jira_rest
                _JIRA_SCOPES = ["read:jira-work", "read:jira-user", "read:me", "offline_access"]

                @tool
                def jira(action: str, jql: str = "", limit: int = 25) -> str:
                    """Read the user's Jira AS them (their `currentUser()`), private to them. action='whoami'
                    confirms the connection; action='search' runs JQL (pass `jql`) and returns issues. For
                    creating/commenting/transitioning, use the Atlassian write tools, not this. Load the
                    `jira` skill for HOW to use Jira (read/write routing, JQL patterns, reports, grounding).
                    If Jira isn't connected, this returns a one-time connect link — relay it and retry."""
                    bearer, auth_url = external_mcp.bearer_or_consent(config.JIRA_PROVIDER, _JIRA_SCOPES, actor_id)
                    if auth_url:
                        return f"🔗 Connect your Jira first (one-time): {auth_url}\nThen ask me again."
                    if not bearer:
                        return "Jira isn't available right now — please try again in a moment."
                    sites = jira_rest.accessible_sites(bearer)
                    if not sites:
                        return "I couldn't reach your Jira sites — the token may lack access. Try reconnecting Jira."
                    cloud_id = sites[0]["id"]
                    act = (action or "search").lower()
                    if act == "whoami":
                        me = jira_rest.myself(bearer, cloud_id)
                        if me.get("error"):
                            return f"Couldn't fetch your Jira identity ({me['error']}): {str(me.get('body'))[:200]}"
                        site = sites[0].get("name") or sites[0].get("url") or cloud_id
                        config.dlog(f"[jira] whoami ok: {me.get('accountId')} on {site}")
                        return (f"✅ Connected to Jira as **{me.get('displayName')}** "
                                f"({me.get('emailAddress', 'email hidden')}) on {site}. "
                                f"accountId `{me.get('accountId')}`.")
                    if act == "search":
                        q = jql or "assignee = currentUser() ORDER BY updated DESC"
                        data = jira_rest.search(bearer, cloud_id, q, limit)
                        if data.get("error"):
                            return f"Jira search failed ({data['error']}): {str(data.get('body'))[:300]}"
                        issues = data.get("issues", [])
                        config.dlog(f"[jira] search q={q!r} -> {len(issues)} issues (REST)")
                        if not issues:
                            return f"No issues matched: {q}"
                        lines = [f"- {i.get('key')}: {(i.get('fields') or {}).get('summary', '')[:80]} "
                                 f"[{((i.get('fields') or {}).get('status') or {}).get('name', '')}]"
                                 for i in issues]
                        return f"Found {len(issues)} issue(s) for `{q}`:\n" + "\n".join(lines)
                    return "Unknown action — use 'whoami' or 'search' (with a `jql`)."
                tools.append(jira)

                @tool
                def jira_code(code: str) -> str:
                    """Run Python against the user's Jira for COMPLEX/bulk work — reports, aggregations,
                    counts/grouping across many issues, comments across issues, "what did I do in the last
                    N months". YOU write the Python and pass it as `code`. The sandbox already has, ready
                    to use (do NOT set up auth): `jira` (atlassian-python-api client; use jira.jql(JQL,
                    limit=100, start=0)) and `atl_get(path, **params)` (authenticated GET, e.g.
                    atl_get('/rest/api/3/search/jql', jql="assignee = currentUser()", maxResults=100)).
                    Scope JQL to the user with currentUser(); paginate; process in Python; PRINT ONLY a
                    concise summary — never dump raw JSON. For a single quick lookup use `jira` instead.
                    Load the `jira` skill for the recipe + examples."""
                    bearer, auth_url = external_mcp.bearer_or_consent(config.JIRA_PROVIDER, _JIRA_SCOPES, actor_id)
                    if auth_url:
                        return f"🔗 Connect your Jira first (one-time): {auth_url}\nThen ask me again."
                    if not bearer:
                        return "Jira isn't available right now — please try again in a moment."
                    sites = jira_rest.accessible_sites(bearer)
                    if not sites:
                        return "I couldn't reach your Jira sites — try reconnecting Jira."
                    sid = _sid()
                    boot = _ci_invoke(sid, _ATL_BOOT.format(token=repr(bearer), cloud=repr(sites[0]["id"])))
                    if "ATL_READY" not in boot:                # token/setup runs server-side; never reaches the model
                        print(f"[jira_code] bootstrap failed: {boot[:200]}", flush=True)
                        return "Couldn't set up the Jira sandbox — please try again."
                    out = _ci_invoke(sid, code)                # the orchestrator's own Python — no second model
                    config.dlog(f"[jira_code] ran {len(code)} chars -> {len(out)} chars out")
                    return out[:6000] or "(the Jira script produced no output)"
                tools.append(jira_code)

            # STABLE system prompt (S3 SYSTEM.md + skill catalog + admin catalog) — identical across a
            # user's turns, so it stays a WARM cached prefix (Anthropic prompt cache, 1h TTL). All the
            # per-turn dynamic context (current file list, connectable integrations, background-task
            # result) rides in the user MESSAGE below instead, so it never busts the system cache.
            sys_prompt = SYSTEM_PROMPT + (skills.ADMIN_CATALOG if is_adm else "")
            _ctx: list[str] = []
            if config.USER_FILES:                      # the user's current file layout (folders) — data, not behavior
                import user_files
                _tree = user_files.tree(actor_id)
                # AUTHORITATIVE: uploads are silent side-effects that don't enter conversation memory, so
                # the model must trust this live list over anything earlier in the chat (otherwise it can
                # wrongly answer 'you have no files' right after an upload). HOW to answer lives in the
                # my_files skill, not here.
                if _tree:
                    _ctx.append("[" + _tree + "\nThis is the user's CURRENT, authoritative uploaded-file "
                                "list as of right now — it overrides anything earlier in the conversation "
                                "about which files they have. Use the my_files tool to search/manage them, "
                                "and load the `my_files` skill for how to answer from them.]")
                else:
                    _ctx.append("[The user currently has NO uploaded files (authoritative as of now). "
                                "If they ask to remove files, tell them there are none.]")
            if consent:                                    # granted but not yet connected → offer the link
                _ctx.append("[Connectable integrations: the user has access to these but hasn't connected "
                            "them yet. If they ask about one, give them its link to connect (one-time), "
                            "then they can retry:\n- " + "\n- ".join(consent) + "\n]")
            # Thread-state awareness (flag-gated): RUNNING is enforced deterministically (a prefix the
            # model can't skip — a soft note loses to "reply to hey"); DONE is fed to the model to use.
            _thread_prefix = ""
            if config.THREAD_JOBS and thread_ts:
                import thread_jobs
                _job = thread_jobs.get(thread_ts)
                if _job and _job.get("status") == "running":
                    _fresh = (time.time() - float(_job.get("updated", 0))) <= config.THREAD_JOB_LEASE_SECS
                    _thread_prefix = ("⏳ _Still working on your earlier task in this thread — keeping that going._\n\n"
                                      if _fresh else
                                      "⚠️ _Your earlier task is taking longer than expected._\n\n")
                elif _job and _job.get("status") == "done" and _job.get("result"):
                    _ctx.append(f"[A background task for this thread finished. Use its result:\n{_job['result']}\n]")
            # Per-turn context prepended to the user's message (keeps the system prompt cache-stable).
            _user_msg = ("\n\n".join(_ctx) + "\n\n" + prompt) if _ctx else prompt

            # Model fail-over: try the routed primary (e.g. Nova on dev), fall back to Sonnet on a
            # hard invoke error. Same tools/session; only the model swaps.
            t0 = time.monotonic()
            chain = model.chain(p)
            result, served, last_err = None, None, None
            config.dlog(f"[agent] turn actor={actor_id} tools={len(tools)}(gw{len(gw_tools)}/ext{len(ext_tools)}) "
                        f"chain={chain} ctx={len(_ctx)}blk prompt={prompt[:100]!r}")
            for mid in chain:
                try:
                    agent = Agent(model=model.build(mid), system_prompt=sys_prompt, tools=tools,
                                  session_manager=_new_session_manager(), callback_handler=_cb)
                    result = agent(_user_msg)
                    served = mid
                    break
                except Exception as e:  # noqa: BLE001 — try the next model in the chain
                    last_err = e
                    print(f"model {mid} failed: {type(e).__name__}: {e}", flush=True)
            if result is None:
                print(f"all models failed: {last_err}", flush=True)
                _report_error(actor_id, prompt, chain, last_err)
                return {"text": "⚠️ Something went wrong — the team's been notified. Please try again shortly.",
                        "error": str(last_err)}

            elapsed = time.monotonic() - t0
            usage = _usage(result)
            try:
                budget.charge(actor_id, usage)              # exact usage, charged in-runtime
            except Exception as e:  # noqa: BLE001 — never fail a reply over metering
                print(f"budget charge failed: {e}", flush=True)
            if elapsed > config.SLOW_TURN_SECS:
                _report_slow(actor_id, prompt, elapsed, served, usage, _calls)
            config.dlog(f"[agent] served={served} elapsed={elapsed:.1f}s usage={usage} "
                        f"tools_called={list(_calls.values())}")
            return {"text": _thread_prefix + str(result), "usage": usage, "files": delivered, "model": served}
    finally:
        if _ci["sid"]:
            try:
                config.agentcore.stop_code_interpreter_session(
                    codeInterpreterIdentifier=config.CI_ID, sessionId=_ci["sid"])
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    app.run()

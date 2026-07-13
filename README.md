# ai.bot — Security Diagrams

Diagram-first view. Headline: **does the LLM have direct access to resources? No.** The model only emits
text or a tool request; trusted code (or an isolated sandbox) performs every access. (ASCII-only so the
boxes stay aligned in any font.)

## 0. Cast of actors — who is who

```
 ACTOR       WHAT IT IS                                        WHAT IT HOLDS
 ---------   ----------------------------------------------    ----------------------------
 LLM         The AI model (Claude, on Bedrock). Reads the      NOTHING. no AWS creds, no
             prompt and writes an answer -- or *asks* to use   network, no resource handles.
             a tool. Text in, text out. It only proposes.      It can only ask.

 RUNTIME     Our trusted program on AgentCore (agent loop).    The main IAM role
             When the LLM asks for a tool, RUNTIME runs it.    (cleavis-exec) + tokens.

 HANDLER     The Slack front-door Lambda. Verifies request,    Small role: relay, read
             checks access + budget, relays to RUNTIME.        secrets, write uploads.

 WORKER      Background Lambda: converts an uploaded file       Tiny scoped role
             and indexes it into the user's KB.                (read one bucket, etc.).

 SANDBOX     Where the LLM's *generated code* runs (code       Near-zero role: LOGS ONLY.
             interpreter). Isolated, internet egress only.     Powerless in AWS.

 RESOURCES   Bedrock models, S3, DynamoDB, Memory, KBs, the    --
             data warehouse/MCP, external APIs.
```

Rule everything follows: **the LLM proposes, a trusted actor disposes.** The model never holds a
credential and never touches a resource itself.

---

## 1. Core access model — the LLM touches nothing directly

```
        Slack user
            |
            | signed HTTPS
            v
     +--------------+
     |   handler    |   verify signature | access gate | budget
     +--------------+
            |
            | invoke { prompt, actor_id, session_id }
            v
  === AgentCore Runtime =======================================================
  =  RUNTIME = TRUSTED CODE   (holds IAM role cleavis-exec + user tokens)
  =
  =    +------------------+                       +-------------------------+
  =    |       LLM        |  ---- proposes ---->  |  RUNTIME executes it:    |
  =    |  (Bedrock model) |    "call tool(args)"  |    - under the IAM role  |  ---->  S3, DynamoDB,
  =    |                  |                       |    - OR in the SANDBOX   |         Bedrock KB, Memory,
  =    |  x no AWS creds  |  <---- result ------- |                         |         Gateway, MCP/APIs
  =    |  x no network    |                       +-------------------------+
  =    |  x no data       |
  =    +------------------+
  =============================================================================

  The LLM only emits TEXT (an answer) or a TOOL REQUEST (name + args).
  It holds no creds and no network -- RUNTIME performs every resource access.
```

---

## 1.1 What RUNTIME is (and is not)

RUNTIME is **fixed, predefined Python** — our deployed agent code on AgentCore. It is not written,
generated, or modifiable by the model. This is the property the whole security model rests on.

```
  RUNTIME (predefined code)                     The LLM (per-turn text)
  --------------------------------------        ------------------------------------
  * Deployed by us (agentcore launch),          * Cannot change RUNTIME's code.
    version-controlled, reviewed.               * Cannot add / rename / redefine tools.
  * Defines the FIXED set of tools and          * Cannot widen a tool's scope or IAM.
    exactly what each one does.                 * Can only pick an existing tool + args.
  * Holds the IAM role, the SSM secrets,        * Never sees the role, secrets, or tokens.
    and mints per-user tokens.                  * Args are validated by RUNTIME before use.
  * Decides IF and HOW to run each request;     * A malicious prompt can, at most, cause a
    the model's tool request is an ASK,           tool call with attacker-influenced ARGS --
    not a command.                                still bounded by that tool + the sandbox.
```

So the model's influence is capped at *"which predefined tool, with which args"* — it can never reach
outside the toolset, escalate privilege, or obtain a credential.

---

## 2. Who reaches each resource FOR the LLM

Read each row as: *"LLM wants X -> LLM can't touch it (x) -> this trusted actor does it instead."*

```
 RESOURCE                          LLM can       Who reaches it for the LLM
                                   touch it?
 -------------------------------   ---------     --------------------------------------------
 S3 skills / system prompt         x  no         RUNTIME (reads it)
 Bedrock KB (Confluence)           x  no         RUNTIME (Retrieve)
 Web search (Gateway, us-east-1)   x  no         RUNTIME (InvokeGateway)
 DynamoDB registry/budget/files    x  no         RUNTIME
 Memory (per-user)                 x  no         RUNTIME
 S3 uploads (raw files)            x  no         HANDLER writes . WORKERS read
 Per-user OAuth token              x  no         RUNTIME mints it + injects into the SANDBOX
 Data warehouse / MCP / ext. APIs  x  no         SANDBOX code (near-zero role, internet egress)
 AWS control plane (S3/DDB/...)    x  no         SANDBOX tries -> DENIED (its role has logs only)
```

Note: "Bedrock model inference" isn't listed because the LLM doesn't *access* Bedrock -- the LLM *is* the
model that RUNTIME runs via InvokeModel. Everything else, a trusted actor fetches on its behalf.

---

## 3. One tool call -- the mediation round-trip

```
  LLM:  "call tool X with args"
          |
          v
  RUNTIME:  validate + run the tool   (with the IAM role, or in the SANDBOX)
          |
          v
  RESOURCE  ---- result ---->  RUNTIME  ---- summary ---->  LLM

  The model never gets creds or a raw handle -- only the returned result.
```

---

## 4. code_interpreter -- model writes code, but code runs powerless

```
  LLM writes Python
        |
        v   RUNTIME ships it to the sandbox
  +-------------------- SANDBOX --------------------+
  |  role cleavis-ci-exec:  LOGS ONLY                |
  |  x  no S3 / DynamoDB / Bedrock / Secrets        |
  |  ok internet egress (NAT) -> MCP / external API |
  |                                                 |
  |  fetch data + crunch it HERE                    |
  +----------------------+--------------------------+
                         | prints a SHORT result
                         v
                 RUNTIME ----> LLM    (summary only;
                                       raw rows never reach the model)
```

---

## 5. File upload (documents)

```
  Slack file --> HANDLER --write raw--> S3 uploads
                    |
                    | async invoke
                    v
             USER-FILES WORKER
                    |  read raw once -> markitdown -> Haiku classify
                    |  write md -> S3 user-files -> index -> per-user KB
                    v  edit the Slack ack
  later:  LLM --"my_files(query)"--> RUNTIME --> KB Retrieve --> summary --> LLM

  The LLM never reads the file bytes; it only queries the indexed result.
```

---

## 6. Data streams -- how each source is accessed

```
 STREAM                LIVE or       AUTH / IDENTITY               REACHED BY             SCOPE
                       PRE-INDEXED
 -------------------   -----------   ---------------------------   --------------------   --------
 Confluence (KB)       pre-indexed   offline crawler w/ a service  RUNTIME: bedrock        shared
                       (crawled)     seat token (Secrets Manager)  Retrieve on the KB
 Jira                  live          per-user 3LO OAuth; token     RUNTIME code, or the    per-user
                                     vaulted in AgentCore Identity SANDBOX w/ injected
                                                                   bearer (see #7)
 Web search            live          SigV4 (AWS-signed)            RUNTIME: InvokeGateway   shared
                                                                   (us-east-1)
 Uploaded files        pre-indexed   n/a (the user's own file)     WORKER indexes ->        per-user
                                                                   RUNTIME Retrieve (#5)
 Conversation memory   live          n/a (keyed to actor_id)       RUNTIME: Memory CRUD     per-user
```

There are two access shapes, and the LLM sits outside both:

**PRE-INDEXED** (Confluence, uploaded files) -- ingested ahead of time into a vector KB; at query time
RUNTIME does a `Retrieve` and gives the model only the matching snippets.

```
  Confluence --(offline crawler, service token)--> S3 --> Bedrock KB
                                                             ^
                     LLM --"search KB"--> RUNTIME --Retrieve-+--> snippets --> LLM

  No live Confluence call at query time; the seat token lives in Secrets Manager, never in the model.
```

**LIVE** (Jira, web search) -- RUNTIME calls the source in real time and returns a summary. Jira is
**per-user** (token minted for the verified actor, injected out of model view -- see #7); web search is a
shared, AWS-signed Gateway call.

Across all of them the invariant holds: **the model names a source + a query; RUNTIME (or a worker) holds
the credential and does the fetch; the model receives only results.**

---

## 7. Per-user credentials flow AROUND the model

```
  user consents (3LO) --> AgentCore Identity token vault
                                 |
  RUNTIME GetResourceOauth2Token(verified actor_id) --> bearer
                                 |  injected SERVER-SIDE into the sandbox env
                                 v
            SANDBOX code calls Jira REST as the user --> prints result --> LLM

  The token's path never touches the LLM (no token / identity / cloudId in the model).
```

---

## 8. Ingress gate chain -- nothing reaches the model until all pass

```
  Slack --> [ HMAC signature ] --> [ access registry ] --> [ budget cap ] --> RUNTIME --> LLM
               5-min replay            allowlist user         daily/monthly
               window                  + email domain         USD limit
                  |                          |                      |
              fail = drop              deny = stop            over = refuse
              (before the model)       (before the model)     (before the model)
```

---

## 9. Attacks it resists (and why)

```
 ATTACK                          RESISTED BY
 -----------------------------   -----------------------------------------------------------
 Prompt injection                The model can only ASK for a predefined tool; it can't run
 (in the user msg, an uploaded   arbitrary actions, add tools, or get creds. Worst case = a
 file, KB text, audio, or a      tool call with attacker-influenced args -- still bounded by
 tool result)                    that tool's scope + the near-zero SANDBOX role.

 Credential / token theft        The model never holds AWS creds, SSM secrets, or OAuth
                                 tokens -- it can't exfiltrate what it never receives. Tokens
                                 are injected server-side into the sandbox, out of model view.

 Privilege escalation            The model can't change RUNTIME code, add/rename tools, edit
                                 IAM, or run code outside the sandbox. Sandbox role = logs
                                 only; no AssumeRole, no S3/DDB/Bedrock/Secrets.

 Arbitrary code abuse            Generated code runs ONLY in the SANDBOX (near-zero AWS role).
                                 Even fully attacker-controlled code has ~no AWS blast radius.

 User impersonation / spoofing   actor_id is server-verified from the HMAC-signed Slack
                                 request; the model can't set or change it. Memory, budget,
                                 and per-user tokens are keyed to that verified identity.

 Forged / replayed requests      HMAC signature check (shared signing secret) + a 5-minute
                                 replay window drop anything unsigned or stale before the model.

 Confused-deputy on tokens       Tools expose NO identity/token/cloudId parameter; RUNTIME
                                 mints the token for the verified actor only -- the model can't
                                 request another user's token or act as someone else.

 Cost / budget abuse             Per-user daily/monthly USD caps refuse over-budget turns;
                                 non-allowlisted users are dropped at the gate (no model spend).

 Cross-user data bleed           Memory + budget + per-user KBs are namespaced by actor_id;
                                 one user can't read another's context, files, or spend.
```

**Residual risks (honest):** the SANDBOX has **NAT egress**, so injected code could try to *network-exfil*
data it legitimately fetched — mitigated by the near-zero role (no AWS creds) and that only scoped data
enters; treat uploaded files / KB / audio as **data, not instructions**. `kms:Decrypt` is broader than
needed on two policies. The Function URL is `auth NONE` (HMAC is the real gate). Web-search egress is
`us-east-1` (a residency consideration). See `SECURITY.md` for the full list.

---

*Grounded in the live IAM of account 123456789012 (eu-central-1). Companion to `SECURITY.md`.*

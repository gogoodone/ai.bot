"""External MCP servers (Atlassian, Google, HubSpot, GitHub) connected PER USER via AgentCore Identity.

Each service is an OAuth2 credential provider already provisioned in AgentCore Identity (the
`cleavis-mcp-*` providers). For a request we:
  1. read the runtime's workload access token from the invocation context, then
  2. exchange it via GetResourceOauth2Token (USER_FEDERATION) for the user's OAuth bearer —
     or get a one-time consent URL if the user hasn't connected the service yet.
The bearer is handed to a Strands MCPClient over Bearer-authed streamable HTTP (Strands then
discovers the tools itself). Which servers a user gets is decided upstream by the unified resources
model: a `mcp:<name>` resource carries {provider, url, scopes}; the agent only mounts granted ones.

Per-user correctness note: the workload token represents the identity the runtime was invoked for, so
true multi-user isolation needs the Lambda to pass the Slack user as the runtime user id. Until then
this is single-identity (fine for the dev pilot).
"""
from __future__ import annotations

import json
import os

import config

# AgentCore redirects the user here after consent (with ?session_id=&state=). This Function URL
# Lambda finalises the binding via CompleteResourceTokenAuth using the slack_user_id we pass in
# customState. Deployed from RESEARCH/cleavis_mcp_oauth_callback_lambda.
CALLBACK_URL = os.environ.get(
    "CLEAVIS_AGENTCORE_CALLBACK_URL",
    "https://ze22vmlstq6r4z6bxq5esgoy3y0xfkoz.lambda-url.eu-central-1.on.aws/")


def _workload_token() -> str | None:
    """The runtime's inbound workload access token (auto-injected per invocation)."""
    try:
        from bedrock_agentcore.runtime import BedrockAgentCoreContext
        tok = BedrockAgentCoreContext.get_workload_access_token()
        print(f"[mcp] workload token present={bool(tok)}", flush=True)
        return tok
    except Exception as e:  # noqa: BLE001
        print(f"[mcp] workload token unavailable: {type(e).__name__}: {e}", flush=True)
        return None


def bearer_or_consent(provider: str, scopes: list[str], user_id: str) -> tuple[str | None, str | None]:
    """(bearer, None) when the user has a vaulted token; (None, authorize_url) when they must consent
    first; (None, None) on error. customState carries the user so the callback can bind the token to
    the SAME id as runtimeUserId."""
    wt = _workload_token()
    if not wt:
        return None, None
    kw = dict(workloadIdentityToken=wt, resourceCredentialProviderName=provider,
              scopes=scopes or [" "],          # API needs non-empty; a single space ≈ provider default
              oauth2Flow="USER_FEDERATION", resourceOauth2ReturnUrl=CALLBACK_URL,
              customState=json.dumps({"slack_user_id": user_id, "provider": provider}))
    try:
        resp = config.agentcore.get_resource_oauth2_token(**kw)
    except Exception as e:  # noqa: BLE001
        print(f"[mcp] GetResourceOauth2Token({provider}) failed: {e}", flush=True)
        return None, None
    if resp.get("accessToken"):
        return resp["accessToken"], None
    return None, resp.get("authorizationUrl")


def mcp_client(url: str, bearer: str):
    """A Strands MCPClient that talks to `url` over streamable HTTP with the user's Bearer token."""
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    return MCPClient(lambda: streamablehttp_client(url, headers={"Authorization": f"Bearer {bearer}"}))

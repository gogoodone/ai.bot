"""Code writer — delegates Python code generation to a fast, cheap model (Haiku) so the orchestrator
(Sonnet) doesn't spend output tokens writing routine code. The orchestrator describes the task; this
returns runnable Python, which the agent then executes on the CI.

Grounding is the caller's job: the code model does NOT see the conversation, so the orchestrator must
put every needed detail (data sources, columns, values, exact table names for SQL) into the task.
"""
from __future__ import annotations

import re

import config

_PROMPT = (
    "Write a single self-contained Python script for the task below. Standard library plus "
    "pandas/numpy/openpyxl if helpful. Print the result concisely (no giant dumps). Save any files "
    "under /tmp. Output ONLY the Python code — no prose, no markdown fences.\n\nTask:\n{task}")


def generate(task: str) -> str:
    r = config.bedrock_runtime.converse(
        modelId=config.CODE_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": _PROMPT.format(task=task)}]}],
        inferenceConfig={"temperature": 0, "maxTokens": 2500})
    txt = "".join(b.get("text", "") for b in r["output"]["message"]["content"])
    m = re.search(r"```(?:python)?\s*\n(.*?)```", txt, re.S)   # strip fences if the model added them
    return (m.group(1) if m else txt).strip()

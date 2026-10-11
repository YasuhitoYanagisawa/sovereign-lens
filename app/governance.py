"""Governance layer: input screening, tool allow-list, call budget, audit log.

Implemented as an ADK Plugin so every agent/tool call in the run passes through it.
Audit records are emitted as structured JSON on stdout, which Cloud Run ships to
Cloud Logging (filterable by jsonPayload.audit=true).
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin

log = logging.getLogger("audit")

_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|prompts?)",
    r"disregard (the )?(system|previous) (prompt|instructions)",
    r"(reveal|print|show) (your|the) (system )?(prompt|instructions)",
    r"you are now (a|an|the)\b",
    r"jailbreak|DAN mode|developer mode",
    r"(以前|前|上記|これまで)の(指示|命令|プロンプト)を(無視|忘れ)",
    r"システムプロンプトを(表示|出力|教え)",
    r"あなたは(今から|これから).{0,20}(として|になりきって)",
    r"(発注|注文|売買)を(実行|自動で|代わりに)",
    r"(パスワード|暗証番号|口座番号|API ?キー)",
]
_INJ = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]
MAX_INPUT_CHARS = 2000


def screen_text(text: str) -> tuple[bool, str | None]:
    """Return (allowed, reason)."""
    if text is None:
        return True, None
    if len(text) > MAX_INPUT_CHARS:
        return False, f"入力が長すぎます（{MAX_INPUT_CHARS}文字まで）"
    for p in _INJ:
        if p.search(text):
            return False, "安全ポリシーにより処理できない指示が含まれています（指示の上書き・発注依頼・認証情報など）"
    return True, None


def audit(event: str, **fields: Any) -> None:
    rec = {"audit": True, "event": event, "ts": time.time(), "severity": fields.pop("severity", "INFO"), **fields}
    sys.stdout.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


class GovernancePlugin(BasePlugin):
    """Allow-list + per-run tool budget + audit trail for every tool call."""

    def __init__(self, allowed_tools: set[str], max_tool_calls: int = 30, per_tool_limits: dict[str, int] | None = None):
        super().__init__(name="governance")
        self.allowed_tools = allowed_tools
        self.max_tool_calls = max_tool_calls
        self.per_tool_limits = per_tool_limits or {}
        self._calls: dict[str, int] = {}
        self._per_tool: dict[str, int] = {}
        self._t0: dict[str, float] = {}

    async def on_user_message_callback(self, *, invocation_context, user_message):
        text = "".join(p.text or "" for p in (user_message.parts or []))
        ok, reason = screen_text(text)
        audit("user_message", invocation_id=invocation_context.invocation_id, chars=len(text), allowed=ok, reason=reason)
        if not ok:
            from google.genai import types

            return types.Content(role="user", parts=[types.Part(text=f"[SYSTEM GUARD] ユーザー入力はブロックされました: {reason}。安全上の理由で対応できないことだけを丁寧に伝えてください。")])
        return None

    async def before_tool_callback(self, *, tool, tool_args, tool_context) -> Optional[dict]:
        inv = tool_context.invocation_id
        n = self._calls.get(inv, 0) + 1
        self._calls[inv] = n
        self._t0[f"{inv}:{tool.name}:{n}"] = time.time()
        if tool.name not in self.allowed_tools:
            audit("tool_blocked", tool=tool.name, invocation_id=inv, severity="WARNING")
            return {"error": f"tool '{tool.name}' is not allowed by policy"}
        if n > self.max_tool_calls:
            audit("tool_budget_exceeded", tool=tool.name, invocation_id=inv, calls=n, severity="WARNING")
            return {"error": "tool call budget exceeded for this run; summarise with what you have"}
        key = f"{inv}:{tool.name}"
        self._per_tool[key] = self._per_tool.get(key, 0) + 1
        limit = self.per_tool_limits.get(tool.name)
        if limit and self._per_tool[key] > limit:
            audit("tool_limit_reached", tool=tool.name, invocation_id=inv, severity="WARNING")
            return {"error": f"'{tool.name}' は1回の実行で{limit}回までです。これ以上の修正は行わず、直近のプランと審査結果（未解決の指摘はリスク欄に明記）で最終回答を書いてください。"}
        audit("tool_call", tool=tool.name, args=tool_args, invocation_id=inv, call_no=n)
        return None

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result) -> Optional[dict]:
        inv = tool_context.invocation_id
        size = len(json.dumps(result, ensure_ascii=False, default=str)) if result is not None else 0
        audit("tool_result", tool=tool.name, invocation_id=inv, result_chars=size, error=(result or {}).get("error") if isinstance(result, dict) else None)
        return None

    async def after_run_callback(self, *, invocation_context) -> None:
        inv = invocation_context.invocation_id
        audit("run_complete", invocation_id=inv, tool_calls=self._calls.pop(inv, 0))
        for k in [k for k in self._per_tool if k.startswith(inv)]:
            self._per_tool.pop(k, None)
        for k in [k for k in self._t0 if k.startswith(inv)]:
            self._t0.pop(k, None)

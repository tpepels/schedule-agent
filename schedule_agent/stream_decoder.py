"""Render `claude -p --output-format stream-json` events as readable text.

`claude -p` with the default text format buffers all output until the
response is complete, leaving the per-job log file empty for the entire
run and only filling it with the final assistant text at exit. That
breaks `tail -f` for running jobs and makes the log of a completed job
look like "only the last bit of output".

`stream-json` output, in contrast, flushes one JSON event per line as
work happens (init, tool uses, text deltas, results). The wrapper script
pipes claude's stdout through this decoder so the log file gets a
readable stream of what claude is doing, in real time.

Codex `exec` already streams plain text, so its log path is unchanged.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator
from typing import Any, TextIO


def _render_tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def render_event(event: dict) -> Iterator[str]:
    """Yield zero or more chunks to write for a single stream-json event."""
    etype = event.get("type")

    if etype == "system":
        sub = event.get("subtype")
        if sub == "init":
            session = (event.get("session_id") or "")[:12]
            model = event.get("model", "?")
            mode = event.get("permissionMode", "?")
            yield f"[claude] session={session} model={model} permission={mode}\n"
        return

    if etype == "stream_event":
        inner = event.get("event") or {}
        itype = inner.get("type")
        if itype == "content_block_start":
            block = inner.get("content_block") or {}
            btype = block.get("type")
            if btype == "tool_use":
                yield f"\n[tool_use: {block.get('name', '?')}]\n"
            elif btype == "thinking":
                yield "\n[thinking]\n"
            elif btype == "text":
                yield "\n[assistant]\n"
            return
        if itype == "content_block_delta":
            delta = inner.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                yield delta.get("text", "")
            elif dtype == "thinking_delta":
                yield delta.get("thinking", "")
            return
        if itype == "message_stop":
            yield "\n"
        return

    if etype == "user":
        msg = event.get("message") or {}
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                rendered = _render_tool_result_content(block.get("content"))
                yield f"\n[tool_result]\n{rendered}\n"
        return

    if etype == "result":
        sub = event.get("subtype", "?")
        dur = event.get("duration_ms", 0)
        turns = event.get("num_turns", 0)
        cost = event.get("total_cost_usd")
        cost_s = f" cost=${cost:.4f}" if isinstance(cost, (int, float)) else ""
        yield f"\n[result] {sub} duration={dur}ms turns={turns}{cost_s}\n"
        return


def decode_stream(lines: Iterable[str], out: TextIO) -> None:
    """Decode a stream-json line iterator into `out`, flushing per line.

    Lines that aren't valid JSON are passed through verbatim — this keeps
    leading shell output from the wrapper (`[schedule-agent] start ...`)
    visible if it ever ends up on stdin, and makes diagnostics easier
    when claude prints a non-JSON error before the stream begins.
    """
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            out.write(raw if raw.endswith("\n") else raw + "\n")
            out.flush()
            continue
        for chunk in render_event(event):
            out.write(chunk)
        out.flush()


def main(argv: list[str] | None = None) -> int:
    decode_stream(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

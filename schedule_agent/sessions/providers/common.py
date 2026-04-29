from __future__ import annotations

from pathlib import Path

from ..jsonl import read_first_json_object, read_jsonl_sample


def extract_session_title(path: Path, agent: str) -> str | None:
    sample = read_jsonl_sample(path)
    agent = agent.lower().strip()

    if agent == "claude":
        for record in sample.head_records:
            if record.get("type") == "ai-title":
                title = record.get("aiTitle")
                if isinstance(title, str) and title.strip():
                    return title.strip()
        for record in list(sample.head_records) + list(sample.tail_records):
            if record.get("type") != "user" or record.get("isMeta"):
                continue
            message = record.get("message", {})
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip().splitlines()[0]
        return None

    if agent == "codex":
        for record in list(sample.head_records) + list(sample.tail_records):
            if record.get("type") == "event_msg":
                payload = record.get("payload", {})
                if payload.get("type") == "user_message":
                    message = payload.get("message")
                    if isinstance(message, str) and message.strip():
                        return message.strip().splitlines()[0]
            if record.get("type") == "response_item":
                payload = record.get("payload", {})
                if payload.get("type") != "message" or payload.get("role") != "user":
                    continue
                for item in payload.get("content", []):
                    if item.get("type") != "input_text":
                        continue
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        return text.strip().splitlines()[0]
        return None

    return None


def codex_session_is_subagent(path: Path) -> bool:
    record = read_first_json_object(path)
    if record is None:
        return False
    payload = record.get("payload") or {}
    source = payload.get("source")
    return isinstance(source, dict) and "subagent" in source

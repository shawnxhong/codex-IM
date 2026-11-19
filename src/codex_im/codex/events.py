"""Version-tolerant views over SDK notifications and thread history.

Payloads are converted to plain dicts (camelCase wire names) instead of relying on generated class
names, so an SDK upgrade that renames or adds generated models does not break the bridge (D2, D12).
"""

from __future__ import annotations

from typing import Any


def payload_dict(notification: Any) -> dict[str, Any]:
    payload = getattr(notification, "payload", None)
    if payload is None:
        return {}
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        try:
            data = dump(mode="json", by_alias=True)
        except TypeError:
            data = dump()
        return data if isinstance(data, dict) else {}
    params = getattr(payload, "params", None)
    return params if isinstance(params, dict) else {}


def turn_id_of(method: str, data: dict[str, Any]) -> str | None:
    if method.startswith("turn/"):
        turn = data.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
    value = data.get("turnId")
    return str(value) if value else None


def is_async_message(item: dict[str, Any]) -> bool:
    return item.get("type") == "agentMessage" and item.get("delivery") == "async"


def format_questions(item: dict[str, Any]) -> str:
    """Text for an async question message. Codex already renders questions into ``text``; fall back to the
    structured ``questions`` field when it is empty."""
    text = str(item.get("text") or "").strip()
    if text:
        return text
    lines: list[str] = []
    for question in item.get("questions") or []:
        if not isinstance(question, dict):
            continue
        title = str(question.get("title") or question.get("question") or "").strip()
        if title:
            lines.append(title)
        for option in question.get("options") or []:
            label = option.get("label") if isinstance(option, dict) else option
            if label:
                lines.append(f"- {label}")
        lines.append("")
    return "\n".join(lines).strip()


def pick_final_text(items: list[dict[str, Any]]) -> str | None:
    """Same rule as the SDK's ``final_response`` (last final_answer, else last unknown-phase message),
    but async question messages are excluded because they are delivered on their own (D8, D13)."""
    fallback: str | None = None
    for item in reversed(items):
        if item.get("type") != "agentMessage" or is_async_message(item):
            continue
        phase = item.get("phase")
        if phase == "final_answer":
            return str(item.get("text") or "")
        if phase is None and fallback is None:
            fallback = str(item.get("text") or "")
    return fallback


def user_texts_of_turn(turn: dict[str, Any]) -> list[str]:
    """Text of each userMessage item of a history turn, in order: the turn's input first, then steers.

    VERIFY(ubuntu): V1/V3 — steered input appears in the history turn as its own userMessage item.
    """
    texts: list[str] = []
    for item in turn.get("items") or []:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        parts = [
            str(content.get("text") or "")
            for content in item.get("content") or []
            if isinstance(content, dict) and content.get("type") == "text"
        ]
        texts.append("\n".join(parts))
    return texts


def user_text_of_turn(turn: dict[str, Any]) -> str:
    """The input that started a history turn (its first userMessage item)."""
    texts = user_texts_of_turn(turn)
    return texts[0] if texts else ""


def started_at_of_turn(turn: dict[str, Any]) -> float | None:
    value = turn.get("startedAt")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def agent_items_of_turn(turn: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in (turn.get("items") or []) if isinstance(i, dict) and i.get("type") == "agentMessage"]

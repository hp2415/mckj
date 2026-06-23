"""将 MiBuddy 转写 Sentences 整形为可读对话文本。"""
from __future__ import annotations

import json
from typing import Any


def _ms_to_mmss(ms: int) -> str:
    s = max(0, int(ms or 0)) // 1000
    m, r = divmod(s, 60)
    return f"{m:02d}:{r:02d}"


def _speaker_label(channel_id: int | None, *, is_send: int | None = None) -> str:
    """双轨录音：按 ChannelId 区分说话人，不标注销售/客户角色。"""
    _ = is_send
    ch = int(channel_id or 0)
    return f"说话人{ch + 1}"


def format_sentences_to_dialogue(
    sentences: list[dict[str, Any]],
    *,
    is_send: int | None = None,
) -> tuple[str, int, int]:
    """返回 (dialogue_text, sentence_count, char_count)。"""
    if not sentences:
        return "", 0, 0

    rows = [s for s in sentences if isinstance(s, dict)]
    rows.sort(key=lambda x: int(x.get("BeginTime") or 0))

    lines: list[str] = []
    for s in rows:
        text = str(s.get("Text") or "").strip()
        if not text:
            continue
        label = _speaker_label(s.get("ChannelId"), is_send=is_send)
        t = _ms_to_mmss(int(s.get("BeginTime") or 0))
        lines.append(f"[{t}] {label}：{text}")

    dialogue = "\n".join(lines)
    return dialogue, len(lines), len(dialogue)


def format_transcript_from_result(
    result: dict[str, Any] | None,
    *,
    is_send: int | None = None,
) -> tuple[str, str, int, int]:
    """
    从 get_file_trans_result 的 result 字段整形。
    返回 (dialogue_text, json_str, sentence_count, char_count)。
    """
    if not result or not isinstance(result, dict):
        return "", "{}", 0, 0
    sentences = result.get("Sentences") or []
    if not isinstance(sentences, list):
        sentences = []
    dialogue, sc, cc = format_sentences_to_dialogue(sentences, is_send=is_send)
    json_str = json.dumps({"Sentences": sentences}, ensure_ascii=False, separators=(",", ":"))
    return dialogue, json_str, sc, cc

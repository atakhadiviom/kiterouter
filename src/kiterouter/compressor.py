"""RTK-style prompt and tool-result token compressor."""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, List, Tuple

RE_CONSECUTIVE_NEWLINES = re.compile(r"\n{3,}")
RE_TRAILING_WHITESPACE = re.compile(r"[ \t]+$", re.MULTILINE)
RE_GIT_INDEX_HASH = re.compile(r"^index [0-9a-f]{7,40}\.\.[0-9a-f]{7,40}(?: \d+)?$", re.MULTILINE)
RE_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


@dataclass
class CompressionStats:
    original_chars: int = 0
    compressed_chars: int = 0
    messages_modified: int = 0

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.compressed_chars)

    @property
    def saved_ratio(self) -> float:
        if self.original_chars == 0:
            return 0.0
        return max(0.0, self.saved_chars / self.original_chars)


def compress_text(text: str, max_chars: int | None = None) -> str:
    """Clean and optionally truncate verbose text (e.g. tool output, git diff, logs)."""
    if not text or not isinstance(text, str):
        return text

    # Strip ANSI colors/escapes
    cleaned = RE_ANSI_ESCAPE.sub("", text)
    # Strip trailing whitespace on each line
    cleaned = RE_TRAILING_WHITESPACE.sub("", cleaned)
    # Collapse 3+ newlines to 2
    cleaned = RE_CONSECUTIVE_NEWLINES.sub("\n\n", cleaned)
    # Strip unnecessary git diff index hashes
    cleaned = RE_GIT_INDEX_HASH.sub("", cleaned)

    if max_chars and len(cleaned) > max_chars:
        half = max_chars // 2
        cleaned = (
            cleaned[:half]
            + "\n\n[... output truncated by KiteRouter RTK ...]\n\n"
            + cleaned[-half:]
        )
    return cleaned


def compress_messages(
    messages: List[dict[str, Any]],
    max_tool_chars: int = 12000,
    compress_tools_only: bool = False,
) -> Tuple[List[dict[str, Any]], CompressionStats]:
    """
    Compress conversation history.
    Handles both OpenAI tool format and Anthropic tool_result blocks.
    """
    stats = CompressionStats()
    if not messages:
        return [], stats

    out_messages: List[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            out_messages.append(msg)
            continue

        role = msg.get("role")
        content = msg.get("content")
        new_msg = deepcopy(msg)
        is_tool = role == "tool"

        if isinstance(content, str):
            stats.original_chars += len(content)
            # If compress_tools_only is True and this is not a tool message, don't truncate
            limit = max_tool_chars if is_tool else (None if compress_tools_only else None)
            compressed = compress_text(content, max_chars=limit)
            stats.compressed_chars += len(compressed)
            if compressed != content:
                stats.messages_modified += 1
            new_msg["content"] = compressed

        elif isinstance(content, list):
            new_blocks = []
            modified = False
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    raw_tool = block.get("content", "")
                    if isinstance(raw_tool, str):
                        stats.original_chars += len(raw_tool)
                        c_tool = compress_text(raw_tool, max_chars=max_tool_chars)
                        stats.compressed_chars += len(c_tool)
                        if c_tool != raw_tool:
                            modified = True
                        new_blocks.append({**block, "content": c_tool})
                    else:
                        new_blocks.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    raw_text = block.get("text", "")
                    stats.original_chars += len(raw_text)
                    c_text = compress_text(raw_text)
                    stats.compressed_chars += len(c_text)
                    if c_text != raw_text:
                        modified = True
                    new_blocks.append({**block, "text": c_text})
                else:
                    new_blocks.append(block)
            if modified:
                stats.messages_modified += 1
            new_msg["content"] = new_blocks
        else:
            out_messages.append(new_msg)
            continue

        out_messages.append(new_msg)

    return out_messages, stats

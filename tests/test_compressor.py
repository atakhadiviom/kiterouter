import pytest
from kiterouter.compressor import compress_text, compress_messages


def test_compress_text_whitespace_and_newlines():
    text = "Hello world   \n\n\n\n\nThis is a test.  "
    compressed = compress_text(text)
    assert compressed == "Hello world\n\nThis is a test."


def test_compress_text_git_hash():
    text = "diff --git a/foo.py b/foo.py\nindex 1234567..89abcdef 100644\n--- a/foo.py\n+++ b/foo.py"
    compressed = compress_text(text)
    assert "index 1234567..89abcdef" not in compressed
    assert "diff --git" in compressed


def test_compress_text_truncation():
    text = "A" * 2000
    compressed = compress_text(text, max_chars=500)
    assert len(compressed) < 2000
    assert "[... output truncated by KiteRouter RTK ...]" in compressed


def test_compress_messages_openai_tool():
    messages = [
        {"role": "user", "content": "Run tests"},
        {"role": "tool", "content": "line\n" * 1000},
    ]
    compressed, stats = compress_messages(messages, max_tool_chars=200)
    assert stats.saved_chars > 0
    assert stats.saved_ratio > 0.5
    assert len(compressed[1]["content"]) < 1000


def test_compress_messages_anthropic_blocks():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "content": "huge output " * 500}
            ],
        }
    ]
    compressed, stats = compress_messages(messages, max_tool_chars=200)
    assert stats.messages_modified == 1
    assert stats.saved_ratio > 0
    res = compressed[0]["content"][0]["content"]
    assert "[... output truncated by KiteRouter RTK ...]" in res

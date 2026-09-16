"""Tests for the chat_summary: config block."""

import pytest

from src.config_loader import ChatSummaryConfig, load_config

BASE = "settings:\n  target_user_id: 123456789\n"


def _load(tmp_path, block: str):
    path = tmp_path / "config.yaml"
    path.write_text(BASE + block)
    return load_config(str(path))


@pytest.mark.unit
def test_defaults_when_block_missing(tmp_path, mock_env_vars):
    assert _load(tmp_path, "").chat_summary == ChatSummaryConfig()


@pytest.mark.unit
def test_full_block_parsed(tmp_path, mock_env_vars):
    cfg = _load(
        tmp_path,
        """
chat_summary:
  enabled: true
  ai_model: " gpt-5-mini "
  message_counts: [500, 50, 50]
  max_messages: 2000
  max_message_chars: 700
  single_call_chars: 100000
  summary_max_chars: 5000
  max_output_tokens: 9000
  reasoning_effort: null
  api_timeout: 90
  page_size: 5
""",
    ).chat_summary
    assert cfg.ai_model == "gpt-5-mini"
    assert cfg.message_counts == [50, 500]  # sorted, deduplicated
    assert cfg.max_messages == 2000
    assert cfg.max_message_chars == 700
    assert cfg.single_call_chars == 100000
    assert cfg.summary_max_chars == 5000
    assert cfg.max_output_tokens == 9000
    assert cfg.reasoning_effort == ""
    assert cfg.api_timeout == 90
    assert cfg.page_size == 5


@pytest.mark.unit
@pytest.mark.parametrize(
    "block, message",
    [
        ("chat_summary: []", "must be a mapping"),
        ("chat_summary:\n  enabled: 1", "enabled must be a bool"),
        ("chat_summary:\n  ai_model: 5", "ai_model must be a string"),
        ("chat_summary:\n  max_messages: 0", "max_messages must be a positive int"),
        ("chat_summary:\n  max_messages: 9000", "max_messages must be <= 5000"),
        ("chat_summary:\n  max_messages: true", "max_messages must be a positive int"),
        ("chat_summary:\n  message_counts: []", "message_counts must be a non-empty list"),
        ("chat_summary:\n  message_counts: [100, -1]", "message_counts must be a non-empty"),
        ("chat_summary:\n  message_counts: [2000]", "exceed chat_summary.max_messages"),
        ("chat_summary:\n  reasoning_effort: turbo", "reasoning_effort must be one of"),
        ("chat_summary:\n  page_size: 50", "page_size must be <= 20"),
        ("chat_summary:\n  api_timeout: slow", "api_timeout must be a positive int"),
    ],
)
def test_invalid_values_rejected(tmp_path, mock_env_vars, block, message):
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, block)

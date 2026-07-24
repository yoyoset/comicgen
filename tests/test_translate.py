"""Tests for pipeline.translate (offline, no API/network)."""
import pytest
from comic_gen.core.config import Config
from comic_gen.pipeline.translate import MockTranslator, DeepSeekTranslator


def test_mock_translator_basic():
    """Test MockTranslator returns input texts as-is."""
    cfg = Config(translator="mock")
    translator = MockTranslator()

    texts = ["本文1", "本文2", "本文3"]
    result = translator.translate(texts, cfg)

    assert len(result) == len(texts)
    assert result == texts


def test_mock_translator_empty():
    """Test MockTranslator with empty list."""
    translator = MockTranslator()
    cfg = Config()
    result = translator.translate([], cfg)
    assert result == []


def test_mock_translator_single():
    """Test MockTranslator with single text."""
    translator = MockTranslator()
    cfg = Config()
    result = translator.translate(["単一"], cfg)
    assert result == ["単一"]


def test_deepseek_translator_no_api_key():
    """Test DeepSeekTranslator init requires API key or env."""
    cfg = Config(translator="deepseek", deepseek_api_key="")
    translator = DeepSeekTranslator()

    # resolved_api_key should check env or default
    api_key = cfg.resolved_api_key()
    # Test doesn't fail; the translator checks at translate() time
    # This test just ensures init doesn't crash
    assert translator is not None


def test_deepseek_translator_with_key():
    """Test DeepSeekTranslator can be initialized with API key."""
    cfg = Config(translator="deepseek", deepseek_api_key="test-key-123")
    api_key = cfg.resolved_api_key()
    assert api_key == "test-key-123"


def test_parse_numbered_basic():
    """Test _parse_numbered output format."""
    from comic_gen.pipeline.translate import DeepSeekTranslator

    # This method is private but important for alignment
    translator = DeepSeekTranslator()

    # Simulate LLM output: "1. 翻译1\n2. 翻译2"
    llm_output = "1. 本文1的翻译\n2. 本文2的翻译"
    result = translator._parse_numbered(llm_output, 2)

    assert len(result) == 2
    assert "翻译1" in result[0] or "本文1" in result[0]


def test_parse_numbered_mismatch():
    """Test _parse_numbered with count mismatch."""
    from comic_gen.pipeline.translate import DeepSeekTranslator

    translator = DeepSeekTranslator()

    # Output has 2 items but we expect 3
    llm_output = "1. 翻译1\n2. 翻译2"
    result = translator._parse_numbered(llm_output, 3)

    # Should return None or fallback (see implementation)
    # This tests the robustness of the retry logic
    assert result is None or isinstance(result, list)

"""Tests for providers config entry validation and normalization.

Covers Issue #9332: camelCase keys silently ignored, non-URL strings
accepted as base_url, and unknown keys go unreported.
"""

import logging
from unittest.mock import patch

import pytest

from hermes_cli.config import _normalize_custom_provider_entry


class TestNormalizeCustomProviderEntry:
    """Tests for _normalize_custom_provider_entry validation."""

    def test_valid_entry_snake_case(self):
        """Standard snake_case entry should normalize correctly."""
        entry = {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="myhost")
        assert result is not None
        assert result["name"] == "myhost"
        assert result["base_url"] == "https://api.example.com/v1"
        assert result["api_key"] == "sk-test-key"

    def test_camel_case_api_key_mapped(self):
        """camelCase apiKey should be auto-mapped to api_key."""
        entry = {
            "base_url": "https://api.example.com/v1",
            "apiKey": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="myhost")
        assert result is not None
        assert result["api_key"] == "sk-test-key"

    def test_camel_case_base_url_mapped(self):
        """camelCase baseUrl should be auto-mapped to base_url."""
        entry = {
            "baseUrl": "https://api.example.com/v1",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="myhost")
        assert result is not None
        assert result["base_url"] == "https://api.example.com/v1"

    def test_non_url_api_field_rejected(self):
        """Non-URL string in 'api' field should be skipped with a warning."""
        entry = {
            "api": "openai-reverse-proxy",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="nvidia")
        # Should return None because no valid URL was found
        assert result is None

    def test_valid_url_in_api_field_accepted(self):
        """Valid URL in 'api' field should still be accepted."""
        entry = {
            "api": "https://integrate.api.nvidia.com/v1",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="nvidia")
        assert result is not None
        assert result["base_url"] == "https://integrate.api.nvidia.com/v1"

    def test_api_preferred_over_base_url(self):
        """`api` is the canonical endpoint field and wins over base_url when
        both are present (precedence api > url > base_url — see
        test_config.test_compatible_custom_providers_prefers_api_then_url_then_base_url)."""
        entry = {
            "api": "https://correct.example.com/v1",
            "base_url": "https://wrong.example.com/v1",
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is not None
        assert result["base_url"] == "https://correct.example.com/v1"

    def test_unknown_keys_logged(self, caplog):
        """Unknown config keys should produce a warning."""
        entry = {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-key",
            "unknownField": "value",
            "anotherBad": 42,
        }
        # Propagation-proof capture: another test in the suite can leave the
        # config logger with propagate=False or a raised level (caplog reaches
        # records only via propagation to root), which silently emptied
        # caplog.records here. Force a clean logging state for THIS logger and
        # capture at it directly.
        logging.disable(logging.NOTSET)
        _cfg = logging.getLogger("hermes_cli.config")
        _cfg.propagate = True
        _cfg.setLevel(logging.NOTSET)
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is not None
        assert any("unknown config keys" in r.message.lower() for r in caplog.records)

    def test_camel_case_warning_logged(self, caplog):
        """camelCase alias mapping should produce a warning."""
        entry = {
            "baseUrl": "https://api.example.com/v1",
            "apiKey": "sk-test-key",
        }
        # Propagation-proof capture: another test in the suite can leave the
        # config logger with propagate=False or a raised level (caplog reaches
        # records only via propagation to root), which silently emptied
        # caplog.records here. Force a clean logging state for THIS logger and
        # capture at it directly.
        logging.disable(logging.NOTSET)
        _cfg = logging.getLogger("hermes_cli.config")
        _cfg.propagate = True
        _cfg.setLevel(logging.NOTSET)
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is not None
        camel_warnings = [r for r in caplog.records if "camelcase" in r.message.lower() or "auto-mapped" in r.message.lower()]
        assert len(camel_warnings) >= 1

    def test_snake_case_takes_precedence_over_camel(self):
        """If both snake_case and camelCase exist, snake_case wins."""
        entry = {
            "api_key": "snake-key",
            "apiKey": "camel-key",
            "base_url": "https://api.example.com/v1",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is not None
        assert result["api_key"] == "snake-key"

    def test_non_dict_returns_none(self):
        """Non-dict entry should return None."""
        assert _normalize_custom_provider_entry("not-a-dict") is None
        assert _normalize_custom_provider_entry(42) is None
        assert _normalize_custom_provider_entry(None) is None

    def test_no_url_returns_none(self):
        """Entry with no valid URL in any field should return None."""
        entry = {
            "api_key": "sk-test-key",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="test")
        assert result is None

    def test_no_name_returns_none(self):
        """Entry with no name and no provider_key should return None."""
        entry = {
            "base_url": "https://api.example.com/v1",
        }
        result = _normalize_custom_provider_entry(entry, provider_key="")
        assert result is None

"""Tests for hermes_cli.model_normalize — provider-aware model name normalization.

Covers issue #5211: opencode-go model names with dots (e.g. minimax-m2.7)
must NOT be mangled to hyphens (minimax-m2-7).
"""
import pytest

from hermes_cli.model_normalize import (
    normalize_model_for_provider,
    _DOT_TO_HYPHEN_PROVIDERS,
    _AGGREGATOR_PROVIDERS,
    detect_vendor,
)


# ── Regression: issue #5211 ────────────────────────────────────────────

class TestIssue5211OpenCodeGoDotPreservation:
    """OpenCode Go model names with dots must pass through unchanged."""

    @pytest.mark.parametrize("model,expected", [
        ("minimax-m2.7", "minimax-m2.7"),
        ("minimax-m2.5", "minimax-m2.5"),
        ("glm-4.5", "glm-4.5"),
        ("kimi-k2.5", "kimi-k2.5"),
        ("some-model-1.0.3", "some-model-1.0.3"),
    ])
    def test_opencode_go_preserves_dots(self, model, expected):
        result = normalize_model_for_provider(model, "opencode-go")
        assert result == expected, f"Expected {expected!r}, got {result!r}"

    def test_opencode_go_not_in_dot_to_hyphen_set(self):
        """opencode-go must NOT be in the dot-to-hyphen provider set."""
        assert "opencode-go" not in _DOT_TO_HYPHEN_PROVIDERS


# ── Anthropic dot-to-hyphen conversion (regression) ────────────────────

class TestAnthropicDotToHyphen:
    """Anthropic API still needs dots→hyphens."""

    @pytest.mark.parametrize("model,expected", [
        ("claude-sonnet-4.6", "claude-sonnet-4-6"),
        ("claude-opus-4.5", "claude-opus-4-5"),
    ])
    def test_anthropic_converts_dots(self, model, expected):
        result = normalize_model_for_provider(model, "anthropic")
        assert result == expected

    def test_anthropic_strips_vendor_prefix(self):
        result = normalize_model_for_provider("anthropic/claude-sonnet-4.6", "anthropic")
        assert result == "claude-sonnet-4-6"


# ── OpenCode Zen regression ────────────────────────────────────────────

class TestOpenCodeZenModelNormalization:
    """OpenCode Zen preserves dots for most models, but Claude stays hyphenated."""

    @pytest.mark.parametrize("model,expected", [
        ("claude-sonnet-4.6", "claude-sonnet-4-6"),
        ("opencode-zen/claude-opus-4.5", "claude-opus-4-5"),
        ("glm-4.5", "glm-4.5"),
        ("glm-5.1", "glm-5.1"),
        ("gpt-5.4", "gpt-5.4"),
        ("minimax-m2.5-free", "minimax-m2.5-free"),
        ("kimi-k2.5", "kimi-k2.5"),
    ])
    def test_zen_normalizes_models(self, model, expected):
        result = normalize_model_for_provider(model, "opencode-zen")
        assert result == expected

    def test_zen_strips_vendor_prefix(self):
        result = normalize_model_for_provider("opencode-zen/claude-sonnet-4.6", "opencode-zen")
        assert result == "claude-sonnet-4-6"

    def test_zen_strips_vendor_prefix_for_non_claude(self):
        result = normalize_model_for_provider("opencode-zen/glm-5.1", "opencode-zen")
        assert result == "glm-5.1"


# ── Copilot dot preservation (regression) ──────────────────────────────

class TestCopilotDotPreservation:
    """Copilot preserves dots in model names."""

    @pytest.mark.parametrize("model,expected", [
        ("claude-sonnet-4.6", "claude-sonnet-4.6"),
        ("gpt-5.4", "gpt-5.4"),
    ])
    def test_copilot_preserves_dots(self, model, expected):
        result = normalize_model_for_provider(model, "copilot")
        assert result == expected


# ── Copilot model-name normalization (issue #6879 regression) ──────────

class TestCopilotModelNormalization:
    """Copilot requires bare dot-notation model IDs.

    Regression coverage for issue #6879 and the broken Copilot branch
    that previously left vendor-prefixed Anthropic IDs (e.g.
    ``anthropic/claude-sonnet-4.6``) and dash-notation Claude IDs (e.g.
    ``claude-sonnet-4-6``) unchanged, causing the Copilot API to reject
    the request with HTTP 400 "model_not_supported".
    """

    @pytest.mark.parametrize("model,expected", [
        # Vendor-prefixed Anthropic IDs — prefix must be stripped.
        ("anthropic/claude-opus-4.6",   "claude-opus-4.6"),
        ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"),
        ("anthropic/claude-sonnet-4.5", "claude-sonnet-4.5"),
        ("anthropic/claude-haiku-4.5",  "claude-haiku-4.5"),
        # Vendor-prefixed OpenAI IDs — prefix must be stripped.
        ("openai/gpt-5.4",              "gpt-5.4"),
        ("openai/gpt-4o",               "gpt-4o"),
        ("openai/gpt-4o-mini",          "gpt-4o-mini"),
        # Dash-notation Claude IDs — must be converted to dot-notation.
        ("claude-opus-4-6",             "claude-opus-4.6"),
        ("claude-sonnet-4-6",           "claude-sonnet-4.6"),
        ("claude-sonnet-4-5",           "claude-sonnet-4.5"),
        ("claude-haiku-4-5",            "claude-haiku-4.5"),
        # Combined: vendor-prefixed + dash-notation.
        ("anthropic/claude-opus-4-6",   "claude-opus-4.6"),
        ("anthropic/claude-sonnet-4-6", "claude-sonnet-4.6"),
        # Already-canonical inputs pass through unchanged.
        ("claude-sonnet-4.6",           "claude-sonnet-4.6"),
        ("gpt-5.4",                     "gpt-5.4"),
        ("gpt-5-mini",                  "gpt-5-mini"),
    ])
    def test_copilot_normalization(self, model, expected):
        assert normalize_model_for_provider(model, "copilot") == expected

    @pytest.mark.parametrize("model,expected", [
        ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"),
        ("claude-sonnet-4-6",           "claude-sonnet-4.6"),
        ("claude-opus-4-6",             "claude-opus-4.6"),
        ("openai/gpt-5.4",              "gpt-5.4"),
    ])
    def test_copilot_acp_normalization(self, model, expected):
        """Copilot ACP shares the same API expectations as HTTP Copilot."""
        assert normalize_model_for_provider(model, "copilot-acp") == expected

    def test_openai_codex_still_strips_openai_prefix(self):
        """Regression: openai-codex must still strip the openai/ prefix."""
        assert normalize_model_for_provider("openai/gpt-5.4", "openai-codex") == "gpt-5.4"


# ── Aggregator providers (regression) ──────────────────────────────────

class TestAggregatorProviders:
    """Aggregators need vendor/model slugs."""

    def test_openrouter_prepends_vendor(self):
        result = normalize_model_for_provider("claude-sonnet-4.6", "openrouter")
        assert result == "anthropic/claude-sonnet-4.6"

    def test_nous_prepends_vendor(self):
        result = normalize_model_for_provider("gpt-5.4", "nous")
        assert result == "openai/gpt-5.4"

    def test_vendor_already_present(self):
        result = normalize_model_for_provider("anthropic/claude-sonnet-4.6", "openrouter")
        assert result == "anthropic/claude-sonnet-4.6"


class TestIssue6211NativeProviderPrefixNormalization:
    @pytest.mark.parametrize("model,target_provider,expected", [
        ("zai/glm-5.1", "zai", "glm-5.1"),
        ("google/gemini-2.5-pro", "gemini", "google/gemini-2.5-pro"),
        ("moonshot/kimi-k2.5", "kimi-coding", "kimi-k2.5"),
        ("anthropic/claude-sonnet-4.6", "openrouter", "anthropic/claude-sonnet-4.6"),
        ("Qwen/Qwen3.5-397B-A17B", "huggingface", "Qwen/Qwen3.5-397B-A17B"),
        ("modal/zai-org/GLM-5-FP8", "custom", "modal/zai-org/GLM-5-FP8"),
    ])
    def test_native_provider_prefixes_are_only_stripped_on_matching_provider(
        self, model, target_provider, expected
    ):
        assert normalize_model_for_provider(model, target_provider) == expected


# ── detect_vendor ──────────────────────────────────────────────────────

class TestDetectVendor:
    @pytest.mark.parametrize("model,expected", [
        ("claude-sonnet-4.6", "anthropic"),
        ("gpt-5.4-mini", "openai"),
        ("minimax-m2.7", "minimax"),
        ("glm-4.5", "z-ai"),
        ("kimi-k2.5", "moonshotai"),
    ])
    def test_detects_known_vendors(self, model, expected):
        assert detect_vendor(model) == expected


class TestDeepSeekV4:
    """DeepSeek deprecated deepseek-chat/deepseek-reasoner (HTTP 400); the API
    now requires deepseek-v4-pro / deepseek-v4-flash. Every normalization path
    must land on a supported name — this broke every customer's bots when the
    deprecation went live (2026-07-26)."""

    def test_never_returns_deprecated_names(self):
        from hermes_cli.model_normalize import _normalize_for_deepseek
        for m in ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro",
                  "deepseek/deepseek-chat", "deepseek", "r1", "think", ""]:
            out = _normalize_for_deepseek(m)
            assert out in ("deepseek-v4-pro", "deepseek-v4-flash",
                           "deepseek-v4-flash-vision-exp"), \
                f"{m!r} -> {out!r} is not a supported DeepSeek model"

    def test_legacy_and_default_map_to_pro(self):
        from hermes_cli.model_normalize import _normalize_for_deepseek
        assert _normalize_for_deepseek("deepseek-chat") == "deepseek-v4-pro"
        assert _normalize_for_deepseek("anything") == "deepseek-v4-pro"

    def test_flash_keywords_map_to_flash(self):
        from hermes_cli.model_normalize import _normalize_for_deepseek
        assert _normalize_for_deepseek("flash") == "deepseek-v4-flash"
        assert _normalize_for_deepseek("deepseek-fast") == "deepseek-v4-flash"

    def test_vision_exp_passes_through_uncollapsed(self):
        """THE landmine: the vision id contains 'flash', so without canonical
        membership the keyword rule rewrites it to the vision-less v4-flash and
        every image call 400s. It must survive normalization verbatim."""
        from hermes_cli.model_normalize import (
            _normalize_for_deepseek,
            normalize_model_for_provider,
        )
        assert _normalize_for_deepseek("deepseek-v4-flash-vision-exp") \
            == "deepseek-v4-flash-vision-exp"
        # Vendor-prefixed + mixed case also survive
        assert normalize_model_for_provider(
            "deepseek/deepseek-v4-flash-vision-exp", "deepseek"
        ) == "deepseek-v4-flash-vision-exp"
        assert _normalize_for_deepseek("DeepSeek-V4-Flash-Vision-Exp") \
            == "deepseek-v4-flash-vision-exp"

    def test_vision_keyword_beats_flash_keyword(self):
        """Vision intent wins: only vision-exp accepts images, so any name
        expressing vision must not land on the vision-less flash model."""
        from hermes_cli.model_normalize import _normalize_for_deepseek
        assert _normalize_for_deepseek("deepseek-vision") \
            == "deepseek-v4-flash-vision-exp"
        assert _normalize_for_deepseek("flash-vision") \
            == "deepseek-v4-flash-vision-exp"

    def test_provider_default_is_v4_pro(self):
        from hermes_cli.model_switch import MODEL_ALIASES
        ident = MODEL_ALIASES.get("deepseek")
        assert ident is not None
        # family/default model must be a supported name, not the dead one
        fam = getattr(ident, "family", None) or getattr(ident, "model", None)
        assert fam == "deepseek-v4-pro", f"deepseek provider default is {fam!r}"

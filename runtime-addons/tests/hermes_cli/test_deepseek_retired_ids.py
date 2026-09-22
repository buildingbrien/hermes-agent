"""Lucaryin folds (runtime-patches/0006, 0022, 0002): DeepSeek retired-ID safety net.

DeepSeek retired ``deepseek-v4-flash`` and ``deepseek-v4-flash-vision-exp`` on 2026-09-10
(api-docs.deepseek.com/news/news260910). Both only "temporarily" route to V4.1-Flash, whose id is
``deepseek-flash`` and which is natively multimodal. ``deepseek-v4-pro`` is NOT retired and stays
the fleet's main model. Precedent: on 2026-07-26 ``deepseek-chat`` started returning HTTP 400 and
every bot on every box answered empty, so the runtime folds every retired id at call time. That
keeps un-healed customer configs working the day DeepSeek ends the temporary routing.

- 0006 (hermes_cli/model_normalize.py): retired aliases, ``*vision*`` and the
  ``deepseek-v4-flash*`` family fold onto ``deepseek-flash``. v4-pro and unknown ids pass through.
- 0022 (agent/auxiliary_client.py): the DeepSeek vision default is ``deepseek-flash``. That
  literal never passes through the normalizer, so it must itself be a live id.
- 0002 (agent/auxiliary_client.py): text aux tasks on a v4-pro main downgrade to ``deepseek-flash``.

Bare tier: pure functions, no network, no credentials.
"""

import pytest

from hermes_cli.model_normalize import _normalize_for_deepseek, normalize_model_for_provider

LIVE_FLASH = "deepseek-flash"
RETIRED_IDS = [
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    # the same ids as a customer might have typed them into config.yaml
    "DeepSeek-V4-Flash",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-flash-vision-exp",
    # the dated snapshot and suffixed variants of the retired family
    "deepseek-v4-flash-0731",
    "deepseek-v4-flash:free",
    # any DeepSeek vision id (vision beats everything)
    "deepseek-vision",
    "deepseek-v4-pro-vision",
]


class TestRetiredIdsFoldOntoDeepseekFlash:
    @pytest.mark.parametrize("model", RETIRED_IDS)
    def test_retired_id_normalizes_to_deepseek_flash(self, model):
        assert normalize_model_for_provider(model, "deepseek") == LIVE_FLASH

    @pytest.mark.parametrize("model", RETIRED_IDS)
    def test_retired_id_folds_in_the_bare_helper(self, model):
        assert _normalize_for_deepseek(model) == LIVE_FLASH

    def test_live_flash_is_a_fixed_point(self):
        assert normalize_model_for_provider(LIVE_FLASH, "deepseek") == LIVE_FLASH


class TestMainModelAndUnknownIdsUntouched:
    def test_v4_pro_stays_the_main_model(self):
        assert normalize_model_for_provider("deepseek-v4-pro", "deepseek") == "deepseek-v4-pro"
        assert normalize_model_for_provider("deepseek/deepseek-v4-pro", "deepseek") == "deepseek-v4-pro"

    @pytest.mark.parametrize("model", [
        # names that contain "flash" but are not the retired v4-flash family must not collapse
        # (upstream #107206: a shape allow-list once swallowed the vendor's own new id)
        "deepseek-v4.1-flash",
        "deepseek-v4-pro-flashback",
        "deepseek-r1",
        "deepseek-next-preview",
        "my-fine-tune",
    ])
    def test_non_retired_ids_pass_through(self, model):
        assert _normalize_for_deepseek(model) == model

    def test_fold_is_deepseek_provider_only(self):
        # an OpenRouter / aggregator slug is the aggregator's business, not ours
        assert normalize_model_for_provider("deepseek-v4-flash", "custom") == "deepseek-v4-flash"


class TestAuxDefaultsNameTheLiveId:
    def test_vision_default_is_deepseek_flash(self):
        from agent.auxiliary_client import _PROVIDER_VISION_MODELS, _resolve_provider_vision_default

        assert _PROVIDER_VISION_MODELS["deepseek"] == LIVE_FLASH
        assert _resolve_provider_vision_default("deepseek") == LIVE_FLASH

    def test_text_aux_on_pro_main_downgrades_to_deepseek_flash(self):
        from agent.auxiliary_client import _main_route_target

        runtime = {"provider": "deepseek", "model": "deepseek-v4-pro", "base_url": "", "api_key": ""}
        _provider, model, *_ = _main_route_target(runtime, "compression")
        assert model == LIVE_FLASH

    def test_vision_aux_on_pro_main_is_not_downgraded(self):
        from agent.auxiliary_client import _main_route_target

        runtime = {"provider": "deepseek", "model": "deepseek-v4-pro", "base_url": "", "api_key": ""}
        _provider, model, *_ = _main_route_target(runtime, "vision")
        assert model == "deepseek-v4-pro"


class TestExplicitAuxModelIsFoldedOnTheWire:
    """An EXPLICIT auxiliary.<task>.model bypasses resolve_provider_client()'s normalization:
    _get_cached_client() -> _compat_model() returns the caller's raw id. Found 2026-09-22 in
    adversarial verification — an un-healed ``auxiliary.vision.model:
    deepseek-v4-flash-vision-exp`` reached api.deepseek.com verbatim. 0006 folds it at that
    return, keyed on the resolved DeepSeek host (never on another vendor's endpoint)."""

    class _Client:
        def __init__(self, base_url):
            self.base_url = base_url

    @pytest.mark.parametrize("model", ["deepseek-v4-flash-vision-exp", "deepseek-v4-flash", "deepseek-chat"])
    def test_retired_explicit_model_folds_for_deepseek_host(self, model):
        from agent.auxiliary_client import _fold_retired_deepseek_model

        client = self._Client("https://api.deepseek.com/v1/")
        assert _fold_retired_deepseek_model(client, model) == LIVE_FLASH

    def test_v4_pro_explicit_model_untouched(self):
        from agent.auxiliary_client import _fold_retired_deepseek_model

        client = self._Client("https://api.deepseek.com/v1/")
        assert _fold_retired_deepseek_model(client, "deepseek-v4-pro") == "deepseek-v4-pro"

    @pytest.mark.parametrize("base_url,model", [
        ("https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash"),
        ("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash"),
        ("https://api.deepseek.com.evil.example/v1", "deepseek-v4-flash"),
    ])
    def test_other_hosts_untouched(self, base_url, model):
        from agent.auxiliary_client import _fold_retired_deepseek_model

        assert _fold_retired_deepseek_model(self._Client(base_url), model) == model

    def test_get_cached_client_returns_folded_model(self, monkeypatch):
        import agent.auxiliary_client as ac

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-a-key")
        ac._client_cache.clear()
        try:
            client, model = ac._get_cached_client("deepseek", "deepseek-v4-flash-vision-exp", is_vision=True)
            assert client is not None
            assert model == LIVE_FLASH
            # the cache-hit branch folds too
            _client, model_again = ac._get_cached_client("deepseek", "deepseek-v4-flash-vision-exp", is_vision=True)
            assert model_again == LIVE_FLASH
        finally:
            ac._client_cache.clear()

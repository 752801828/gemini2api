import json

from app.core.gemini_client import (
    _resolve_model,
    _build_model_header,
    MODEL_HEADER_KEY,
    _build_id_alias_map,
    PUBLIC_MODELS,
    GEMINI_MODELS,
    MODEL_ALIASES,
    GeminiWebClient,
)

FLASH_LITE_ID = "8c46e95b1a07cecc"


def test_flash_lite_in_public_models():
    assert "gemini-flash-lite" in PUBLIC_MODELS


def test_flash_lite_family_entries_exist_with_correct_id():
    # Base (free tier) uses free-tier id; plus/advanced (paid tiers) use paid id
    assert "gemini-3-flash-lite" in GEMINI_MODELS
    assert GEMINI_MODELS["gemini-3-flash-lite"]["id"] == "cf41b0e0dd7d53e5"
    assert GEMINI_MODELS["gemini-3-flash-lite"]["family"] == "flash-lite"

    for name in ("gemini-3-flash-lite-plus", "gemini-3-flash-lite-advanced"):
        assert name in GEMINI_MODELS, name
        assert GEMINI_MODELS[name]["id"] == FLASH_LITE_ID
        assert GEMINI_MODELS[name]["family"] == "flash-lite"


def test_id_alias_map_flash_lite_resolves_to_advanced():
    # -advanced is defined last among the shared-id entries, so it wins the id->name map
    assert _build_id_alias_map()[FLASH_LITE_ID] == "gemini-3-flash-lite-advanced"


def test_resolve_flash_lite_for_cap2_account():
    fm = {"flash-lite": "gemini-3-flash-lite-advanced"}
    assert _resolve_model("gemini-flash-lite", fm) == "gemini-3-flash-lite-advanced"


def test_resolve_flash_lite_default_without_account_map():
    assert _resolve_model("gemini-flash-lite") == "gemini-3-flash-lite"


def test_alias_2_0_flash_lite_maps_to_flash_lite():
    assert MODEL_ALIASES["gemini-2.0-flash-lite"] == "gemini-flash-lite"
    assert _resolve_model("gemini-2.0-flash-lite") == "gemini-3-flash-lite"


def test_current_web_model_names_map_to_stable_public_families():
    family_models = {
        "pro": "gemini-3-pro-advanced",
        "flash": "gemini-3-flash-advanced",
        "flash-lite": "gemini-3-flash-lite-advanced",
        "flash-thinking": "gemini-3-flash-thinking-advanced",
    }
    assert _resolve_model("gemini-3.1-pro", family_models) == "gemini-3-pro-advanced"
    assert _resolve_model("gemini-3.6-flash", family_models) == "gemini-3-flash-advanced"
    assert _resolve_model("gemini-3.5-flash-lite", family_models) == "gemini-3-flash-lite-advanced"
    assert _resolve_model("gemini-3.6-flash-thinking", family_models) == "gemini-3-flash-advanced"
    assert _resolve_model("gemini-3.1-pro-thinking", family_models) == "gemini-3-pro-advanced"


def test_stable_public_models_resolve_to_current_families():
    assert _resolve_model("gemini-pro") == "gemini-3-pro"
    assert _resolve_model("gemini-flash") == "gemini-3-flash"
    assert _resolve_model("gemini-flash-thinking") == "gemini-3-flash"


def test_pro_and_flash_support_native_extended_thinking_header():
    cases = (
        ("gemini-flash", "gemini-flash-thinking"),
        ("gemini-pro", "gemini-pro-thinking"),
    )
    for standard_model, extended_model in cases:
        standard = json.loads(_build_model_header(standard_model)[MODEL_HEADER_KEY])
        extended = json.loads(_build_model_header(extended_model)[MODEL_HEADER_KEY])
        assert standard[4] == extended[4]
        assert standard[15] == 1
        assert extended[15] == 2


def test_parse_models_from_status_maps_flash_lite():
    c = GeminiWebClient.__new__(GeminiWebClient)
    c._family_model = {}
    c._available_models = []
    # body[15] is the account's model list; each entry's [0] is the internal id
    body = list(range(15)) + [[[FLASH_LITE_ID, "Flash-Lite", "Fastest answers"]]]
    raw = json.dumps([["wrb.fr", None, json.dumps(body)]])
    c._parse_models_from_status(raw)
    assert c._family_model.get("flash-lite") == "gemini-3-flash-lite-advanced"


def test_v1_models_includes_flash_lite(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer sk-test-key"})
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert "gemini-flash-lite" in ids


def test_backfill_flash_lite_for_account_without_it():
    # Known-limitation path: an account exposing only a cap-1 flash model must
    # still get a flash-lite name via capacity backfill (same mechanism as the
    # existing synthetic flash-thinking).
    from app.core.gemini_client import GEMINI_MODELS
    c = GeminiWebClient.__new__(GeminiWebClient)
    c._family_model = {}
    c._available_models = []
    flash_base_id = GEMINI_MODELS["gemini-3-flash"]["id"]
    body = list(range(15)) + [[[flash_base_id, "Flash", "All-around help"]]]
    raw = json.dumps([["wrb.fr", None, json.dumps(body)]])
    c._parse_models_from_status(raw)
    assert c._family_model.get("flash") == "gemini-3-flash"
    assert c._family_model.get("flash-lite") == "gemini-3-flash-lite"

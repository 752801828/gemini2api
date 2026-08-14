from app.core.gemini_client import GEMINI_MODELS, _model_number

def test_flash_lite_base_id_is_free_tier():
    assert GEMINI_MODELS["gemini-3-flash-lite"]["id"] == "cf41b0e0dd7d53e5"
    # paid tiers keep the paid id
    assert GEMINI_MODELS["gemini-3-flash-lite-advanced"]["id"] == "8c46e95b1a07cecc"
    assert GEMINI_MODELS["gemini-3-flash-lite-plus"]["id"] == "8c46e95b1a07cecc"

def test_model_numbers():
    assert GEMINI_MODELS["gemini-3-pro"]["model_number"] == 3
    assert GEMINI_MODELS["gemini-3-flash"]["model_number"] == 1
    assert GEMINI_MODELS["gemini-3-flash-lite"]["model_number"] == 6
    assert GEMINI_MODELS["gemini-3-flash-thinking"]["model_number"] == 1

def test_model_number_helper_resolves_public_names():
    # public names resolve via _FAMILY_DEFAULT
    assert _model_number("gemini-pro") == 3
    assert _model_number("gemini-flash") == 1
    assert _model_number("gemini-flash-lite") == 6
    assert _model_number("unknown-model") == 1  # safe default

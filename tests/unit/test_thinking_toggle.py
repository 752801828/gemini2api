from app.config import settings


def test_extended_thinking_enabled_default_true():
    assert settings.extended_thinking_enabled is True


def test_toggle_is_editable_and_typed():
    from app.routers.settings import EDITABLE_FIELDS, FIELD_TYPES
    assert "extended_thinking_enabled" in EDITABLE_FIELDS
    assert FIELD_TYPES["extended_thinking_enabled"] is bool

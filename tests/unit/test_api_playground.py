from pathlib import Path


PLAYGROUND = Path(__file__).resolve().parents[2] / "static" / "api-playground.html"


def test_api_playground_keeps_supported_request_examples() -> None:
    html = PLAYGROUND.read_text(encoding="utf-8")

    assert "/v1/chat/completions" in html
    assert "/v1beta/models/" in html
    assert "inline_data" in html
    assert "YOUR_API_KEY" in html
    assert "MAX_IMAGES = 20" in html
    assert 'data-tab="python"' in html

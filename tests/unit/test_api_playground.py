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
    assert "Gemini 3.1 Pro" in html
    assert "Gemini 3.6 Flash · 扩展思考" in html
    assert "Gemini 3.1 Pro · 扩展思考" in html
    assert "Temperature" not in html
    assert "max_tokens" not in html

_AUTH = {"Authorization": "Bearer sk-test-key"}


def test_responses_reasoning_effort_enables_thinking(gem_client, monkeypatch):
    import app.routers.responses as rp
    captured = {}
    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None, account_id=None, extended_thinking=False):
        captured["ext"] = extended_thinking
        return {"text": "answer", "images": [], "conversation_id": "c", "thoughts": "resp thoughts"}
    monkeypatch.setattr(rp.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/responses", json={
        "model": "gemini-flash", "input": "hi", "reasoning": {"effort": "high"},
    }, headers=_AUTH)
    assert r.status_code == 200
    assert captured["ext"] is True
    types = [it.get("type") for it in r.json()["output"]]
    assert "reasoning" in types


def test_responses_no_reasoning_no_thinking(gem_client, monkeypatch):
    import app.routers.responses as rp
    captured = {}
    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None, account_id=None, extended_thinking=False):
        captured["ext"] = extended_thinking
        return {"text": "answer", "images": [], "conversation_id": "c", "thoughts": ""}
    monkeypatch.setattr(rp.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/responses", json={
        "model": "gemini-flash", "input": "hi",
    }, headers=_AUTH)
    assert r.status_code == 200
    assert captured["ext"] is False
    types = [it.get("type") for it in r.json()["output"]]
    assert "reasoning" not in types


def test_responses_top_level_reasoning_effort_enables_thinking(gem_client, monkeypatch):
    """顶层 reasoning_effort（无嵌套 reasoning.effort 时）也应启用思维链。"""
    import app.routers.responses as rp
    captured = {}
    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None, account_id=None, extended_thinking=False):
        captured["ext"] = extended_thinking
        return {"text": "answer", "images": [], "conversation_id": "c", "thoughts": ""}
    monkeypatch.setattr(rp.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/responses", json={
        "model": "gemini-flash", "input": "hi", "reasoning_effort": "high",
    }, headers=_AUTH)
    assert r.status_code == 200
    assert captured["ext"] is True


def test_responses_extended_thinking_disabled_by_settings_toggle(gem_client, monkeypatch):
    import app.routers.responses as rp
    monkeypatch.setattr(rp.settings, "extended_thinking_enabled", False)
    captured = {}
    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None, account_id=None, extended_thinking=False):
        captured["ext"] = extended_thinking
        return {"text": "answer", "images": [], "conversation_id": "c", "thoughts": ""}
    monkeypatch.setattr(rp.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/responses", json={
        "model": "gemini-flash", "input": "hi", "reasoning": {"effort": "high"},
    }, headers=_AUTH)
    assert r.status_code == 200
    assert captured["ext"] is False


def test_responses_thinking_failure_falls_back_to_normal(gem_client, monkeypatch):
    """thinking 路径失败应自动降级重试一次非思维链请求。"""
    import app.routers.responses as rp
    calls = []
    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None, account_id=None, extended_thinking=False):
        calls.append(extended_thinking)
        if extended_thinking:
            raise RuntimeError("thinking path exploded")
        return {"text": "recovered", "images": [], "conversation_id": "c", "thoughts": ""}
    monkeypatch.setattr(rp.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/responses", json={
        "model": "gemini-flash", "input": "hi", "reasoning": {"effort": "high"},
    }, headers=_AUTH)
    assert r.status_code == 200
    assert calls == [True, False]
    body = r.json()
    types = [it.get("type") for it in body["output"]]
    assert "reasoning" not in types
    assert any(it.get("type") == "message" for it in body["output"])

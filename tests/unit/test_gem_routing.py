_AUTH = {"Authorization": "Bearer sk-test-key"}


def test_models_includes_gem_model(gem_client):
    gem_client.post("/admin/gem-mapping", json={
        "model_name": "route-gem", "gem_id": "g1",
        "base_model": "gemini-pro", "account_id": "account-0",
    }, headers=_AUTH)
    r = gem_client.get("/v1/models", headers=_AUTH)
    ids = [m["id"] for m in r.json()["data"]]
    assert "route-gem" in ids


def test_chat_with_gem_model_passes_gem_id(gem_client, monkeypatch):
    import app.routers.openai as oai
    gem_client.post("/admin/gem-mapping", json={
        "model_name": "route-gem2", "gem_id": "g2",
        "base_model": "gemini-pro", "account_id": "account-1",
    }, headers=_AUTH)

    captured = {}
    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None, account_id=None,
                            extended_thinking=False):
        captured.update(model=model, gem_id=gem_id, account_id=account_id)
        return {"text": "hi", "images": [], "conversation_id": "c1"}

    monkeypatch.setattr(oai.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/chat/completions", json={
        "model": "route-gem2", "messages": [{"role": "user", "content": "hello"}],
    }, headers=_AUTH)
    assert r.status_code == 200
    assert captured["model"] == "gemini-pro"   # 用基础模型
    assert captured["gem_id"] == "g2"
    assert captured["account_id"] == "account-1"

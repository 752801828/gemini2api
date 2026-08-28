"""issue #10: Claude Code 把 Anthropic 的 system 作为文本块数组发送，旧模型只认字符串 → 422。"""
from app.models.claude import ClaudeRequest

_AUTH = {"Authorization": "Bearer sk-test-key"}

# Claude Code 真实形态：第一块 billing header，第二块系统提示词并带 ephemeral 缓存标记
_CC_SYSTEM = [
    {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.220.564; cc_entrypoint=cli;"},
    {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.",
     "cache_control": {"type": "ephemeral"}},
]


def test_system_block_array_is_flattened():
    r = ClaudeRequest(model="gemini-pro", messages=[{"role": "user", "content": "hi"}], system=_CC_SYSTEM)
    assert isinstance(r.system, str)
    assert "x-anthropic-billing-header" in r.system
    assert "You are Claude Code" in r.system
    assert r.system.index("x-anthropic-billing-header") < r.system.index("You are Claude Code")  # 顺序保持
    assert "\n\n" in r.system  # 块之间以空行分隔
    assert r.system == _CC_SYSTEM[0]["text"] + "\n\n" + _CC_SYSTEM[1]["text"]  # 精确锁定分隔符，防止 "\n\n\n" 等误判通过


def test_system_string_unchanged():
    r = ClaudeRequest(model="gemini-pro", messages=[{"role": "user", "content": "hi"}], system="plain system")
    assert r.system == "plain system"


def test_system_none_and_absent():
    r1 = ClaudeRequest(model="gemini-pro", messages=[{"role": "user", "content": "hi"}], system=None)
    r2 = ClaudeRequest(model="gemini-pro", messages=[{"role": "user", "content": "hi"}])
    assert r1.system is None and r2.system is None


def test_system_empty_or_non_text_blocks_become_none():
    for v in ([], [{"type": "image", "source": {}}], [{"type": "text"}], [{"type": "text", "text": ""}]):
        r = ClaudeRequest(model="gemini-pro", messages=[{"role": "user", "content": "hi"}], system=v)
        assert r.system is None, f"{v!r} 应视为无 system"


def test_full_claude_code_payload_validates():
    """守卫：Claude Code 的完整报文除 system 外不应再有字段被拒（tools/metadata/thinking 等）。"""
    ClaudeRequest(**{
        "model": "claude-sonnet-4-5", "max_tokens": 32000, "temperature": 1, "top_p": 0.95, "stream": True,
        "system": _CC_SYSTEM,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                     {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
                     {"role": "user", "content": "plain string too"}],
        "tools": [{"name": "Bash", "description": "run cmd",
                   "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
                   "cache_control": {"type": "ephemeral"}}],
        "tool_choice": {"type": "auto"}, "metadata": {"user_id": "abc"},
        "stop_sequences": ["</done>"], "thinking": {"type": "enabled", "budget_tokens": 10000},
    })


def test_messages_endpoint_accepts_array_system_and_uses_it(gem_client, monkeypatch):
    """端点级：曾经 422，现在 200；且拍平后的 system 确实进了送往上游的 prompt。"""
    import app.routers.claude as cl
    seen = {}

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        seen["prompt"] = prompt
        return {"text": "ok", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "system": _CC_SYSTEM,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH)
    assert r.status_code == 200, r.text
    assert "You are Claude Code" in seen["prompt"]      # 没被吞掉
    assert "System:" in seen["prompt"]


def test_messages_endpoint_string_system_regression(gem_client, monkeypatch):
    import app.routers.claude as cl
    seen = {}

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        seen["prompt"] = prompt
        return {"text": "ok", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)
    r = gem_client.post("/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100, "system": "plain system",
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH)
    assert r.status_code == 200, r.text
    assert "System: plain system" in seen["prompt"]


def test_count_tokens_accepts_array_system(gem_client):
    """同一模型，顺带覆盖 count_tokens（纯函数、不调上游）。
    只断言 >0 不够严谨：即使 system 被静默丢弃，无 system 的空壳请求也有 2 tokens 能通过。
    改为跟"不带 system 的同一请求"比较，要求带 system 的计数严格更大，才能证明文本确实被计入。"""
    base = gem_client.post("/v1/messages/count_tokens", json={
        "model": "gemini-pro", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH)
    assert base.status_code == 200, base.text
    baseline_tokens = base.json()["input_tokens"]

    r = gem_client.post("/v1/messages/count_tokens", json={
        "model": "gemini-pro", "max_tokens": 100, "system": _CC_SYSTEM,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["input_tokens"] > baseline_tokens


def test_messages_endpoint_empty_or_non_text_system_produces_no_system_section(gem_client, monkeypatch):
    """端点级：空/非文本 system 拍平为 None 后，prompt 里不应出现游离的 'System:' 段，
    且应与完全不传 system 时的 prompt 逐字节一致（不能悄悄留下空段落之类的痕迹）。"""
    import app.routers.claude as cl

    async def fake_generate(prompt, model, conversation_id="", attachments=None, gem_id=None,
                            account_id=None, extended_thinking=False):
        seen["prompt"] = prompt
        return {"text": "ok", "conversation_id": "", "images": [], "thoughts": ""}

    monkeypatch.setattr(cl.gemini_client, "generate", fake_generate)

    seen = {}
    no_system = gem_client.post("/v1/messages", json={
        "model": "gemini-pro", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers=_AUTH)
    assert no_system.status_code == 200, no_system.text
    no_system_prompt = seen["prompt"]

    for v in ([], [{"type": "image", "source": {}}], [{"type": "text"}], [{"type": "text", "text": ""}]):
        seen.clear()
        r = gem_client.post("/v1/messages", json={
            "model": "gemini-pro", "max_tokens": 100, "system": v,
            "messages": [{"role": "user", "content": "hi"}],
        }, headers=_AUTH)
        assert r.status_code == 200, r.text
        assert "System:" not in seen["prompt"], f"{v!r} 不应产生 System: 段，但 prompt={seen['prompt']!r}"
        assert seen["prompt"] == no_system_prompt, f"{v!r} 的 prompt 应与无 system 时完全一致"

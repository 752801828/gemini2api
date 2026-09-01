"""issue #10-C：可开关的完整请求/响应体记录。

LogRecord 早就有 request/response 字段、面板也早就有 log-detail-json 详情视图，
但中间件从未填充过它们。这里补上填充逻辑 + 开关。

三条硬约束由本文件钉死：
1. 默认关；关闭时中间件行为与开关引入前逐字节一致（两个字段恒为 None）。
2. **绝不缓冲流式响应体** —— 只记一个短标记，且流式响应本身不得被破坏。
3. 不记录任何请求头（Authorization 等凭据永不进日志）。
"""

import json

import pytest

from app.config import Settings

_AUTH = {"Authorization": "Bearer sk-test-key"}

_OPENAI_BODY = {"model": "gemini-flash", "messages": [{"role": "user", "content": "PROMPT_MARKER"}]}


class _CapturingLogStore:
    def __init__(self):
        self.records = []

    def add(self, record):
        self.records.append(record)

    def flush(self, *a, **k):
        return None

    @property
    def last(self):
        assert self.records, "中间件没有落任何日志记录"
        return self.records[-1]


@pytest.fixture
def log_client(app_main, tmp_path):
    """TestClient（不跑 lifespan），日志 store 换成会留存记录的替身。"""
    from fastapi.testclient import TestClient
    from app.core.model_mapping import ModelMapping
    from app.core.gem_mapping import GemMapping

    store = _CapturingLogStore()
    app_main.app.state.log_store = store
    app_main.app.state.model_mapping = ModelMapping(path=str(tmp_path / "mm.json"))
    app_main.app.state.gem_mapping = GemMapping(path=str(tmp_path / "gm.json"))
    return TestClient(app_main.app), store


def _fake_generate(text):
    async def gen(prompt, model, conversation_id="", attachments=None, gem_id=None,
                  account_id=None, extended_thinking=False):
        return {"text": text, "conversation_id": "", "images": [], "thoughts": ""}
    return gen


def _enable(monkeypatch, app_main, on=True):
    monkeypatch.setattr(app_main.settings, "log_bodies_enabled", on)


# ---------------------------------------------------------------------------
# 1. 默认关 + 关闭时零回归
# ---------------------------------------------------------------------------

def test_setting_defaults_to_false():
    assert Settings.model_fields["log_bodies_enabled"].default is False


def test_disabled_records_no_bodies(log_client, app_main, monkeypatch):
    """回归：开关关闭时 request/response 均为 None，与开关引入前一致。"""
    import app.routers.openai as oai
    client, store = log_client
    _enable(monkeypatch, app_main, on=False)
    monkeypatch.setattr(oai.gemini_client, "generate", _fake_generate("hello"))

    r = client.post("/v1/chat/completions", json=_OPENAI_BODY, headers=_AUTH)

    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello"
    assert store.last.request is None
    assert store.last.response is None


# ---------------------------------------------------------------------------
# 2. 开启 + 非流式：两者都被填充，内容正确，且响应体原样送达
# ---------------------------------------------------------------------------

def test_enabled_non_stream_records_both_bodies(log_client, app_main, monkeypatch):
    import app.routers.openai as oai
    client, store = log_client
    _enable(monkeypatch, app_main)
    monkeypatch.setattr(oai.gemini_client, "generate", _fake_generate("ANSWER_MARKER"))

    r = client.post("/v1/chat/completions", json=_OPENAI_BODY, headers=_AUTH)

    assert r.status_code == 200
    # 响应体必须原样送达客户端（重建响应不能破坏内容）
    assert r.json()["choices"][0]["message"]["content"] == "ANSWER_MARKER"
    assert r.headers["content-type"].startswith("application/json")

    rec = store.last
    assert rec.request["model"] == "gemini-flash"
    assert rec.request["messages"][0]["content"] == "PROMPT_MARKER"
    assert rec.response["choices"][0]["message"]["content"] == "ANSWER_MARKER"


def test_enabled_records_no_request_headers(log_client, app_main, monkeypatch):
    """凭据绝不进日志：整条记录里不得出现 Authorization / api key。"""
    import app.routers.openai as oai
    client, store = log_client
    _enable(monkeypatch, app_main)
    monkeypatch.setattr(oai.gemini_client, "generate", _fake_generate("hi"))

    client.post("/v1/chat/completions", json=_OPENAI_BODY, headers=_AUTH)

    dumped = json.dumps(store.last.to_dict(), ensure_ascii=False).lower()
    assert "authorization" not in dumped
    assert "sk-test-key" not in dumped


def test_admin_paths_are_not_captured(log_client, app_main, monkeypatch):
    """只对 API 路径记 body；/admin/* 仍旧只记元数据。"""
    client, store = log_client
    _enable(monkeypatch, app_main)

    r = client.get("/admin/status", headers=_AUTH)

    assert r.status_code == 200
    assert store.last.path == "/admin/status"
    assert store.last.request is None
    assert store.last.response is None


# ---------------------------------------------------------------------------
# 3. 开启 + 流式：只记标记，绝不缓冲，且流本身不得被破坏
# ---------------------------------------------------------------------------

def test_enabled_sse_stream_is_marked_not_buffered(log_client, app_main, monkeypatch):
    import app.routers.openai as oai
    client, store = log_client
    _enable(monkeypatch, app_main)
    monkeypatch.setattr(oai.gemini_client, "generate", _fake_generate("STREAM_MARKER"))

    r = client.post("/v1/chat/completions",
                    json={**_OPENAI_BODY, "stream": True, "tools": [{
                        "type": "function",
                        "function": {"name": "noop", "description": "d", "parameters": {}}}]},
                    headers=_AUTH)

    assert r.status_code == 200
    # 流本身完好：客户端仍收到全部帧
    assert "STREAM_MARKER" in r.text
    assert "data: [DONE]" in r.text

    rec = store.last
    assert rec.request["model"] == "gemini-flash"
    assert rec.response == {"_note": "streaming response not captured"}
    assert "STREAM_MARKER" not in json.dumps(rec.response, ensure_ascii=False)


def test_enabled_ndjson_stream_is_marked_not_buffered(log_client, app_main, monkeypatch):
    """gemini 的 :streamGenerateContent 用 media_type="application/json"（NDJSON 流），
    content-type 认不出来 —— 必须靠路径识别，否则会把一个真流式响应整个缓冲掉。"""
    import app.routers.gemini as gm
    client, store = log_client
    _enable(monkeypatch, app_main)
    monkeypatch.setattr(gm.gemini_client, "generate", _fake_generate("NDJSON_MARKER"))

    r = client.post("/v1beta/models/gemini-pro:streamGenerateContent", json={
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "tools": [{"function_declarations": [
            {"name": "noop", "description": "d", "parameters": {}}]}],
    }, headers=_AUTH)

    assert r.status_code == 200
    assert "NDJSON_MARKER" in r.text
    assert store.last.response == {"_note": "streaming response not captured"}


# ---------------------------------------------------------------------------
# 4. 超大响应体：截断 + 标注
# ---------------------------------------------------------------------------

def test_oversized_response_body_is_truncated_and_annotated(log_client, app_main, monkeypatch):
    import app.routers.openai as oai
    client, store = log_client
    _enable(monkeypatch, app_main)
    huge = "X" * (64 * 1024)
    monkeypatch.setattr(oai.gemini_client, "generate", _fake_generate(huge))

    r = client.post("/v1/chat/completions", json=_OPENAI_BODY, headers=_AUTH)

    assert r.status_code == 200
    # 客户端仍拿到完整的大响应，截断只发生在日志里
    assert r.json()["choices"][0]["message"]["content"] == huge

    rec = store.last
    assert rec.response["_truncated"] is True
    assert rec.response["_size"] > app_main.LOG_BODY_MAX_BYTES
    assert len(rec.response["_preview"]) <= app_main.LOG_BODY_MAX_BYTES


def test_oversized_request_body_is_truncated_and_annotated(log_client, app_main, monkeypatch):
    import app.routers.openai as oai
    client, store = log_client
    _enable(monkeypatch, app_main)
    monkeypatch.setattr(oai.gemini_client, "generate", _fake_generate("ok"))

    r = client.post("/v1/chat/completions", json={
        "model": "gemini-flash",
        "messages": [{"role": "user", "content": "Y" * (64 * 1024)}],
    }, headers=_AUTH)

    assert r.status_code == 200
    assert store.last.request["_truncated"] is True


# ---------------------------------------------------------------------------
# 5. 记录进得了 LogStore 的详情视图（to_dict 全量返回）
# ---------------------------------------------------------------------------

def test_captured_bodies_survive_to_dict(log_client, app_main, monkeypatch):
    import app.routers.openai as oai
    client, store = log_client
    _enable(monkeypatch, app_main)
    monkeypatch.setattr(oai.gemini_client, "generate", _fake_generate("DETAIL_MARKER"))

    client.post("/v1/chat/completions", json=_OPENAI_BODY, headers=_AUTH)

    d = store.last.to_dict()
    assert d["request"]["messages"][0]["content"] == "PROMPT_MARKER"
    assert d["response"]["choices"][0]["message"]["content"] == "DETAIL_MARKER"

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

    def update_response(self, record_id, response):
        for record in self.records:
            if record.id == record_id:
                record.response = response
                return

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


def test_disabled_keeps_sanitized_compatibility_preview(log_client, app_main, monkeypatch):
    """定制版关闭完整正文时仍保留既有的脱敏请求/响应预览。"""
    import app.routers.openai as oai
    client, store = log_client
    _enable(monkeypatch, app_main, on=False)
    monkeypatch.setattr(oai.gemini_client, "generate", _fake_generate("hello"))

    r = client.post("/v1/chat/completions", json=_OPENAI_BODY, headers=_AUTH)

    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello"
    assert store.last.request["messages"][0]["content"] == "PROMPT_MARKER"
    assert store.last.response["choices"][0]["message"]["content"] == "hello"


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


# ---------------------------------------------------------------------------
# 6. issue #10 followup F1：32KB → 4KB 上限 + flush 离线程（F1c/F1b）
# ---------------------------------------------------------------------------

def test_log_body_max_bytes_reduced_to_4kb(app_main):
    """F1(c)：审计实测 32KB body * 2000 条会把 flush() 从 56ms/0.6MB 拖到 4.3s+/119MB；
    收窄到 4KB 把单条记录的最坏开销压到可接受范围。"""
    assert app_main.LOG_BODY_MAX_BYTES == 4 * 1024


def test_flush_log_store_runs_off_the_event_loop_thread():
    """F1(b)：log_flush_loop 每 10 秒调一次，同步 flush() 会卡住事件循环；
    必须用 asyncio.to_thread 丢进线程池执行。"""
    import asyncio
    import threading

    from app import main as app_main

    calls = {"thread": None}

    class _Store:
        def flush(self):
            calls["thread"] = threading.current_thread()

    asyncio.run(app_main._flush_log_store(_Store()))

    assert calls["thread"] is not None
    assert calls["thread"] is not threading.main_thread()


# ---------------------------------------------------------------------------
# 7. issue #10 followup F3：捕获响应体失败绝不能让客户端拿到截断/空响应
# ---------------------------------------------------------------------------

def test_capture_failure_after_drain_does_not_truncate_response(log_client, app_main, monkeypatch):
    """强制在「body_iterator 已排空之后」的序列化阶段抛异常：客户端必须仍拿到完整、
    未被破坏的原始响应体；日志记录里对应字段跳过即可，绝不能拖累响应本身。"""
    import app.main as main_mod
    import app.routers.openai as oai

    client, store = log_client
    _enable(monkeypatch, app_main)
    monkeypatch.setattr(oai.gemini_client, "generate", _fake_generate("ANSWER_MARKER"))

    orig_body_for_log = main_mod._body_for_log
    calls = {"n": 0}

    def flaky(raw):
        calls["n"] += 1
        # 第 1 次调用是中间件里的请求体捕获（发生在 body_iterator 排空之前，不是本次要测的场景）；
        # 第 2 次调用是 _capture_response_body 里对已排空 chunks 的序列化——这里才是 F3 的目标窗口。
        if calls["n"] >= 2:
            raise RuntimeError("simulated failure serializing drained response body")
        return orig_body_for_log(raw)

    monkeypatch.setattr(main_mod, "_body_for_log", flaky)

    r = client.post("/v1/chat/completions", json=_OPENAI_BODY, headers=_AUTH)

    assert r.status_code == 200
    # 响应体必须完整送达，不能因为日志序列化失败而被截断成空包
    assert r.json()["choices"][0]["message"]["content"] == "ANSWER_MARKER"
    assert calls["n"] >= 2
    # 日志记录里的 response 字段允许跳过（None），但绝不能影响上面对响应体的断言
    assert store.last.response is None

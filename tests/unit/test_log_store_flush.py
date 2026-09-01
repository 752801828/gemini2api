"""issue #10 followup F1(a)：LogStore.flush() 落盘绝不带 request/response body。

审计实测：2000 条记录、32KB body 上限时，flush() 从 0.6MB/56ms 膨胀到 119MB/4.3s+，
且是每 10 秒在事件循环上同步跑一次——启用 LOG_BODIES_ENABLED 会让代理定期卡住几秒，
_load() 启动时还要重新读回这个巨型文件。修复：body 只留在内存环形缓冲区（面板详情视图
读的就是它），落盘内容与开关引入前的体积/耗时完全一致。
"""
import json

from app.core.log_store import LogStore, create_log_record


def _make_record(**kw):
    defaults = dict(method="POST", path="/v1/chat/completions", model="gemini-flash")
    defaults.update(kw)
    return create_log_record(**defaults)


def test_flush_strips_bodies_from_persisted_file(tmp_path):
    path = tmp_path / "logs.json"
    store = LogStore(capacity=10, persist_path=str(path))
    rec = _make_record(
        request_body={"messages": [{"role": "user", "content": "SECRET_PROMPT"}]},
        response_body={"choices": [{"message": {"content": "SECRET_ANSWER"}}]},
    )
    store.add(rec)
    store.flush()

    raw = path.read_text(encoding="utf-8")
    assert "SECRET_PROMPT" not in raw
    assert "SECRET_ANSWER" not in raw

    persisted = json.loads(raw)
    assert len(persisted) == 1
    assert persisted[0]["request"] is None
    assert persisted[0]["response"] is None


def test_flush_leaves_in_memory_record_bodies_intact(tmp_path):
    """面板详情视图读的是内存 buffer——落盘剥离 body 不能连带把内存里的也剥了。"""
    path = tmp_path / "logs.json"
    store = LogStore(capacity=10, persist_path=str(path))
    rec = _make_record(
        request_body={"messages": [{"role": "user", "content": "SECRET_PROMPT"}]},
        response_body={"choices": [{"message": {"content": "SECRET_ANSWER"}}]},
    )
    store.add(rec)
    store.flush()

    fetched = store.get(rec.id)
    assert fetched["request"]["messages"][0]["content"] == "SECRET_PROMPT"
    assert fetched["response"]["choices"][0]["message"]["content"] == "SECRET_ANSWER"

    queried = store.query(limit=10)["records"][0]
    assert queried["request"]["messages"][0]["content"] == "SECRET_PROMPT"
    assert queried["response"]["choices"][0]["message"]["content"] == "SECRET_ANSWER"


def test_reloaded_store_has_no_bodies(tmp_path):
    """重启后从磁盘 _load() 回来的记录里，body 字段本来就没写过，恒为 None。"""
    path = tmp_path / "logs.json"
    store = LogStore(capacity=10, persist_path=str(path))
    store.add(_make_record(request_body={"a": 1}, response_body={"b": 2}))
    store.flush()

    reloaded = LogStore(capacity=10, persist_path=str(path))
    data = reloaded.query(limit=10)["records"]
    assert len(data) == 1
    assert data[0]["request"] is None
    assert data[0]["response"] is None


def test_flush_file_size_unaffected_by_large_bodies(tmp_path):
    """性能维度的轻量回归：即便每条记录都塞满 4KB 上限的 body，落盘文件仍应保持很小
    （远小于「体积 * 条数」），因为 body 根本没写进去。"""
    path = tmp_path / "logs.json"
    store = LogStore(capacity=50, persist_path=str(path))
    huge_body = {"messages": [{"role": "user", "content": "X" * 4000}]}
    for _ in range(50):
        store.add(_make_record(request_body=huge_body, response_body=huge_body))
    store.flush()

    # 50 条 * 2 个 4KB body 若真落盘会 >= 400KB；剥离后应远小于这个量级。
    assert path.stat().st_size < 50_000


def test_disabled_toggle_records_have_no_bodies_to_begin_with(tmp_path):
    """开关关闭场景（request_body/response_body 传 None）：flush 前后都恒为 None，零回归。"""
    path = tmp_path / "logs.json"
    store = LogStore(capacity=10, persist_path=str(path))
    rec = _make_record(request_body=None, response_body=None)
    store.add(rec)
    assert store.get(rec.id)["request"] is None
    assert store.get(rec.id)["response"] is None
    store.flush()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted[0]["request"] is None
    assert persisted[0]["response"] is None

"""defect ⑨：第三方 Anthropic 转发只转换响应（_convert_anthropic_to_openai），不转换请求——
role:"tool" 和 content:null 都会被真实 Anthropic API 拒收（4xx），工具循环配置了第三方
Anthropic key 时走完第一轮就必然硬失败。覆盖 _openai_messages_to_anthropic 的纯逻辑转换
（不触网）与 _forward_anthropic 端到端发出的请求体（MockTransport，同 test_anthropic_thinking.py
的手法）。"""

import asyncio
import json

import httpx

import app.core.api_forwarder as fwd


def _no_ssrf(monkeypatch):
    monkeypatch.setattr(fwd, "_build_safe_target_url", lambda entry, suffix: entry.base_url.rstrip("/") + suffix)


class _Entry:
    def __init__(self):
        self.id = "e"
        self.provider = "anthropic"
        self.model = "claude"
        self.api_key = "sk"
        self.base_url = "https://up.example"
        self.reasoning_effort = None


class _Req:
    def __init__(self, stream=False):
        self.stream = stream
        self.max_tokens = None
        self.tools = None
        self.temperature = None
        self.tool_choice = None


# ---------------------------------------------------------------------------
# 纯逻辑：_openai_messages_to_anthropic 两轮工具循环的完整转换
# ---------------------------------------------------------------------------

_TWO_ROUND_TOOL_LOOP = [
    {"role": "system", "content": "You are a bot."},
    {"role": "user", "content": "read foo.py"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "read_file", "arguments": '{"path": "foo.py"}'}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"},
    {"role": "assistant", "content": "The file prints 1."},
    {"role": "user", "content": "now write bar.py"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_2", "type": "function",
         "function": {"name": "write_file", "arguments": '{"path": "bar.py", "content": "print(2)"}'}}]},
    # 第二轮工具结果 content 显式为 None：真实 Anthropic API 会拒收 null content。
    {"role": "tool", "tool_call_id": "call_2", "content": None},
]


def test_two_round_tool_loop_converted_exactly():
    system, msgs = fwd._openai_messages_to_anthropic(_TWO_ROUND_TOOL_LOOP)
    assert system == "You are a bot."
    assert msgs == [
        {"role": "user", "content": "read foo.py"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "foo.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "print(1)"}]},
        {"role": "assistant", "content": "The file prints 1."},
        {"role": "user", "content": "now write bar.py"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_2", "name": "write_file",
             "input": {"path": "bar.py", "content": "print(2)"}}]},
        # content:null -> "" — 与 Anthropic 官方 tool_result 的实际要求一致
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_2", "content": ""}]},
    ]


def test_plain_content_none_becomes_empty_string():
    """role:"tool"/tool_calls 之外的一般消息 content:null 同样兜底成 ""。"""
    system, msgs = fwd._openai_messages_to_anthropic(
        [{"role": "assistant", "content": None}])
    assert msgs == [{"role": "assistant", "content": ""}]


def test_tool_call_invalid_json_arguments_falls_back_not_raises():
    """function.arguments 是畸形 JSON 字符串：json.loads 失败必须降级兜底，不能抛异常
    击穿整个转发请求。"""
    system, msgs = fwd._openai_messages_to_anthropic(
        [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "not json"}}]}])
    block = msgs[0]["content"][0]
    assert block == {"type": "tool_use", "id": "c1", "name": "f", "input": {"_raw": "not json"}}


def test_assistant_tool_calls_with_text_content_keeps_both_blocks():
    """assistant 消息文本 + tool_calls 同时存在：文本块与 tool_use 块都要保留。"""
    system, msgs = fwd._openai_messages_to_anthropic(
        [{"role": "assistant", "content": "Let me check.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}])
    assert msgs == [{"role": "assistant", "content": [
        {"type": "text", "text": "Let me check."},
        {"type": "tool_use", "id": "c1", "name": "f", "input": {}},
    ]}]


def test_regular_user_assistant_messages_unaffected():
    """没有 tool_calls / role:"tool" 的普通消息转换结果不变（content 非空时逐字透传）。"""
    system, msgs = fwd._openai_messages_to_anthropic(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
    assert msgs == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]


# ---------------------------------------------------------------------------
# 端到端：_forward_anthropic 真正发给上游的请求体（MockTransport 抓包）
# ---------------------------------------------------------------------------

def test_forward_anthropic_sends_converted_body_for_tool_loop(monkeypatch):
    _no_ssrf(monkeypatch)
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}],
                                         "stop_reason": "end_turn", "usage": {}})

    def factory(timeout=300.0):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(fwd, "_make_async_client", factory)

    req = _Req(stream=False)
    asyncio.run(fwd._forward_anthropic(_Entry(), _TWO_ROUND_TOOL_LOOP, req))

    body = captured["body"]
    assert body["system"] == "You are a bot."
    # role:"tool" 从未出现在发给上游的 body 里——这是本次修复要堵住的那个必然 4xx。
    assert all(m["role"] != "tool" for m in body["messages"])
    assert body["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "print(1)"}],
    }
    assert body["messages"][-1] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_2", "content": ""}],
    }
    # content:null 从未出现——None 在 JSON 里会序列化成 null。
    assert "null" not in json.dumps(body)

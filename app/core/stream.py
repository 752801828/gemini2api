import asyncio
import contextlib
import json
from typing import Any, AsyncGenerator, AsyncIterator


async def split_into_chunks(text: str, delay: float = 0.03) -> AsyncGenerator[str, None]:
    words = text.split(" ")
    for i, word in enumerate(words):
        chunk = word if i == len(words) - 1 else word + " "
        yield chunk
        await asyncio.sleep(delay)


def format_sse(data: dict | str) -> str:
    if isinstance(data, dict):
        return f"data: {json.dumps(data)}\n\n"
    return f"data: {data}\n\n"


SSE_KEEPALIVE_INTERVAL: float = 10.0
SSE_KEEPALIVE_FRAME: str = ": ping\n\n"


async def iter_with_keepalive(agen, interval: float | None = None) -> AsyncIterator[tuple[str, Any]]:
    """包住 async 生成器 agen：
      - 每个上游事件 → yield ("evt", evt)
      - 上游静默超过 interval → yield ("ping", None)
      - 上游正常结束 → 本生成器正常结束
      - 上游抛异常 → 事件耗尽处原样重抛，交给调用方既有 try/except
    interval 默认 None → 在【调用时】从模块常量 SSE_KEEPALIVE_INTERVAL 解析（可被 monkeypatch）。
    实现要点：后台 task 消费 agen 塞进 Queue；主循环 wait_for(queue.get(), interval)
    超时只取消安全的 queue.get()，绝不取消 anext（避免击穿上游生成器）。"""
    if interval is None:
        interval = SSE_KEEPALIVE_INTERVAL
    queue: asyncio.Queue = asyncio.Queue()

    async def _pump():
        try:
            async for evt in agen:
                await queue.put(("evt", evt))
        except Exception as e:  # noqa: BLE001 上游异常透传给调用方
            await queue.put(("err", e))
        else:
            await queue.put(("done", None))

    task = asyncio.create_task(_pump())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield ("ping", None)
                continue
            if kind == "done":
                return
            if kind == "err":
                raise payload
            yield ("evt", payload)
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

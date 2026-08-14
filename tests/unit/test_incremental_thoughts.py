from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent.parent


def test_generate_stream_emits_incremental_thoughts_event():
    src = (_ROOT / "app" / "core" / "gemini_client.py").read_text(encoding="utf-8")
    # generate_stream 里有逐帧思考增量事件
    assert '{"type": "thoughts"' in src
    # 用独立累积游标做前缀 diff
    assert "emitted_thoughts" in src
    # 思考增量在答案 delta 之前 yield（guard 运行时 yield 语句本身，而非 docstring 提及顺序）
    assert src.index('yield {"type": "thoughts"') < src.index('yield {"type": "delta"'), \
        "思考增量必须在答案 delta 之前 yield"

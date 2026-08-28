# Fix C1: Claude SSE frames missing `event:` line — report

北京时间 2026-08-28 完成。

## Problem

`/v1/messages` streaming (`app/routers/claude.py`) emitted SSE frames as `data: {...}` only,
via the shared `app.core.stream.format_sse`. The real Anthropic Messages streaming API emits
two lines per frame (`event: <type>` + `data: {...}`); official SDKs dispatch on `event:`, so a
data-only stream can parse to zero events. `app/core/responses_protocol.py` already does this
correctly for the OpenAI-Responses protocol; only the Claude router was missing it.

## Fix

Scope: `app/routers/claude.py` only.

1. Added module-level helper `_claude_sse(data: dict) -> str` right after the imports/logger,
   emitting `f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"`. `json` was already
   imported in this file (line 3) — no new import needed.
2. Replaced every `format_sse(...)` call site in `app/routers/claude.py` with `_claude_sse(...)`.
   **Exact count: 20 replacements** (verified via `git diff` — every `yield format_sse(` /
   `yield format_sse({...})` occurrence across `_stream_claude`, `_stream_claude_buffered`, and
   the buffered error path became `_claude_sse`).
3. Removed `format_sse` from claude.py's import line (`from app.core.stream import
   split_into_chunks, iter_with_keepalive, SSE_KEEPALIVE_FRAME, sse_keepalive_during`) since
   nothing else in the file used it. `app/core/stream.py`'s shared `format_sse` itself was
   **not modified** — `app/routers/openai.py` and `app/routers/gemini.py` still import and use
   it unchanged (verified via grep: their call sites are untouched).
4. Keepalive `: ping` SSE-comment frames (`SSE_KEEPALIVE_FRAME`) were **not touched** — they
   are yielded raw, not through `format_sse`/`_claude_sse`, in both `iter_with_keepalive` and
   `sse_keepalive_during`.

### "type" key verification

Every one of the 20 call sites passes a dict literal with `"type"` set directly inline (not
computed/optional), confirmed by reading the full pre-fix file and cross-checking each site
individually: `message_start` (x2), `content_block_start` (x3), `content_block_delta` (x5),
`content_block_stop` (x3), `message_delta` (x3), `message_stop` (x3), `error` (x1,
buffered-generate-failure path). 2+3+5+3+3+3+1 = 20, matching the replacement count exactly.
No call site was found where `"type"` could be absent — nothing to flag.

## Tests

Added to `tests/unit/test_claude_buffered_keepalive.py`:

- `_parse_sse(body)` — real SSE-text parser (splits on blank-line-delimited chunks, skips `:`
  comment lines, pairs `event:`/`data:` lines) rather than substring assertions.
- `test_claude_stream_frames_are_parseable_with_event_field` — drives `/v1/messages` with
  `stream: true` AND `tools` (forces the buffered path, `_stream_claude_buffered`) with a fake
  `generate`. Asserts every parsed frame has a non-`None` `event`, `event == data["type"]` for
  every frame, sequence starts with `message_start` and ends with `message_stop`, and no
  `": ping"` frame surfaces as a parsed event.
- `test_claude_real_stream_frames_are_parseable_with_event_field` — same assertions, but drives
  a request **without** `tools` (fake `generate_stream`, delta/final events) to exercise the
  non-buffered real-stream branch of `_stream_claude`.

Both new tests pass; the pre-existing substring-based tests in the same file (which would have
passed even on an unparseable data-only stream) are left as-is per instructions — only the new
real-parser tests were added.

## Verification

- `python3 -m pytest tests/unit -q` (system python; repo `.venv` has no pytest):
  **244 passed, 1 xfailed** (baseline was 242 passed + 1 xfailed; +2 new tests, zero
  regressions, same xfail as baseline — `test_known_false_positive_draw_a_conclusion`).
- `python3 -m pytest tests/unit/test_claude_buffered_keepalive.py -v`: 6 passed (4 original + 2
  new).
- `ruff check .`: All checks passed.
- Grepped `app/routers/claude.py` for leftover `format_sse`: none found.
- `app/core/stream.py` diff: none (untouched, confirmed via `git diff --stat`).

## Commit

`git commit --author="xwteam <xwteam@xwteam.cn>"`, no Co-Authored-By trailer, no push.
Files touched: `app/routers/claude.py`, `tests/unit/test_claude_buffered_keepalive.py`. No
changes to `docs/` or `.superpowers/` source content (this report file itself lives under
`.superpowers/sdd/...` per instructions, added in the same commit).

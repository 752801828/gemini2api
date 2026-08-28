import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.core.account_pool import account_pool as gemini_client
from app.core.gemini_client import HTTPStatusError, classify_error
from app.core.stream import split_into_chunks, iter_with_keepalive
from app.models.gemini import (
    GeminiRequest,
    GeminiResponse,
    GeminiCandidate,
    GeminiContent,
    GeminiPart,
    GeminiUsageMetadata,
    GeminiModelInfo,
    GeminiModelList,
)
from app.utils.tools import build_tool_prompt, parse_tool_response, estimate_tokens, is_image_generation_intent
from app.utils.prompt import build_prompt_from_messages, last_user_text
from app.core.limiter import limiter, dynamic_rate_limit, rate_limit_exempt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Gemini"])


def _whitelist_set() -> set[str] | None:
    """解析 MODEL_WHITELIST（逗号分隔）为集合；为空返回 None 表示不过滤。
    白名单条目按裸模型名匹配（兼容传入带 `models/` 前缀的写法）。"""
    raw = (settings.model_whitelist or "").strip()
    if not raw:
        return None
    allowed = set()
    for m in raw.split(","):
        m = m.strip()
        if not m:
            continue
        allowed.add(m[7:] if m.startswith("models/") else m)
    return allowed or None


def _parse_system(system_instruction) -> str | None:
    """system_instruction 可能是 str 或 GeminiContent，统一取文本。"""
    if system_instruction is None:
        return None
    if isinstance(system_instruction, str):
        return system_instruction or None
    parts = getattr(system_instruction, "parts", None)
    if parts:
        texts = [p.text for p in parts if getattr(p, "text", None)]
        if texts:
            return " ".join(texts)
    return None


def _parse_contents(contents):
    """从 Gemini contents 解析出 messages 和 attachments（inline_data）。"""
    messages = []
    attachments = []
    idx = 0
    for content in contents:
        role = content.role
        text_parts = [part.text for part in content.parts if part.text]
        if text_parts:
            messages.append({"role": role, "content": " ".join(text_parts)})
        for part in content.parts:
            inline = getattr(part, "inline_data", None)
            if inline and isinstance(inline, dict):
                import base64
                mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
                raw = inline.get("data", "")
                try:
                    data = base64.b64decode(raw) if isinstance(raw, str) else raw
                except Exception:
                    continue
                ext = mime.split("/")[-1] if "/" in mime else "bin"
                attachments.append({"data": data, "filename": f"image_{idx}.{ext}", "mime": mime})
                idx += 1
    return messages, attachments


@router.get("/models")
async def list_models():
    """List available Gemini models."""
    models = [
        GeminiModelInfo(
            name="models/gemini-2.0-flash-exp",
            display_name="Gemini 2.0 Flash Experimental",
            description="Fast and efficient model for general tasks",
        ),
        GeminiModelInfo(
            name="models/gemini-1.5-pro",
            display_name="Gemini 1.5 Pro",
            description="Advanced model for complex reasoning",
        ),
        GeminiModelInfo(
            name="models/gemini-1.5-flash",
            display_name="Gemini 1.5 Flash",
            description="Fast model for quick responses",
        ),
    ]
    # MODEL_WHITELIST 过滤（为空则放行全部）；按裸模型名匹配 name 去掉 `models/` 前缀
    allowed = _whitelist_set()
    if allowed is not None:
        models = [
            m for m in models
            if (m.name[7:] if m.name.startswith("models/") else m.name) in allowed
        ]
    return JSONResponse(content=GeminiModelList(models=models).model_dump())


@router.post("/models/{model}:generateContent")
@limiter.limit(dynamic_rate_limit, exempt_when=rate_limit_exempt)
async def generate_content(model: str, req: GeminiRequest, request: Request):
    """Generate content using Gemini API (non-streaming)."""
    if model.startswith("models/"):
        model = model[7:]

    # gem 模型解析：命中则取出 gem_id/account_id，并把对话模型换成 base_model
    gem_mapping = getattr(request.app.state, "gem_mapping", None)
    gem_id = None
    gem_account_id = None
    if gem_mapping:
        gem_info = gem_mapping.resolve(model)
        if gem_info:
            gem_id = gem_info.get("gem_id")
            gem_account_id = gem_info.get("account_id") or None
            model = gem_info.get("base_model") or "gemini-pro"

    messages, attachments = _parse_contents(req.contents)
    system = _parse_system(req.system_instruction)
    prompt = build_prompt_from_messages(messages, system=system)

    has_tools = False
    # 生图意图只看最后一轮用户消息：prompt 含 system_instruction 与全部历史轮次，
    # 拿它判断会误判成生图，从而静默丢掉客户端声明的 functionDeclarations。
    if req.tools and not is_image_generation_intent(last_user_text(messages)):
        function_declarations = []
        for tool in req.tools:
            if tool.function_declarations:
                function_declarations.extend([fd.model_dump() for fd in tool.function_declarations])
        if function_declarations:
            has_tools = True
            prompt = build_tool_prompt(prompt, function_declarations)

    try:
        result = await gemini_client.generate(prompt, model, "", attachments,
                                              gem_id=gem_id, account_id=gem_account_id)
    except (RuntimeError, ValueError, HTTPStatusError) as e:
        status, err_type, retry_after = classify_error(e)
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        return JSONResponse(
            status_code=status,
            content={"error": {"message": str(e), "type": err_type}},
            headers=headers,
        )

    response_text = result.get("text", "")

    # 工具调用：解析成 Gemini 原生 functionCall part（而非把工具 JSON 当文本塞回去）
    tool_parts = []
    if has_tools:
        parsed = parse_tool_response(response_text)
        if isinstance(parsed, dict):
            if parsed.get("type") == "tool_calls":
                for tc in parsed["tool_calls"]:
                    tool_parts.append({"functionCall": {
                        "name": tc["name"],
                        "args": tc.get("arguments", {}),
                    }})
                response_text = ""  # 工具调用时不再带文本
            else:
                response_text = parsed.get("content", response_text)

    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(response_text)

    # parts：图片在前 + 文本。inlineData（Gemini 原生 base64）给能解析的客户端，
    # 同时图片本地托管 URL 排在文字前面（图在前），方便不渲染 inlineData 的客户端拿到可点链接。
    gen_images = result.get("images") or []
    base = str(request.base_url).rstrip("/")
    text_part = response_text
    if gen_images and base:
        urls = "\n".join(f"![generated image]({base}/images/{im['id']})"
                         for im in gen_images if im.get("id"))
        if urls:
            text_part = (urls + "\n" + response_text.strip()) if response_text.strip() else urls
    # 工具调用 part 优先；否则文本 part + 图片
    if tool_parts:
        parts = tool_parts
        finish = "STOP"
    else:
        parts = [{"text": text_part}]
        for im in gen_images:
            parts.append({"inlineData": {"mimeType": im.get("mime", "image/png"), "data": im["b64"]}})
        finish = "STOP"

    gemini_response = {
        "candidates": [{
            "content": {"parts": parts, "role": "model"},
            "finishReason": finish,
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": completion_tokens,
            "totalTokenCount": prompt_tokens + completion_tokens,
        },
    }

    return JSONResponse(content=gemini_response)


@router.post("/models/{model}:streamGenerateContent")
@limiter.limit(dynamic_rate_limit, exempt_when=rate_limit_exempt)
async def stream_generate_content(model: str, req: GeminiRequest, request: Request):
    """Generate content using Gemini API (streaming with chunked JSON)."""
    if model.startswith("models/"):
        model = model[7:]

    # gem 模型解析：命中则取出 gem_id/account_id，并把对话模型换成 base_model
    gem_mapping = getattr(request.app.state, "gem_mapping", None)
    gem_id = None
    gem_account_id = None
    if gem_mapping:
        gem_info = gem_mapping.resolve(model)
        if gem_info:
            gem_id = gem_info.get("gem_id")
            gem_account_id = gem_info.get("account_id") or None
            model = gem_info.get("base_model") or "gemini-pro"

    messages, attachments = _parse_contents(req.contents)
    system = _parse_system(req.system_instruction)
    prompt = build_prompt_from_messages(messages, system=system)

    has_tools = False
    # 生图意图只看最后一轮用户消息：prompt 含 system_instruction 与全部历史轮次，
    # 拿它判断会误判成生图，从而静默丢掉客户端声明的 functionDeclarations。
    if req.tools and not is_image_generation_intent(last_user_text(messages)):
        function_declarations = []
        for tool in req.tools:
            if tool.function_declarations:
                function_declarations.extend([fd.model_dump() for fd in tool.function_declarations])
        if function_declarations:
            has_tools = True
            prompt = build_tool_prompt(prompt, function_declarations)

    async def stream_generator() -> AsyncGenerator[str, None]:
        def _chunk(text: str) -> str:
            return json.dumps({
                "candidates": [{
                    "content": {"parts": [{"text": text}], "role": "model"},
                    "index": 0,
                }]
            }) + "\n"

        prompt_tokens = estimate_tokens(prompt)
        response_text = ""

        # 有工具/附件：需完整文本，走非流式收集后切片（零回归）
        if has_tools or attachments:
            try:
                result = await gemini_client.generate(prompt, model, "", attachments,
                                                      gem_id=gem_id, account_id=gem_account_id)
            except Exception as e:
                yield json.dumps({"error": {"message": str(e), "type": "api_error"}}) + "\n"
                return
            response_text = result.get("text", "")
            if has_tools:
                parsed = parse_tool_response(response_text)
                if isinstance(parsed, dict):
                    response_text = parsed.get("content", response_text)
            async for chunk in split_into_chunks(response_text):
                yield _chunk(chunk)
        else:
            # === 真流式路径（纯文本）===
            try:
                async for _kind, evt in iter_with_keepalive(
                    gemini_client.generate_stream(prompt, model, "", attachments,
                                                  gem_id=gem_id, account_id=gem_account_id)):
                    if _kind == "ping":
                        # NDJSON blank-line keepalive (SSE ": ping" comment is invalid on this application/json stream)
                        yield "\n"
                        continue
                    if evt.get("type") == "delta":
                        # generate_stream 现在是严格 append-only（不再发 _replace），delta 总是新增尾部
                        delta = evt.get("text", "")
                        response_text += delta
                        if delta:
                            yield _chunk(delta)
                    elif evt.get("type") == "final":
                        # final.text 是过滤完占位串的完整文本，可能比已流出的 response_text
                        # 多出（流式时被 hold 住的尾部）。补发缺失尾部，保证拿到完整内容。
                        final_text = evt.get("text", response_text)
                        if final_text.startswith(response_text) and len(final_text) > len(response_text):
                            tail = final_text[len(response_text):]
                            response_text = final_text
                            if tail:
                                yield _chunk(tail)
                        else:
                            response_text = final_text
            except Exception as e:
                # generate_stream 在 HTTP>=400 时抛 HTTPStatusError（非 RuntimeError/ValueError 子类），
                # 故这里捕获 Exception，与 openai/claude 真流式路径一致，避免击穿生成器
                yield json.dumps({"error": {"message": str(e), "type": "api_error"}}) + "\n"
                return

        completion_tokens = estimate_tokens(response_text)

        # Google 原生 wire format 用 camelCase（finishReason/usageMetadata...），
        # 与非流式 generateContent 端点一致；snake_case 会被官方 SDK 静默忽略，
        # 导致流式端丢失 finishReason 和用量统计。
        final_chunk = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": ""}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": prompt_tokens,
                "candidatesTokenCount": completion_tokens,
                "totalTokenCount": prompt_tokens + completion_tokens,
            },
        }
        yield json.dumps(final_chunk) + "\n"

    return StreamingResponse(stream_generator(), media_type="application/json")

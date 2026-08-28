from pydantic import BaseModel, Field, field_validator
from typing import Any


class ClaudeMessage(BaseModel):
    role: str
    content: str | list


class ClaudeTool(BaseModel):
    name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict)


class ClaudeRequest(BaseModel):
    model: str
    max_tokens: int = 4096
    messages: list[ClaudeMessage]
    system: str | list | None = None
    stream: bool = False
    tools: list[ClaudeTool] | None = None
    tool_choice: dict | None = None

    @field_validator("system", mode="before")
    @classmethod
    def _flatten_system(cls, v):
        """Anthropic 的 system 允许字符串或文本块数组 [{type:"text",text:...}]（Claude Code 发的是
        数组，块上可能带 cache_control）。这里在类型校验前把数组拍平成字符串，下游只需处理 str。"""
        if isinstance(v, (list, tuple)):
            parts = [b["text"] for b in v
                     if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
            joined = "\n\n".join(p for p in parts if p)
            return joined or None
        return v


class ContentBlock(BaseModel):
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None
    source: dict | None = None  # type=image 时：{type:"base64",media_type,data}


class ClaudeUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ClaudeResponse(BaseModel):
    id: str
    type: str = "message"
    role: str = "assistant"
    model: str = ""
    content: list[ContentBlock] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: ClaudeUsage = Field(default_factory=ClaudeUsage)


class ClaudeModelInfo(BaseModel):
    id: str
    type: str = "model"
    created_at: str = ""
    display_name: str = ""


class ClaudeModelList(BaseModel):
    data: list[ClaudeModelInfo]

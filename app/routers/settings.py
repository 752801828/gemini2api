import logging
import math
from pathlib import Path
from typing import Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.core.account_pool import account_pool, RotationStrategy
from app.core.settings_overrides import (
    EDITABLE_FIELDS,
    FIELD_TYPES,
    NON_NEGATIVE_FIELDS as _NON_NEGATIVE_FIELDS,
    POSITIVE_FIELDS as _POSITIVE_FIELDS,
    coerce_value,
    save_overrides,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/settings", tags=["Settings"])

# 白名单 / 类型表 / 取值域集合的唯一真源在 app.core.settings_overrides —— app/config.py
# 需要在启动时用同一份定义回放持久化的面板改动，而 config 不能反向 import 本路由。
# 这里保留同名再导出，历史调用方（含测试）仍可 `from app.routers.settings import ...`。
__all__ = ["router", "EDITABLE_FIELDS", "FIELD_TYPES", "SettingsResponse", "SettingsUpdateRequest"]


class SettingsResponse(BaseModel):
    """Grouped settings response"""
    performance: Dict[str, Any] = Field(description="Performance-related settings")
    rate_limiting: Dict[str, Any] = Field(description="Rate limiting configuration")
    health_check: Dict[str, Any] = Field(description="Health check configuration")
    account_management: Dict[str, Any] = Field(description="Account rotation settings")
    usage_stats: Dict[str, Any] = Field(description="Usage statistics settings")
    chat_cleanup: Dict[str, Any] = Field(default_factory=dict, description="Web chat auto-cleanup settings")
    # 新增分组必须在此显式声明：pydantic 默认 extra="ignore"，且路由声明了
    # response_model=SettingsResponse，未声明的分组会被静默丢弃（面板永远看不到该开关）。
    logging: Dict[str, Any] = Field(default_factory=dict, description="Request/response body logging settings")


class SettingsUpdateRequest(BaseModel):
    """Request body for updating settings"""
    settings: Dict[str, Any] = Field(description="Key-value pairs of settings to update")

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        for key in v.keys():
            if key not in EDITABLE_FIELDS:
                raise ValueError(f"Setting '{key}' is not editable")
        return v


def _get_grouped_settings() -> Dict[str, Dict[str, Any]]:
    """Get current settings grouped by category"""
    return {
        "performance": {
            "refresh_interval": settings.refresh_interval,
            "max_retries": settings.max_retries,
            "jitter_enabled": settings.jitter_enabled,
        },
        "rate_limiting": {
            "rate_limit_enabled": settings.rate_limit_enabled,
            "rate_limit_window": settings.rate_limit_window,
            "rate_limit_max": settings.rate_limit_max,
        },
        "health_check": {
            "health_check_enabled": settings.health_check_enabled,
            "health_check_interval": settings.health_check_interval,
        },
        "account_management": {
            "rotation_strategy": settings.rotation_strategy,
            "max_concurrent_per_account": settings.max_concurrent_per_account,
        },
        "usage_stats": {
            "usage_stats_enabled": settings.usage_stats_enabled,
            "usage_stats_interval": settings.usage_stats_interval,
            "usage_stats_retention_days": settings.usage_stats_retention_days,
        },
        "chat_cleanup": {
            "chat_cleanup_enabled": settings.chat_cleanup_enabled,
            "chat_cleanup_keep_hours": settings.chat_cleanup_keep_hours,
            "chat_cleanup_interval_hours": settings.chat_cleanup_interval_hours,
            "chat_cleanup_skip_pinned": settings.chat_cleanup_skip_pinned,
            "extended_thinking_enabled": settings.extended_thinking_enabled,
        },
        "logging": {
            "log_bodies_enabled": settings.log_bodies_enabled,
        },
    }


def _update_env_file(updates: Dict[str, Any]) -> None:
    """Update .env file with new values"""
    env_path = Path(".env")

    if not env_path.exists():
        lines = []
        for key, value in updates.items():
            env_key = key.upper()
            lines.append(f"{env_key}={value}")
        env_path.write_text("\n".join(lines) + "\n")
        logger.info(f"Created .env file with {len(updates)} settings")
        return

    # Read existing content
    content = env_path.read_text()
    lines = content.splitlines()

    # Track which keys were updated
    updated_keys = set()
    new_lines = []

    # Update existing lines
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            key_part = line.split("=", 1)[0].strip()
            matching_update = None
            for update_key, update_value in updates.items():
                if update_key.upper() == key_part.upper():
                    matching_update = (update_key, update_value)
                    break
            if matching_update:
                key, value = matching_update
                new_lines.append(f"{key_part}={value}")
                updated_keys.add(key)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # Add new keys that weren't in the file
    for key, value in updates.items():
        if key not in updated_keys:
            env_key = key.upper()
            new_lines.append(f"{env_key}={value}")

    # Write back to file
    env_path.write_text("\n".join(new_lines) + "\n")
    logger.info(f"Updated .env file with {len(updates)} settings")


def _update_in_memory_settings(updates: Dict[str, Any]) -> None:
    """Update in-memory settings object"""
    for key, value in updates.items():
        # Validate type
        expected_type = FIELD_TYPES.get(key)
        if expected_type and not isinstance(value, expected_type):
            raise ValueError(f"Setting '{key}' must be of type {expected_type.__name__}")

        # Update using object.__setattr__ to bypass pydantic immutability
        object.__setattr__(settings, key, value)
        logger.info(f"Updated in-memory setting: {key}={value}")

    # Propagate to AccountPool
    if "rotation_strategy" in updates:
        account_pool.set_strategy(updates["rotation_strategy"])
    if "max_concurrent_per_account" in updates:
        account_pool.set_max_concurrent(updates["max_concurrent_per_account"])


@router.get("", response_model=SettingsResponse)
async def get_settings() -> SettingsResponse:
    """Get current editable settings, grouped by category."""
    grouped = _get_grouped_settings()
    return SettingsResponse(**grouped)


def _validate_settings_domain(updates: Dict[str, Any]) -> None:
    """对每个待更新值做类型 + 取值域校验（任何持久化之前）。

    只做类型检查不够：type-correct 但取值非法的值（如 rotation_strategy='garbage'、
    或因 bool 是 int 子类导致 True 通过 int 检查）会被写进 .env，并在下次启动时
    让 RotationStrategy(...) / Settings() 构造抛异常，永久 brick 启动。
    这里在写盘前拒绝这些值。
    """
    for key, value in updates.items():
        expected_type = FIELD_TYPES.get(key)
        if expected_type is None:
            continue

        # bool 是 int 的子类：int/float 字段必须显式拒绝 bool，
        # 否则 JSON true 会被当成合法 int 写进 .env 破坏下次启动。
        if expected_type in (int, float) and isinstance(value, bool):
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' must be of type {expected_type.__name__}, got bool",
            )

        if not isinstance(value, expected_type):
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' must be of type {expected_type.__name__}, got {type(value).__name__}",
            )

        # NaN / ±Infinity 是合法 float 且 `nan < 0` / `inf < 0` 均为 False，会绕过
        # 下面所有比较，被原样持久化并在每次启动回放（chat_cleanup_interval_hours=inf
        # 会让清理循环 asyncio.sleep(inf) 永不唤醒）。必须在这里挡掉。
        if isinstance(value, float) and not math.isfinite(value):
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' must be a finite number",
            )

        # 取值域：计数/间隔不允许负数，并发上限至少为 1
        if key in _NON_NEGATIVE_FIELDS and value < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' must be >= 0",
            )
        if key in _POSITIVE_FIELDS and value < 1:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' must be >= 1",
            )

    # rotation_strategy 必须是 RotationStrategy 枚举的合法成员，否则下次启动时
    # account_pool 模块级实例化会 RotationStrategy(value) 抛 ValueError 阻断导入。
    if "rotation_strategy" in updates:
        valid = {s.value for s in RotationStrategy}
        if updates["rotation_strategy"] not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"Setting 'rotation_strategy' must be one of {sorted(valid)}",
            )


def _coerce_payload(updates: Dict[str, Any]) -> Dict[str, Any]:
    """把 JSON 数值收敛到字段声明的类型（主要是 int -> float）。

    JSON 没有 int/float 之分，前端 number 输入取整后 48 会以 int 上线，而
    chat_cleanup_keep_hours / chat_cleanup_interval_hours 声明是 float。不收敛的话
    整个保存请求 400，同一次提交里的其它字段（比如隐私开关 log_bodies_enabled）
    也一起被拒 —— 校验是原子的，用户只看到一个笼统的"保存失败"。
    非法值不在这里报错，仍交给 _validate_settings_domain 统一给出 400 文案。
    """
    coerced = dict(updates)
    for key, value in updates.items():
        ok, new_value = coerce_value(key, value)
        if ok:
            coerced[key] = new_value
    return coerced


@router.post("", response_model=SettingsResponse)
async def update_settings(request: SettingsUpdateRequest) -> SettingsResponse:
    """Update application settings. Updates the persisted overrides, .env and memory."""
    try:
        updates = _coerce_payload(request.settings)

        # 1) 写盘前完成全部类型 + 取值域校验（包含 rotation_strategy 枚举校验）
        _validate_settings_domain(updates)

        # 2) 先更新内存（含对 account_pool set_strategy/set_max_concurrent 的真实生效），
        #    成功后才写盘；失败则回滚内存，保证不会把会 brick 启动的坏值持久化。
        snapshot = {key: getattr(settings, key) for key in updates if hasattr(settings, key)}
        try:
            _update_in_memory_settings(updates)
        except Exception:
            # 回滚已改动的内存值，避免半更新状态
            for key, old in snapshot.items():
                object.__setattr__(settings, key, old)
            raise

        # 3) 内存更新成功，持久化。data/settings-overrides.json 才是真正跨重启生效的
        #    那份（详见 app/core/settings_overrides 模块注释：docker-compose 用 env_file
        #    注入真实环境变量，压过容器内 .env，且宿主机 .env 根本没挂进容器）。
        #    .env 仍然照写，保持裸机/源码部署的配置文件可读可改。
        #
        #    写盘同样要回滚内存：只读的 data/ 或 .env（自定 compose `user:`、rootless
        #    podman uid 映射、只读卷）会让这里抛异常。若不回滚，管理员看到的是"保存失败"，
        #    而值其实已经在内存里生效了 —— 对 log_bodies_enabled 这种隐私开关，
        #    "以为没关掉、其实关了"和"以为关掉了、其实还在记录"都不可接受。
        try:
            save_overrides(updates)
            _update_env_file(updates)
        except Exception:
            for key, old in snapshot.items():
                object.__setattr__(settings, key, old)
            if "rotation_strategy" in snapshot:
                account_pool.set_strategy(snapshot["rotation_strategy"])
            if "max_concurrent_per_account" in snapshot:
                account_pool.set_max_concurrent(snapshot["max_concurrent_per_account"])
            raise

        # Return updated settings
        grouped = _get_grouped_settings()
        return SettingsResponse(**grouped)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # 细节只进日志：原始异常里带着容器内绝对路径和原子写的临时文件名，
        # 没必要回给浏览器。
        logger.error(f"Failed to update settings: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to update settings; see server logs for details",
        )

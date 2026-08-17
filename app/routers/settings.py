import logging
from pathlib import Path
from typing import Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.core.account_pool import account_pool, RotationStrategy

# 这些字段语义上是计数/间隔，必须为非负（其中并发上限至少为 1）；用于域校验，
# 防止把负数/0 等会让服务在下次启动时无法构造 Settings 或行为异常的值写进 .env。
_NON_NEGATIVE_FIELDS = {
    "refresh_interval",
    "max_retries",
    "rate_limit_window",
    "rate_limit_max",
    "health_check_interval",
    "usage_stats_interval",
    "usage_stats_retention_days",
    "chat_cleanup_keep_hours",
    "chat_cleanup_interval_hours",
}
_POSITIVE_FIELDS = {
    "max_concurrent_per_account",  # 并发上限必须 >= 1
}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/settings", tags=["Settings"])

# Whitelist of editable settings
EDITABLE_FIELDS = {
    "refresh_interval",
    "max_retries",
    "rate_limit_enabled",
    "rate_limit_window",
    "rate_limit_max",
    "health_check_enabled",
    "health_check_interval",
    "rotation_strategy",
    "max_concurrent_per_account",
    "usage_stats_enabled",
    "usage_stats_interval",
    "usage_stats_retention_days",
    "jitter_enabled",
    "version_sync_enabled",
    "chat_cleanup_enabled",
    "chat_cleanup_keep_hours",
    "chat_cleanup_interval_hours",
    "chat_cleanup_skip_pinned",
    "extended_thinking_enabled",
}

# Type mapping for validation
FIELD_TYPES = {
    "refresh_interval": int,
    "max_retries": int,
    "rate_limit_enabled": bool,
    "rate_limit_window": int,
    "rate_limit_max": int,
    "health_check_enabled": bool,
    "health_check_interval": int,
    "rotation_strategy": str,
    "max_concurrent_per_account": int,
    "usage_stats_enabled": bool,
    "usage_stats_interval": int,
    "usage_stats_retention_days": int,
    "jitter_enabled": bool,
    "version_sync_enabled": bool,
    "chat_cleanup_enabled": bool,
    "chat_cleanup_keep_hours": float,
    "chat_cleanup_interval_hours": float,
    "chat_cleanup_skip_pinned": bool,
    "extended_thinking_enabled": bool,
}


class SettingsResponse(BaseModel):
    """Grouped settings response"""
    performance: Dict[str, Any] = Field(description="Performance-related settings")
    rate_limiting: Dict[str, Any] = Field(description="Rate limiting configuration")
    health_check: Dict[str, Any] = Field(description="Health check configuration")
    account_management: Dict[str, Any] = Field(description="Account rotation settings")
    usage_stats: Dict[str, Any] = Field(description="Usage statistics settings")
    chat_cleanup: Dict[str, Any] = Field(default_factory=dict, description="Web chat auto-cleanup settings")


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
        if key == "max_retries" and value not in (1, 2):
            raise HTTPException(
                status_code=400,
                detail="Setting 'max_retries' must be 1 or 2",
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


@router.post("", response_model=SettingsResponse)
async def update_settings(request: SettingsUpdateRequest) -> SettingsResponse:
    """Update application settings. Updates both .env file and in-memory settings."""
    try:
        # 1) 写盘前完成全部类型 + 取值域校验（包含 rotation_strategy 枚举校验）
        _validate_settings_domain(request.settings)

        # 2) 先更新内存（含对 account_pool set_strategy/set_max_concurrent 的真实生效），
        #    成功后才写 .env；失败则回滚内存，保证 .env 不会被写入会 brick 启动的坏值。
        snapshot = {key: getattr(settings, key) for key in request.settings if hasattr(settings, key)}
        try:
            _update_in_memory_settings(request.settings)
        except Exception:
            # 回滚已改动的内存值，避免半更新状态
            for key, old in snapshot.items():
                object.__setattr__(settings, key, old)
            raise

        # 3) 内存更新成功，持久化到 .env
        _update_env_file(request.settings)

        # Return updated settings
        grouped = _get_grouped_settings()
        return SettingsResponse(**grouped)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to update settings: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update settings: {str(e)}")

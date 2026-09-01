"""LOG_BODIES_ENABLED 必须能在管理面板里开关（v1.6.38 只给了环境变量）。

覆盖：白名单/类型表登记、GET 分组回显、POST 内存热生效、非法类型被拒且内存不被改动、
以及"默认必须是 False"这条隐私红线（请求体含用户完整提示词）。
"""

import pytest

from app.config import Settings, settings
import app.routers.settings as sr

_AUTH = {"Authorization": "Bearer sk-test-key"}


@pytest.fixture(autouse=True)
def _restore_log_bodies():
    """settings 是模块级单例，_update_in_memory_settings 直接 setattr 且不还原。

    不复位会污染同一次 pytest 会话里后续的测试（本文件按字母序排在
    test_log_bodies_toggle.py 之前）。
    """
    old = settings.log_bodies_enabled
    yield
    object.__setattr__(settings, "log_bodies_enabled", old)


@pytest.fixture
def _no_env_write(monkeypatch):
    """拦截 .env 落盘：_update_env_file 用的是相对路径 Path(".env")，

    pytest 的 CWD 是仓库根目录，不拦截会真的改写开发者本地的 .env。
    返回捕获到的持久化入参，用于断言"确实要求写盘了"。
    """
    captured = {}
    monkeypatch.setattr(sr, "_update_env_file", lambda updates: captured.update(updates))
    return captured


def test_default_stays_false():
    """隐私红线：请求体含用户完整提示词，上了面板也绝不能改默认值。"""
    assert Settings.model_fields["log_bodies_enabled"].default is False


def test_is_editable_and_typed():
    assert "log_bodies_enabled" in sr.EDITABLE_FIELDS
    assert sr.FIELD_TYPES["log_bodies_enabled"] is bool


def test_get_exposes_field_in_logging_group(client):
    """必须出现在 GET 响应里 —— 面板 renderSettings() 只渲染后端返回的分组。

    注意 SettingsResponse 声明了 response_model，未声明的分组会被静默丢弃，
    所以这里断言的是 HTTP 实际响应体而不是 _get_grouped_settings() 的返回值。
    """
    resp = client.get("/admin/settings", headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "logging" in body, f"logging 分组被响应模型丢掉了: {sorted(body)}"
    assert "log_bodies_enabled" in body["logging"]
    assert body["logging"]["log_bodies_enabled"] is settings.log_bodies_enabled


def test_post_toggles_in_memory_both_directions(client, _no_env_write):
    """热生效：app/main.py 是按请求读 settings.log_bodies_enabled，改完无需重启。"""
    object.__setattr__(settings, "log_bodies_enabled", False)

    resp = client.post(
        "/admin/settings",
        json={"settings": {"log_bodies_enabled": True}},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert settings.log_bodies_enabled is True
    assert _no_env_write == {"log_bodies_enabled": True}
    # 回显必须带上新值，否则面板保存后 UI 会立刻回退成旧值
    assert resp.json()["logging"]["log_bodies_enabled"] is True

    resp = client.post(
        "/admin/settings",
        json={"settings": {"log_bodies_enabled": False}},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert settings.log_bodies_enabled is False
    assert _no_env_write == {"log_bodies_enabled": False}
    assert resp.json()["logging"]["log_bodies_enabled"] is False


@pytest.mark.parametrize("bad_value", ["yes", 1, None])
def test_post_rejects_non_bool_without_touching_memory(client, _no_env_write, bad_value):
    """类型校验必须在写盘/改内存之前生效，坏值不能残留在内存里。"""
    object.__setattr__(settings, "log_bodies_enabled", False)

    resp = client.post(
        "/admin/settings",
        json={"settings": {"log_bodies_enabled": bad_value}},
        headers=_AUTH,
    )
    assert resp.status_code == 400
    assert "log_bodies_enabled" in resp.json()["detail"]
    assert settings.log_bodies_enabled is False
    assert _no_env_write == {}

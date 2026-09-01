"""float 字段必须接受 JSON 整数，且前端不能把小数截断。

缺陷：chat_cleanup_keep_hours / chat_cleanup_interval_hours 后端声明为 float，
但 JSON 没有 int/float 之分（48 与 48.0 都序列化成 `48`），前端 number 输入又走
parseInt。结果任何对这两个字段的修改都必然 400 `must be of type float, got int`；
而 _validate_settings_domain 是原子的，同一次保存里的其它字段（比如隐私开关
log_bodies_enabled）也被一起拒掉，用户只看到一个笼统的"保存失败"。
"""

import re
from pathlib import Path

import pytest

from app.config import settings
import app.routers.settings as sr

_AUTH = {"Authorization": "Bearer sk-test-key"}
_SETTINGS_JS = Path(__file__).resolve().parents[2] / "static" / "app" / "settings.js"


@pytest.fixture(autouse=True)
def _restore_settings():
    keys = ("log_bodies_enabled", "chat_cleanup_keep_hours", "chat_cleanup_interval_hours")
    old = {k: getattr(settings, k) for k in keys}
    yield
    for k, v in old.items():
        object.__setattr__(settings, k, v)


@pytest.fixture
def _no_persist(monkeypatch):
    monkeypatch.setattr(sr, "_update_env_file", lambda updates: None)
    monkeypatch.setattr(sr, "save_overrides", lambda updates: None)


def test_float_field_accepts_json_int(client, _no_persist):
    resp = client.post(
        "/admin/settings",
        json={"settings": {"chat_cleanup_keep_hours": 48}},
        headers=_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert settings.chat_cleanup_keep_hours == 48.0
    assert isinstance(settings.chat_cleanup_keep_hours, float)


def test_combined_save_no_longer_drops_the_privacy_toggle(client, _no_persist):
    """真实用户路径：改保留时长 + 打开记录开关，一次提交。之前整单 400，开关静默没生效。"""
    object.__setattr__(settings, "log_bodies_enabled", False)

    resp = client.post(
        "/admin/settings",
        json={"settings": {"chat_cleanup_keep_hours": 48, "log_bodies_enabled": True}},
        headers=_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert settings.log_bodies_enabled is True
    assert settings.chat_cleanup_keep_hours == 48.0


def test_float_field_still_accepts_real_floats(client, _no_persist):
    resp = client.post(
        "/admin/settings",
        json={"settings": {"chat_cleanup_interval_hours": 0.5}},
        headers=_AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert settings.chat_cleanup_interval_hours == 0.5


def test_bool_is_still_rejected_for_numeric_fields(client, _no_persist):
    """bool 是 int 子类，收敛逻辑绝不能顺手把 True 变成 1.0 写进配置。"""
    resp = client.post(
        "/admin/settings",
        json={"settings": {"chat_cleanup_keep_hours": True}},
        headers=_AUTH,
    )
    assert resp.status_code == 400
    assert "bool" in resp.json()["detail"]


def test_int_field_still_rejects_float(client, _no_persist):
    """收敛只允许 int -> float，反向（2.5 -> refresh_interval）必须仍然 400。"""
    resp = client.post(
        "/admin/settings",
        json={"settings": {"refresh_interval": 2.5}},
        headers=_AUTH,
    )
    assert resp.status_code == 400
    assert "refresh_interval" in resp.json()["detail"]


def test_negative_float_still_rejected(client, _no_persist):
    resp = client.post(
        "/admin/settings",
        json={"settings": {"chat_cleanup_keep_hours": -1}},
        headers=_AUTH,
    )
    assert resp.status_code == 400
    assert ">= 0" in resp.json()["detail"]


# --------------------------------------------------------------------------
# 前端侧：小数不能被 parseInt 截断
# --------------------------------------------------------------------------

def test_number_inputs_are_rendered_with_a_step_attribute():
    """整数值 step=1、小数值 step=any —— collectFormValues 靠它选 parseInt/parseFloat。"""
    src = _SETTINGS_JS.read_text(encoding="utf-8")
    assert "Number.isInteger(value) ? '1' : 'any'" in src, "step 判定逻辑没了"
    assert re.search(r'type="number"[^`]*step="\$\{step\}"', src), "number 输入没带上 step"


def test_collect_form_values_uses_parse_float_for_fractional_fields():
    src = _SETTINGS_JS.read_text(encoding="utf-8")
    assert "input.step === 'any' ? parseFloat(input.value) : parseInt(input.value, 10)" in src, (
        "collectFormValues 又变回一律 parseInt 了，小数会被静默截断成整数"
    )

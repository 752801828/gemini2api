"""float 字段必须接受 JSON 整数，且前端不能把小数截断。

缺陷：chat_cleanup_keep_hours / chat_cleanup_interval_hours 后端声明为 float，
但 JSON 没有 int/float 之分（48 与 48.0 都序列化成 `48`），前端 number 输入又走
parseInt。结果任何对这两个字段的修改都必然 400 `must be of type float, got int`；
而 _validate_settings_domain 是原子的，同一次保存里的其它字段（比如隐私开关
log_bodies_enabled）也被一起拒掉，用户只看到一个笼统的"保存失败"。
"""

import json
import re
import shutil
import subprocess
import tempfile
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
#
# 这里刻意不写 `assert "<源码字面量>" in src` —— 那种断言只证明字符还在，证明不了
# 行为对。前一版正是这么写的：它对"按当前值猜整数/小数"这个必然失效的启发式全程绿灯
# （后端发 24.0，JSON 到 JS 就是 24，永远猜成整数字段），也扛不住把 'number' 拼错这类
# 会让整条保存链路失效的改动。改成两条有牙的：
#   1) 跨语言不变量：JS 的 FLOAT_FIELDS 必须等于后端 FIELD_TYPES 里的 float 字段集合；
#   2) 真跑一遍 JS（有 node 就跑）：喂后端真实 wire 值，断言 0.5 不会变成 0。
# --------------------------------------------------------------------------

_BACKEND_FLOAT_FIELDS = {k for k, v in sr.FIELD_TYPES.items() if v is float}


def _js_float_fields() -> set:
    """从 settings.js 里解析出 FLOAT_FIELDS 集合的字面量成员。"""
    src = _SETTINGS_JS.read_text(encoding="utf-8")
    m = re.search(r"const FLOAT_FIELDS = new Set\(\[(.*?)\]\)", src, re.S)
    assert m, "settings.js 里找不到 FLOAT_FIELDS 集合"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_frontend_float_field_set_matches_backend_field_types():
    """前端必须按字段名判定 float，且这份名单要和后端 FIELD_TYPES 保持一致。

    后端新增/删除 float 字段而前端没跟，这条会红 —— 否则新字段的小数又会被 parseInt
    悄悄截断，而且是"保存成功"地截断。
    """
    assert _js_float_fields() == _BACKEND_FLOAT_FIELDS, (
        "static/app/settings.js 的 FLOAT_FIELDS 与后端 FIELD_TYPES 的 float 字段不一致"
    )
    assert _BACKEND_FLOAT_FIELDS, "后端已经没有 float 字段了，这组测试的前提变了"


def test_frontend_does_not_guess_float_ness_from_the_current_value():
    """守住回归：不能再用 Number.isInteger(value) 猜字段类型。

    后端把 24.0 序列化成 JSON 的 24.0，JSON.parse 之后就是 JS 的 24，
    Number.isInteger 恒为真 —— 默认部署下这个启发式永远选不到小数分支。
    """
    src = _SETTINGS_JS.read_text(encoding="utf-8")
    assert "Number.isInteger(value)" not in src, (
        "又回到按当前值猜 float 了；后端发 24.0 到 JS 就是 24，这个判断永远猜错"
    )


def _slice_js(pattern: str, label: str) -> str:
    """从 settings.js 里原样切出一段源码。切不到就是被改名/删了，直接失败。"""
    src = _SETTINGS_JS.read_text(encoding="utf-8")
    m = re.search(pattern, src, re.S)
    assert m, f"settings.js 里找不到{label}，前端保存逻辑被改动了"
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能真正执行前端逻辑")
def test_fractional_hours_survive_the_real_frontend_round_trip(client, _no_persist):
    """真跑 settings.js 里的判定/取值代码：0.5 必须原样送到后端，并被后端接受成 0.5。

    settings.js 是浏览器 ES module（import 了 auth.js/i18n.js，依赖 DOM 全局），
    node 里整体导入必然失败，所以这里把相关代码**原样切片**后执行 —— 断言的是行为，
    不是"字面量还在不在"。喂进去的是真实服务端 wire 值（float 会被序列化成 24.0）。
    """
    wire = client.get("/admin/settings", headers=_AUTH).json()["chat_cleanup"]

    float_set = _slice_js(r"const FLOAT_FIELDS = new Set\(\[.*?\]\);", "FLOAT_FIELDS 集合")
    is_float_fn = _slice_js(r"function isFloatField\(key\) \{.*?\n\}", "isFloatField()")
    step_line = _slice_js(r"const step = [^;\n]*;", "createFieldInput 里的 step 判定")
    collect_line = _slice_js(
        r"value = [^;\n]*parseInt\(input\.value, 10\);", "collectFormValues 的取值"
    )

    driver = f"""
{float_set}
{is_float_fn}

const wire = {json.dumps(wire)};
const out = {{}};
for (const [field, raw] of Object.entries(wire)) {{
  if (typeof raw !== 'number') continue;
  const key = 'chat_cleanup.' + field;
  const value = raw;
  {step_line}
  // 管理员在这个 input 里敲了 0.5
  const input = {{ type: 'number', step: step, value: '0.5' }};
  let value2;
  {collect_line.replace("value =", "value2 =")}
  out[field] = {{ step: step, posted: value2 }};
}}
console.log(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as d:
        driver_path = Path(d) / "float_probe.mjs"
        driver_path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(driver_path)], capture_output=True, text=True, timeout=60
        )

    assert proc.returncode == 0, f"node 执行失败: {proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    for field in _BACKEND_FLOAT_FIELDS:
        assert result[field]["step"] == "any", f"{field} 渲染成了 step=1，0.5 会被截断"
        assert result[field]["posted"] == 0.5, (
            f"{field}: 管理员输入 0.5，前端却要提交 {result[field]['posted']}"
        )

    # 后端确实接受这个 0.5（而不是靠 ">= 0" 把截断出来的 0 也放行）。
    posted = {f: result[f]["posted"] for f in _BACKEND_FLOAT_FIELDS}
    resp = client.post("/admin/settings", json={"settings": posted}, headers=_AUTH)
    assert resp.status_code == 200, resp.text
    for field in _BACKEND_FLOAT_FIELDS:
        assert getattr(settings, field) == 0.5

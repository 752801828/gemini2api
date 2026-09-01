"""面板改动必须跨重启存活（data/settings-overrides.json）。

背景缺陷：docker-compose 用 `env_file: .env` 把宿主机 .env 注入成**真实环境变量**，
pydantic-settings 里 os.environ 优先级高于 dotenv 文件；且 compose 只挂载 ./data 和
./api，宿主机 .env 在容器里不可见，_update_env_file 写的是容器内临时的 /app/.env。
结果：面板上把 LOG_BODIES_ENABLED 关掉、弹"保存成功"，`docker compose restart` 之后
又变回开着，继续记录用户完整提示词，且全程无任何提示。

这里覆盖：环境变量场景下的回放优先级、坏文件不 brick 启动、白名单收口、
以及路由 POST 确实写了持久化文件。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings, settings
from app.core import settings_overrides as so
import app.routers.settings as sr

_AUTH = {"Authorization": "Bearer sk-test-key"}


@pytest.fixture
def overrides_path(tmp_path, monkeypatch):
    path = tmp_path / "settings-overrides.json"
    monkeypatch.setattr(so, "OVERRIDES_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _restore_settings():
    """settings 是模块级单例，POST 会直接 setattr 且不还原。"""
    keys = ("log_bodies_enabled", "chat_cleanup_keep_hours", "max_retries")
    old = {k: getattr(settings, k) for k in keys}
    yield
    for k, v in old.items():
        object.__setattr__(settings, k, v)


@pytest.fixture
def _no_env_write(monkeypatch):
    captured = {}
    monkeypatch.setattr(sr, "_update_env_file", lambda updates: captured.update(updates))
    return captured


# --------------------------------------------------------------------------
# 核心回归：环境变量说 true，面板持久化的 false 必须赢
# --------------------------------------------------------------------------

def test_override_beats_real_environment_variable(overrides_path, monkeypatch):
    """还原 docker-compose 场景：宿主机 .env 经 env_file 变成真实环境变量。

    没有 overrides 层时 Settings() 只能读到环境变量的 True —— 这正是"面板关了、
    重启后又打开"的根因。
    """
    monkeypatch.setenv("LOG_BODIES_ENABLED", "true")

    fresh = Settings()
    assert fresh.log_bodies_enabled is True, "前提不成立：环境变量本应压过 .env"

    overrides_path.write_text(json.dumps({"log_bodies_enabled": False}), encoding="utf-8")
    applied = so.apply_overrides(fresh, overrides_path)

    assert applied == {"log_bodies_enabled": False}
    assert fresh.log_bodies_enabled is False


def test_override_survives_a_simulated_restart(overrides_path, monkeypatch):
    """保存 -> 重启（重建 Settings 并回放）-> 值还在。"""
    monkeypatch.setenv("LOG_BODIES_ENABLED", "true")

    so.save_overrides({"log_bodies_enabled": False}, overrides_path)

    for _ in range(3):  # 反复"重启"都必须稳定
        rebooted = Settings()
        so.apply_overrides(rebooted, overrides_path)
        assert rebooted.log_bodies_enabled is False


def test_apply_returns_empty_when_no_file(tmp_path):
    fresh = Settings()
    assert so.apply_overrides(fresh, tmp_path / "nope.json") == {}


# --------------------------------------------------------------------------
# 坏文件绝不能 brick 启动
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content",
    ["{ not json", "[]", '"a string"', "null"],
)
def test_corrupt_file_is_ignored_not_fatal(overrides_path, content):
    overrides_path.write_text(content, encoding="utf-8")
    assert so.load_overrides(overrides_path) == {}
    assert so.apply_overrides(Settings(), overrides_path) == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"rotation_strategy": "garbage"},          # 会让 RotationStrategy() 抛异常
        {"max_concurrent_per_account": 0},         # 并发上限 0 = 永远拿不到账号
        {"refresh_interval": -5},                  # 负间隔
        {"log_bodies_enabled": "yes"},             # 类型不对
    ],
)
def test_domain_invalid_values_are_dropped(overrides_path, bad):
    overrides_path.write_text(json.dumps(bad), encoding="utf-8")
    assert so.apply_overrides(Settings(), overrides_path) == {}


def test_non_editable_keys_are_never_loaded_or_saved(overrides_path):
    """持久化文件不是万能后门：只有面板白名单里的字段能被写入/回放。"""
    overrides_path.write_text(
        json.dumps({"api_key": "sk-attacker", "log_bodies_enabled": True}), encoding="utf-8"
    )
    assert so.load_overrides(overrides_path) == {"log_bodies_enabled": True}

    fresh = Settings()
    before = fresh.api_key
    so.apply_overrides(fresh, overrides_path)
    assert fresh.api_key == before

    so.save_overrides({"api_key": "sk-attacker2", "max_retries": 7}, overrides_path)
    assert json.loads(overrides_path.read_text(encoding="utf-8")) == {
        "log_bodies_enabled": True,
        "max_retries": 7,
    }


def test_save_merges_instead_of_replacing(overrides_path):
    so.save_overrides({"max_retries": 7}, overrides_path)
    so.save_overrides({"log_bodies_enabled": True}, overrides_path)
    assert json.loads(overrides_path.read_text(encoding="utf-8")) == {
        "max_retries": 7,
        "log_bodies_enabled": True,
    }


def test_save_creates_missing_parent_directory(tmp_path):
    """容器里 data/ 可能还不存在（首次启动、或空卷）。"""
    target = tmp_path / "data" / "settings-overrides.json"
    so.save_overrides({"log_bodies_enabled": True}, target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"log_bodies_enabled": True}


def test_overrides_live_under_the_bind_mounted_data_dir():
    """默认路径必须在 data/ 下 —— 只有它在 docker-compose 里是 bind mount，重启不丢。

    读源码而不是读模块属性：tests/conftest.py 会把运行时的 OVERRIDES_PATH 重定向到
    临时目录，防止测试改动开发者/CI 的真实文件。
    """
    src = Path(so.__file__).read_text(encoding="utf-8")
    assert 'OVERRIDES_PATH = Path("data/settings-overrides.json")' in src

    compose = Path(so.__file__).resolve().parents[2] / "docker-compose.yml"
    assert "./data:/app/data" in compose.read_text(encoding="utf-8"), (
        "data/ 不再是 bind mount 的话，这套持久化就又不跨重启了"
    )


# --------------------------------------------------------------------------
# 白名单/类型表的一致性守卫
# --------------------------------------------------------------------------

def test_tables_agree_with_each_other_and_with_settings_model():
    assert set(so.FIELD_TYPES) == so.EDITABLE_FIELDS
    unknown = so.EDITABLE_FIELDS - set(Settings.model_fields)
    assert not unknown, f"这些字段不在 Settings 上，回放时会被丢弃: {sorted(unknown)}"


def test_router_reexports_the_single_source_of_truth():
    assert sr.EDITABLE_FIELDS is so.EDITABLE_FIELDS
    assert sr.FIELD_TYPES is so.FIELD_TYPES


def test_rotation_strategy_whitelist_matches_the_enum():
    """settings_overrides 不能 import account_pool（循环），故硬编码了枚举取值。

    枚举一旦新增成员而这里没跟上，面板存得进去、回放却会被静默丢弃。
    """
    from app.core.account_pool import RotationStrategy

    assert so.ROTATION_STRATEGIES == {s.value for s in RotationStrategy}


# --------------------------------------------------------------------------
# 路由层：POST 必须真的落到持久化文件
# --------------------------------------------------------------------------

def test_post_persists_to_overrides_file(client, overrides_path, _no_env_write):
    object.__setattr__(settings, "log_bodies_enabled", False)

    resp = client.post(
        "/admin/settings",
        json={"settings": {"log_bodies_enabled": True}},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert json.loads(overrides_path.read_text(encoding="utf-8")) == {"log_bodies_enabled": True}

    # 关回去也必须落盘，否则"关掉"只在内存里、重启又开着
    resp = client.post(
        "/admin/settings",
        json={"settings": {"log_bodies_enabled": False}},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert json.loads(overrides_path.read_text(encoding="utf-8")) == {"log_bodies_enabled": False}


def test_rejected_post_persists_nothing(client, overrides_path, _no_env_write):
    resp = client.post(
        "/admin/settings",
        json={"settings": {"log_bodies_enabled": "yes"}},
        headers=_AUTH,
    )
    assert resp.status_code == 400
    assert not overrides_path.exists()


def test_config_replays_overrides_at_import_time(tmp_path):
    """真·端到端：起一个干净的 Python 进程，模拟 docker-compose 的部署形态。

    - data/settings-overrides.json 里是面板关掉的 False（data/ 是 bind mount，重启还在）
    - LOG_BODIES_ENABLED=true 是 env_file 注入的真实环境变量（宿主机 .env 那份）

    只 import app.config 就必须看到 False。不在 import 期回放（比如挪到 lifespan、
    或者干脆没接上）的话，account_pool 这些在 import 期读 settings 的模块拿到的还是旧值。
    """
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings-overrides.json").write_text(
        json.dumps({"log_bodies_enabled": False, "max_retries": 7}), encoding="utf-8"
    )

    repo_root = Path(so.__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)
    env["LOG_BODIES_ENABLED"] = "true"
    env["MAX_RETRIES"] = "3"
    env["API_KEY"] = "sk-subprocess-test"

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.config as c;"
            "print(c.settings.log_bodies_enabled, c.settings.max_retries,"
            " sorted(c.APPLIED_OVERRIDES))",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False 7 ['log_bodies_enabled', 'max_retries']", proc.stdout


def test_env_var_and_override_do_not_interfere_with_other_fields(overrides_path, monkeypatch):
    """回放只动被覆盖的字段，其它字段仍然由环境变量说了算。"""
    monkeypatch.setenv("MAX_RETRIES", "9")
    overrides_path.write_text(json.dumps({"log_bodies_enabled": True}), encoding="utf-8")

    fresh = Settings()
    so.apply_overrides(fresh, overrides_path)
    assert fresh.max_retries == 9
    assert fresh.log_bodies_enabled is True


def test_does_not_touch_process_environment(overrides_path):
    """回放只改内存对象，不污染 os.environ（子进程/refresher 不该被牵连）。"""
    before = dict(os.environ)
    so.save_overrides({"log_bodies_enabled": True}, overrides_path)
    so.apply_overrides(Settings(), overrides_path)
    assert dict(os.environ) == before

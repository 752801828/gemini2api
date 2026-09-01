"""面板设置项的标签/翻译完整性守卫。

这里防的是一类反复发生的缺陷：后端往 EDITABLE_FIELDS 里加了字段，前端忘了加标签，
面板就直接把 snake_case 原始字段名显示给用户（extended_thinking_enabled 曾经如此）；
或者标签写成硬编码中文字面量，英日韩面板照样显示中文
（usage_stats_interval / usage_stats_retention_days 曾经如此）。

纯文本解析静态资源。为避免"随机变红"，解析器只认仓库里实际使用的书写格式，
一旦解析不到东西就直接失败并提示格式已变，而不是静默通过。
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_JS = _ROOT / "static" / "app" / "settings.js"
_I18N_JS = _ROOT / "static" / "app" / "i18n.js"

_LANGS = ("zh-CN", "en-US", "ja-JP", "ko-KR", "zh-TW")


def _extract_object_literal(source: str, opening: str) -> dict:
    """抽出 `opening` 之后第一个对象字面量里的 `key: 'value'` 键值对。"""
    start = source.index(opening) + len(opening)
    end = source.index("};", start)
    body = source[start:end]
    pairs = re.findall(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*:\s*'([^']*)'", body, re.M)
    assert pairs, f"没解析到任何键值对，{_SETTINGS_JS.name} 的书写格式可能变了: {opening!r}"
    return dict(pairs)


def _field_label_map() -> dict:
    src = _SETTINGS_JS.read_text(encoding="utf-8")
    head = src.index("function getFieldLabel(")
    return _extract_object_literal(src[head:], "const map = {")


def _group_title_map() -> dict:
    src = _SETTINGS_JS.read_text(encoding="utf-8")
    head = src.index("function getGroupTitle(")
    return _extract_object_literal(src[head:], "const map = {")


def _field_hint_map() -> dict:
    src = _SETTINGS_JS.read_text(encoding="utf-8")
    return _extract_object_literal(src, "const FIELD_HINTS = {")


def _i18n_settings_keys() -> dict:
    """{语言: 该语言块里所有 settings.* 键}"""
    blocks = {}
    current = None
    for line in _I18N_JS.read_text(encoding="utf-8").splitlines():
        lang = re.match(r"^ {4}'([A-Za-z-]+)': \{$", line)
        if lang:
            current = lang.group(1)
            blocks[current] = set()
            continue
        if current is None:
            continue
        key = re.match(r"^ +'(settings\.[^']+)':", line)
        if key:
            blocks[current].add(key.group(1))
    assert set(_LANGS) <= set(blocks), f"i18n.js 语言块解析异常: {sorted(blocks)}"
    return blocks


def test_every_editable_field_has_a_label():
    from app.routers.settings import EDITABLE_FIELDS

    missing = set(EDITABLE_FIELDS) - set(_field_label_map())
    assert not missing, f"这些可编辑字段在 getFieldLabel 里没有标签，面板会显示原始字段名: {sorted(missing)}"


def test_every_backend_group_has_a_title():
    """同一类漂移的上一层：后端加了分组、settings.js 的 getGroupTitle 没加。

    getGroupTitle 的兜底是 `t(map[groupKey] || groupKey)`，而 t() 对未知键直接返回
    键名本身，于是面板会在所有语言下把 "security" 这种原始分组名当标题显示出来。
    """
    from app.routers.settings import _get_grouped_settings

    missing = set(_get_grouped_settings()) - set(_group_title_map())
    assert not missing, f"这些后端分组在 getGroupTitle 里没有标题，面板会显示原始分组名: {sorted(missing)}"


def test_response_model_exposes_every_backend_group():
    """SettingsResponse 未声明的分组会被 pydantic 静默丢弃，面板根本看不到。"""
    from app.routers.settings import SettingsResponse, _get_grouped_settings

    dropped = set(_get_grouped_settings()) - set(SettingsResponse.model_fields)
    assert not dropped, f"这些分组没在 SettingsResponse 上声明，会被响应模型丢掉: {sorted(dropped)}"


def test_labels_and_group_titles_are_i18n_keys_not_literals():
    """标签值必须是 i18n 键；写死字面量会让非中文面板显示中文。"""
    offenders = {
        k: v
        for k, v in {**_field_label_map(), **_group_title_map(), **_field_hint_map()}.items()
        if not v.startswith("settings.")
    }
    assert not offenders, f"这些标签是硬编码字面量而不是 i18n 键: {offenders}"


def test_referenced_i18n_keys_exist_in_every_language():
    referenced = set(_field_label_map().values()) | set(_group_title_map().values()) | set(_field_hint_map().values())
    referenced = {k for k in referenced if k.startswith("settings.")}
    assert referenced, "没解析到任何 i18n 键引用"

    blocks = _i18n_settings_keys()
    missing = {lang: sorted(referenced - blocks[lang]) for lang in _LANGS if referenced - blocks[lang]}
    assert not missing, f"这些语言块缺少已被引用的 i18n 键: {missing}"


def test_all_language_blocks_share_the_same_settings_keys():
    blocks = _i18n_settings_keys()
    baseline = blocks["zh-CN"]
    drift = {
        lang: {"missing": sorted(baseline - blocks[lang]), "extra": sorted(blocks[lang] - baseline)}
        for lang in _LANGS
        if blocks[lang] != baseline
    }
    assert not drift, f"5 个语言块的 settings.* 键集合不一致: {drift}"

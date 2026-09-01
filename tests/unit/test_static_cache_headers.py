"""面板静态资源必须每次回源校验，升级后不能拿到陈旧的 JS 模块。

缺陷：StaticFiles 默认不发 Cache-Control，浏览器按启发式新鲜度自行缓存；而
index.html 只给 base.css / mobile.css / app.js 挂了 ?v= 版本串，app.js 里
`import './settings.js'` / `import './i18n.js'` 这类裸模块说明符带不上查询串。
升级后管理员可能拿到旧 settings.js（看不到新开关），甚至"新 settings.js + 旧 i18n.js"
的半旧状态 —— t() 对未知键回退成返回键名本身，界面会直接显示
`settings.field.logBodiesEnabled` 这种原始 i18n 键。
"""

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "/app/settings.js",
        "/app/i18n.js",
        "/app/app.js",
        "/app/base.css",
        "/components/section-settings.html",
        "/index.html",
        "/login.html",
        # 裸地址 "/" 才是管理员实际收藏/打开的入口。没有显式路由时它会落到 StaticFiles
        # 挂载（html=True），而 starlette 传进 get_response 的 path 是 "."，不匹配任何
        # 后缀 —— 于是唯一写着 ?v= 版本串的那份 HTML 反而没有 no-cache。
        "/",
    ],
)
def test_panel_assets_are_revalidated(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} 拿不到，测试的前提变了: {resp.status_code}"
    assert resp.headers.get("cache-control") == "no-cache", (
        f"{path} 没有 Cache-Control: no-cache，浏览器会按启发式缓存住旧版本"
    )


def test_revalidated_assets_still_serve_etag_for_cheap_304s(client):
    """no-cache 不等于不缓存：必须留着 ETag，回源校验才是 304 空响应而不是全量重传。"""
    resp = client.get("/app/settings.js")
    assert resp.headers.get("etag"), "ETag 没了，no-cache 会退化成每次全量重传"

    again = client.get("/app/settings.js", headers={"If-None-Match": resp.headers["etag"]})
    assert again.status_code == 304


def test_binary_assets_under_the_wrapped_mount_are_not_forced_to_revalidate(client):
    """只对易变的文本资源加 no-cache；图片这类大文件不该被拖下水。

    必须探 RevalidatingStaticFiles **挂载之内**的图片：/api-assets 走的是没包装过的
    普通 StaticFiles，它永远没有 Cache-Control，拿它当守卫等于什么都没测 —— 就算
    _REVALIDATE_SUFFIXES 被人加进 .png/.ico，它照样是绿的。
    """
    resp = client.get("/components/logo.png")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") is None, (
        "面板图片被拖进了 no-cache，每次加载都要多一次条件请求"
    )


def test_unwrapped_api_assets_mount_stays_untouched(client):
    """/api-assets 是另一个挂载，本改动不应波及它。"""
    resp = client.get("/api-assets/sponsor-qr.png")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") is None

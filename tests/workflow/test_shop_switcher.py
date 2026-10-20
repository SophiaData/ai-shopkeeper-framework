"""P2-11 多店铺 UI（顶栏店铺切换）。

## 为什么要有全局切换

此前只有「利润页」有店铺选择，其余页面隐含用默认店。于是会出现
**「页面上看的是 A 店、请求却打到默认店」的静默错配** —— 这类错误不会报错，
只会给出错误的数字，比直接失败更危险。

本 PR 把店铺选择提升到顶栏（全局），并用用例钉住：
- 单店时不显示下拉（避免无意义控件）
- 多店时显示下拉，且选中态跟随 `activeShop`
- 切换会重载数据（只换标签不换数据仍会错配）
- 选择持久化到 localStorage，但**不可用时不影响功能**
"""

from __future__ import annotations

from pathlib import Path

import pytest

MAIN_JS = Path(__file__).resolve().parents[2] / "apps" / "admin" / "src" / "main.js"


@pytest.fixture(scope="module")
def source() -> str:
    return MAIN_JS.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 源码契约（离线可测）
# --------------------------------------------------------------------------


def test_switcher_is_in_topbar(source: str):
    """切换器必须在**全局顶栏**里，而不是某个页面内。"""
    topbar_start = source.index('<header class="topbar">')
    topbar_end = source.index("</header>", topbar_start)
    assert "renderShopSwitcher()" in source[topbar_start:topbar_end]


def test_switcher_function_exists(source: str):
    assert "function renderShopSwitcher()" in source


def test_single_shop_has_no_select(source: str):
    """只有一家店时不该出现下拉 —— 无意义的控件会让人以为还有别的店。"""
    start = source.index("function renderShopSwitcher()")
    end = source.index("function shell(bodyHtml)", start)
    block = source[start:end]
    assert "shops.length <= 1" in block
    assert '<select id="shop-switch"' in block, "多店分支应有 select"


def test_switch_persists_and_reloads(source: str):
    start = source.index("const shopSwitch = document.getElementById")
    block = source[start : start + 900]
    assert "localStorage" in block, "应记住上次选择的店铺"
    assert "render()" in block, "切换后必须重载，否则数据仍是旧店的"
    # 旧实现调用了未定义的 refresh()，点击切店会抛 ReferenceError；
    # 这里同时钉住不能再回退到那个伪函数。
    assert "refresh()" not in block


def test_localstorage_failure_is_tolerated(source: str):
    """无痕模式下 localStorage 可能抛错 —— 不该让切换功能整体失效。"""
    start = source.index("const shopSwitch = document.getElementById")
    block = source[start : start + 900]
    assert "catch" in block


def test_shops_loaded_globally(source: str):
    """店铺列表在 shell 数据里加载 —— 每个页面都能用到。"""
    assert "async function loadShops()" in source
    shell_start = source.index("async function refreshShellData()")
    assert "loadShops()" in source[shell_start : shell_start + 700]


def test_shops_endpoint_failure_falls_back(source: str):
    """取不到店铺列表时退回单店，而不是让整页挂掉。"""
    start = source.index("async function loadShops()")
    # loadShops 之后紧跟的下一处函数定义即边界
    end = source.index("function healthToneClass", start)
    block = source[start:end]
    assert "catch" in block
    assert 'shop_id: "default"' in block


def test_active_shop_restored_from_storage(source: str):
    start = source.index("async function loadShops()")
    end = source.index("function healthToneClass", start)
    block = source[start:end]
    assert "getItem" in block
    assert "known" in block, "存的是已不存在的店时要回退，不能留空"


# --------------------------------------------------------------------------
# 后端：店铺列表形状
# --------------------------------------------------------------------------


def test_shops_endpoint_shape():
    """前端依赖 `shops[].shop_id` —— 钉住这个字段名。"""
    import inspect

    from shopkeeper.workflow.shops import shop_public_view

    src = inspect.getsource(shop_public_view)
    assert '"shop_id"' in src


def test_shops_list_never_exposes_secrets():
    """店铺列表不得带密钥 —— 它会被渲染到前端。"""
    import inspect

    from shopkeeper.workflow.shops import shop_public_view

    src = inspect.getsource(shop_public_view).lower()
    for forbidden in ("access_token", "app_secret", "app_key", "shop_cipher\"".lower()):
        assert f'"{forbidden}":' not in src, f"店铺公开视图泄漏了 {forbidden}"


def test_shop_view_reports_credential_state():
    """只报「是否已配置」，不报值。"""
    from shopkeeper.workflow.shops import shop_public_view

    view = shop_public_view("default", {})
    assert view["shop_id"] == "default"
    assert view["credentials_configured"] is False
    assert set(view) == {
        "shop_id",
        "credentials_configured",
        "has_shop_cipher",
        "region",
        "is_default",
    }

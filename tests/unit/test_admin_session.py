from pathlib import Path


def test_admin_session_uses_24_hour_inactivity_timeout():
    root = Path(__file__).resolve().parents[2]
    auth = (root / "static" / "app" / "auth.js").read_text(encoding="utf-8")

    assert "const SESSION_TIMEOUT = 24 * 60 * 60 * 1000" in auth


def test_dashboard_has_no_remote_qr_promotion():
    root = Path(__file__).resolve().parents[2]
    dashboard = (root / "static" / "components" / "section-dashboard.html").read_text(encoding="utf-8")
    app = (root / "static" / "app" / "app.js").read_text(encoding="utf-8")

    assert "qrCardsContainer" not in dashboard
    assert "QR_REMOTE_BASE" not in app
    assert "qr-config.json" not in app


def test_flow_accounts_show_their_email_and_proxy_in_account_management():
    root = Path(__file__).resolve().parents[2]
    app = (root / "static" / "app" / "app.js").read_text(encoding="utf-8")

    assert "Flow 邮箱" in app
    assert "account.flow_email || '未提供'" in app
    assert "同步代理" in app
    assert "account.flow_proxy_node_name" in app
    assert "account.flow_proxy_bound" in app

"""
Gemini Cookie Refresher - Playwright 自动续期

通过真实 Chromium 浏览器定时访问 Gemini 页面，
触发 Google 前端 JS 自动续期 __Secure-1PSIDTS，
然后将最新 Cookie 写入共享文件并通知 gemini2api 热更新。
"""
import os
import sys
import json
import time
import re
import hashlib
import threading
import signal
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests as http_requests
from playwright.sync_api import sync_playwright

DATA_DIR = "/app/data"
STATE_DIR = os.path.join(DATA_DIR, "browser_states")
COOKIES_OUTPUT = os.path.join(DATA_DIR, "refreshed_cookies.json")
GEMINI2API_URL = os.environ.get("GEMINI2API_URL", "http://gemini2api:5918")
API_KEY = os.environ.get("API_KEY", "")
# /admin/* 路由由 verify_admin_key 鉴权：ADMIN_API_KEY 设置时用它，否则回退 API_KEY，
# 与服务端 auth.verify_admin_key 的优先级保持一致（否则通知恒 401）。
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
ADMIN_KEY = ADMIN_API_KEY or API_KEY
# 续期周期：优先读文档/.env 里的 REFRESH_INTERVAL（单位=分钟，与 README/主服务一致），
# 兼容旧的 REFRESH_INTERVAL_SECONDS（单位=秒）；最终统一换算成秒。
_interval_seconds = os.environ.get("REFRESH_INTERVAL_SECONDS")
if _interval_seconds is not None:
    REFRESH_INTERVAL = int(_interval_seconds)
else:
    REFRESH_INTERVAL = int(float(os.environ.get("REFRESH_INTERVAL", "8")) * 60)
SINGLE_RUN = os.environ.get("SINGLE_RUN", "false").lower() == "true"
REFRESHER_MODE = os.environ.get("REFRESHER_MODE", "scheduled").lower()
REFRESHER_PORT = int(os.environ.get("REFRESHER_PORT", "6080"))
PROFILE_DIR = os.path.join(DATA_DIR, "browser_profiles")
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_profile_lock = threading.Lock()
_manual_lock = threading.Lock()
_manual_process = None
_manual_account_id = None


def load_accounts():
    accounts_file = os.path.join(DATA_DIR, "refresher_accounts.json")
    if os.path.exists(accounts_file):
        with open(accounts_file, "r") as f:
            return json.load(f)

    psid = os.environ.get("GEMINI_PSID", "")
    psidts = os.environ.get("GEMINI_PSIDTS", "")
    if psid:
        return [{"id": "account-0", "psid": psid, "psidts": psidts, "label": "Default"}]
    return []


def ensure_state_dir(account_id):
    path = os.path.join(STATE_DIR, account_id)
    os.makedirs(path, exist_ok=True)
    return path


def _state_psid(state_file):
    """读取 state.json 中当前的 __Secure-1PSID，用于判断配置是否已轮换。"""
    try:
        with open(state_file, "r") as f:
            state = json.load(f)
        for c in state.get("cookies", []):
            if c.get("name") == "__Secure-1PSID":
                return c.get("value")
    except Exception:
        return None
    return None


def inject_cookies_to_state(state_dir, psid, psidts):
    state_file = os.path.join(state_dir, "state.json")
    cookies = [
        {"name": "__Secure-1PSID", "value": psid, "domain": ".google.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "None"},
    ]
    # 仅在 psidts 非空时写入：present-but-empty 的 __Secure-1PSIDTS 与“缺失”语义不同，
    # 空值会污染浏览器状态、阻止 Google 前端 JS 重新签发 token（与主服务 cookie_jar 的处理一致）。
    if psidts:
        cookies.append({"name": "__Secure-1PSIDTS", "value": psidts, "domain": ".google.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "None"})
    state = {"cookies": cookies, "origins": []}
    with open(state_file, "w") as f:
        json.dump(state, f)
    print(f"  [init] Injected cookies from config into state")


def refresh_account(browser, account):
    account_id = account["id"]
    label = account.get("label", account_id)
    state_dir = ensure_state_dir(account_id)
    state_file = os.path.join(state_dir, "state.json")

    # 首次运行注入，或当 refresher_accounts.json 中的源 PSID 已被运营者轮换
    # （与 state.json 中已持久化的 PSID 不一致）时重新注入——否则旋转后的凭据被永久忽略，
    # 过期账号无法通过编辑配置恢复。
    if not os.path.exists(state_file) or _state_psid(state_file) != account["psid"]:
        inject_cookies_to_state(state_dir, account["psid"], account.get("psidts", ""))

    print(f"  [{label}] Opening browser context...")
    context = browser.new_context(
        storage_state=state_file,
        locale="en-US",
        timezone_id="America/New_York",
    )
    page = context.new_page()

    try:
        page.goto("https://gemini.google.com/app", timeout=90000, wait_until="domcontentloaded")
        time.sleep(15)

        cookies = context.cookies()
        psid = next((c["value"] for c in cookies if c["name"] == "__Secure-1PSID"), None)
        psidts = next((c["value"] for c in cookies if c["name"] == "__Secure-1PSIDTS"), None)

        if psid and psidts:
            context.storage_state(path=state_file)
            print(f"  [{label}] OK - PSIDTS: {psidts[:20]}...")
            return {"id": account_id, "label": label, "psid": psid, "psidts": psidts, "status": "active", "updated_at": time.time()}
        else:
            print(f"  [{label}] FAILED - Cookie not found, may need re-login")
            return {"id": account_id, "label": label, "status": "expired", "updated_at": time.time()}
    except Exception as e:
        print(f"  [{label}] ERROR - {e}")
        return {"id": account_id, "label": label, "status": "error", "error": str(e), "updated_at": time.time()}
    finally:
        context.close()


def notify_gemini2api(account_id, psid, psidts):
    headers = {"Content-Type": "application/json"}
    # /admin/* 用 ADMIN_KEY（ADMIN_API_KEY 优先，否则回退 API_KEY）。
    if ADMIN_KEY:
        headers["Authorization"] = f"Bearer {ADMIN_KEY}"

    # 优先按账号 ID 精确更新（多账号隔离）
    try:
        resp = http_requests.put(
            f"{GEMINI2API_URL}/admin/accounts/{account_id}/cookies",
            json={"psid": psid, "psidts": psidts},
            headers=headers,
            timeout=10
        )
        if resp.status_code == 200:
            print(f"  [notify] {account_id} cookies updated via PUT")
            return True
        elif resp.status_code == 404:
            # 账号不存在，fallback 到全局 reload
            resp2 = http_requests.post(
                f"{GEMINI2API_URL}/admin/reload-cookies",
                json={"psid": psid, "psidts": psidts},
                headers=headers,
                timeout=10
            )
            if resp2.status_code == 200:
                print(f"  [notify] cookies reloaded via POST (account not in pool)")
                return True
            elif resp2.status_code == 401:
                print(f"  [notify] auth rejected (401) — set ADMIN_API_KEY/API_KEY to match the server's admin key")
                return False
            else:
                print(f"  [notify] reload failed: {resp2.status_code} {resp2.text[:100]}")
                return False
        elif resp.status_code == 401:
            print(f"  [notify] auth rejected (401) — set ADMIN_API_KEY/API_KEY to match the server's admin key")
            return False
        else:
            print(f"  [notify] PUT failed: {resp.status_code} {resp.text[:100]}")
            return False
    except Exception as e:
        print(f"  [notify] Failed to reach gemini2api: {e}")
        return False


def refresh_all():
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"[{ts}] Starting cookie refresh cycle...")
    print(f"{'='*50}")

    accounts = load_accounts()
    if not accounts:
        print("  [ERROR] No accounts configured!")
        print("  Set GEMINI_PSID/GEMINI_PSIDTS env vars or create data/refresher_accounts.json")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--single-process",
                "--no-zygote",
                "--disable-extensions",
            ]
        )

        for i, account in enumerate(accounts):
            result = refresh_account(browser, account)
            results.append(result)
            if i < len(accounts) - 1:
                time.sleep(5)

        browser.close()

    with open(COOKIES_OUTPUT, "w") as f:
        json.dump(results, f, indent=2)

    active = [r for r in results if r.get("status") == "active"]
    for acc in active:
        notify_gemini2api(acc["id"], acc["psid"], acc["psidts"])

    print(f"\n  Summary: {len(active)}/{len(results)} accounts active")


def _validate_profile_id(account_id):
    if not PROFILE_ID_RE.fullmatch(account_id):
        raise ValueError("Invalid account_id")


def _manual_running_locked():
    global _manual_process, _manual_account_id
    if _manual_process and _manual_process.poll() is None:
        return True
    _manual_process = None
    _manual_account_id = None
    return False


def _stop_manual_locked():
    global _manual_process, _manual_account_id
    if _manual_process and _manual_process.poll() is None:
        try:
            os.killpg(_manual_process.pid, signal.SIGTERM)
            _manual_process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(_manual_process.pid, signal.SIGKILL)
                _manual_process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                pass
    _manual_process = None
    _manual_account_id = None


def _extract_profile(account_id, psid="", psidts=""):
    """Read a profile's Gemini cookies; optionally seed the two auth cookies first."""
    _validate_profile_id(account_id)

    profile_dir = os.path.join(PROFILE_DIR, account_id)
    meta_path = os.path.join(profile_dir, "gemini2api-profile.json")
    os.makedirs(profile_dir, exist_ok=True)
    with _profile_lock, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-extensions",
            ],
        )
        try:
            if psid:
                # Automatic refresh starts from only the two required auth
                # cookies. Manual capture leaves the user's logged-in profile intact.
                context.clear_cookies()
                cookies = [{
                    "name": "__Secure-1PSID", "value": psid, "domain": ".google.com",
                    "path": "/", "secure": True, "httpOnly": True, "sameSite": "None",
                }]
                if psidts:
                    cookies.append({
                        "name": "__Secure-1PSIDTS", "value": psidts, "domain": ".google.com",
                        "path": "/", "secure": True, "httpOnly": True, "sameSite": "None",
                    })
                context.add_cookies(cookies)

            page = context.pages[0] if context.pages else context.new_page()
            body = ""
            for attempt in range(2):
                if attempt == 0:
                    page.goto("https://gemini.google.com/app?hl=en", timeout=90000, wait_until="domcontentloaded")
                else:
                    page.reload(timeout=90000, wait_until="domcontentloaded")
                page.wait_for_timeout(10000)
                body = page.content()
                if '"SNlM0e":"' in body:
                    break
            cookies = context.cookies()
            fresh_psid = next((c["value"] for c in cookies if c["name"] == "__Secure-1PSID"), "")
            fresh_psidts = next((c["value"] for c in cookies if c["name"] == "__Secure-1PSIDTS"), "")
            if not fresh_psid or not fresh_psidts or '"SNlM0e":"' not in body:
                raise RuntimeError("Browser profile is not signed in to Gemini")
            updated_at = datetime.now(timezone.utc).isoformat()
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({
                    "profile_id": account_id,
                    "psid_hash": hashlib.sha256(fresh_psid.encode()).hexdigest(),
                    "updated_at": updated_at,
                }, f, ensure_ascii=False, indent=2)
            return {
                "success": True,
                "profile_id": account_id,
                "psid": fresh_psid,
                "psidts": fresh_psidts,
                "updated_at": updated_at,
            }
        finally:
            context.close()


def refresh_profile(account):
    """Use one persistent Chromium user-data directory per account."""
    account_id = str(account.get("account_id", ""))
    psid = str(account.get("psid", "")).strip()
    psidts = str(account.get("psidts", "")).strip()
    _validate_profile_id(account_id)
    if not psid:
        raise ValueError("Missing __Secure-1PSID")
    with _manual_lock:
        if _manual_running_locked():
            raise RuntimeError("Manual browser session is active; finish it before automatic refresh")
    return _extract_profile(account_id, psid, psidts)


def _clear_stale_chromium_locks(profile_dir):
    """Remove only Chromium's per-profile singleton markers after its process is stopped."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(profile_dir, name))
        except FileNotFoundError:
            pass


def open_manual_profile(account):
    global _manual_process, _manual_account_id
    account_id = str(account.get("account_id", ""))
    _validate_profile_id(account_id)
    profile_dir = os.path.join(PROFILE_DIR, account_id)
    os.makedirs(profile_dir, exist_ok=True)
    with _manual_lock, _profile_lock, sync_playwright() as p:
        _stop_manual_locked()
        _clear_stale_chromium_locks(profile_dir)
        env = {**os.environ, "DISPLAY": ":99"}
        _manual_process = subprocess.Popen([
            p.chromium.executable_path,
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--no-first-run",
            "--disable-default-apps",
            "--window-size=1400,860",
            f"--user-data-dir={profile_dir}",
            "https://gemini.google.com/app?hl=zh-CN",
        ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        _manual_account_id = account_id
        time.sleep(1)
        if _manual_process.poll() is not None:
            _stop_manual_locked()
            raise RuntimeError("Interactive Chromium failed to start")
    return {"success": True, "account_id": account_id, "viewer_path": f"/session_browser.html?account_id={account_id}"}


def capture_manual_profile(account):
    account_id = str(account.get("account_id", ""))
    _validate_profile_id(account_id)
    with _manual_lock:
        if not _manual_running_locked() or _manual_account_id != account_id:
            raise RuntimeError("This account has no active manual browser session")
        _stop_manual_locked()
    return _extract_profile(account_id)


def close_manual_profile(account):
    account_id = str(account.get("account_id", ""))
    _validate_profile_id(account_id)
    with _manual_lock:
        if _manual_running_locked() and _manual_account_id == account_id:
            _stop_manual_locked()
    return {"success": True, "account_id": account_id}


def manual_status():
    with _manual_lock:
        return {"active": _manual_running_locked(), "account_id": _manual_account_id}


class RefreshHandler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "service": "browser-refresher"})
        elif self.path == "/manual/status":
            expected = ADMIN_KEY
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if expected and supplied != expected:
                self._json(401, {"error": "Unauthorized"})
            else:
                self._json(200, manual_status())
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        handlers = {
            "/refresh": refresh_profile,
            "/manual/open": open_manual_profile,
            "/manual/capture": capture_manual_profile,
            "/manual/close": close_manual_profile,
        }
        if self.path not in handlers:
            self._json(404, {"error": "Not found"})
            return
        expected = ADMIN_KEY
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if expected and supplied != expected:
            self._json(401, {"error": "Unauthorized"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(size))
            self._json(200, handlers[self.path](payload))
        except ValueError as e:
            self._json(400, {"error": str(e)[:300]})
        except Exception as e:
            self._json(503, {"error": str(e)[:300]})

    def log_message(self, fmt, *args):
        print("[browser-refresher] " + fmt % args)


def serve():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", REFRESHER_PORT), RefreshHandler)
    print(f"Built-in browser refresher listening on :{REFRESHER_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    if SINGLE_RUN:
        refresh_all()
        print("\n[Single run mode] Done, exiting.")
        sys.exit(0)
    elif REFRESHER_MODE == "server":
        serve()

    print(f"Gemini Cookie Refresher started (interval: {REFRESH_INTERVAL}s; set REFRESH_INTERVAL in minutes)")
    while True:
        try:
            refresh_all()
        except Exception as e:
            print(f"[FATAL] {e}")
        print(f"\nSleeping {REFRESH_INTERVAL}s until next refresh...")
        time.sleep(REFRESH_INTERVAL)

<div align="center">

<img src="../logo.png" width="128" height="128" alt="Gemini2API">

<h1>Gemini2API</h1>
<h3>Lightweight Gemini Web Reverse Proxy</h3>
<p>Single codebase compatible with OpenAI / Claude / Gemini SDKs, pure async architecture, zero official keys, Docker quick deployment.</p>

<p>
  <img src="https://img.shields.io/badge/Python-3.12+-blue?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/curl__cffi-Chrome%20TLS-ff6b35?style=flat-square&logo=google-chrome&logoColor=white" alt="curl_cffi">
  <img src="https://img.shields.io/badge/Docker-20.10+-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Chrome%20%7C%20Edge-Latest-4285F4?style=flat-square&logo=googlechrome&logoColor=white" alt="Browser">
  <img src="https://img.shields.io/badge/License-Non--Commercial-red?style=flat-square" alt="License">
</p>

<p>
  <a href="#-recent-updates">Recent Updates</a> &bull;
  <a href="#-core-features">Core Features</a> &bull;
  <a href="#-system-requirements">System Requirements</a> &bull;
  <a href="#-quick-deployment">Quick Deployment</a> &bull;
  <a href="#-integration-examples">Integration Examples</a> &bull;
  <a href="#-api-endpoints">API Endpoints</a> &bull;
  <a href="#-configuration">Configuration</a> &bull;
  <a href="#-important-notes">Important Notes</a> &bull;
  <a href="#-roadmap">Roadmap</a>
</p>

<p>
  📖 Documentation: <a href="../zh-CN/README.md">简体中文</a> | <a href="../zh-TW/README.md">繁體中文</a> | English | <a href="../ja/README.md">日本語</a> | <a href="../ko/README.md">한국어</a>
</p>

<br>

<a href="https://github.com/xwteam/gemini2api/issues"><img src="https://img.shields.io/github/issues/xwteam/gemini2api?style=flat-square" alt="Issues"></a>
<a href="https://github.com/xwteam/gemini2api/stargazers"><img src="https://img.shields.io/github/stars/xwteam/gemini2api?style=flat-square" alt="Stars"></a>

</div>

---

> [!NOTE]
> This project is for research and learning purposes only. Please use it responsibly and do not use it for any commercial purposes.

> [!WARNING]
> This project is not affiliated with Google. It uses reverse-engineered browser cookies to access Gemini Web, which may violate Google's Terms of Service. Use at your own risk. The author is not responsible for any account penalties or data loss.

> [!TIP]
> It is recommended to use Gemini Pro or higher subscription for complete model access and stable experience.

> [!IMPORTANT]
> Due to Google's risk control policies, cookie sessions typically expire after about 2 hours. We haven't found a perfect long-term solution yet. If you have experience or ideas on this, please share them via [Issue](https://github.com/xwteam/gemini2api/issues) or PR. We look forward to community wisdom.

---

## 📝 Recent Updates

> Showing the 10 most recent updates. For the complete changelog, see [CHANGELOG.md](../../CHANGELOG.md).

| Date | Update |
|------|--------|
| 2026-08-28 14:20:00 | v1.6.33 - 🔌 Fix Claude Code connectivity (issue #10): `system` now accepts the text-block array form (no more 422); `tool_use`/`tool_result` blocks are no longer dropped (agent tool loop works); Anthropic streaming now emits the standard `event:` + `data:` frames; plus keepalive on the Claude buffered stream and prompt account-slot release on client disconnect |
| 2026-08-14 22:50:00 | v1.6.32 - 🧠 Stream reasoning frame-by-frame: native Gemini's thinking now streams incrementally as reasoning_content during generation (/v1/chat/completions), so it appears before the answer with a typewriter effect, fixing the panel's 'answer before thinking'; the final frame still carries full thoughts as a safety net; zero regression for non-thinking/normal chat |
| 2026-08-14 22:40:00 | v1.6.31 - 🌊 Fix intermittent streaming disconnects: send keepalive heartbeats during the silent gap while the model generates, across all four streaming APIs (/v1/chat/completions, /v1/responses, /v1/messages, native Gemini streamGenerateContent), so long responses aren't cut by cross-border/gateway idle timeouts; also reworded the misleading panel network-error copy |
| 2026-08-14 22:30:00 | v1.6.30 - 🧠 Playground 'Thinking' toggle: enable Gemini extended thinking from the model-testing panel; the reasoning shows as a collapsible block above the answer |
| 2026-08-14 21:00:00 | v1.6.29 - 🧠 Native Gemini extended thinking: send `reasoning_effort` to enable thinking on pro/flash/flash-lite, returned as reasoning_content; default-on, one-click disable, auto-fallback so normal chat is unaffected; also fixed the flash-lite free-tier model id |
| 2026-07-30 21:55:00 | v1.6.28 - 🆕 New gemini-flash-lite model: exposes Gemini's lightest Flash-Lite tier (3.5 Flash-Lite), so there's still a usable model when Pro/Flash are rate-limited (mapped per-account to the real model behind the fixed public name) |
| 2026-07-25 09:50:00 | v1.6.27 - 🎨 Admin panel brand logo & favicon: replaced the top-left icon with a custom brand logo image (compressed to 128×128, ~16KB, ~97% smaller than the original); added a browser-tab favicon to the admin panel and login page (same logo) |
| 2026-07-07 12:48:37 | v1.6.26 - 🔌 New OpenAI Responses API support (`/v1/responses` or `/openai/v1/responses`): lets clients that require the newer Responses protocol (e.g. Codex CLI, which dropped Chat Completions support in Feb 2026) work with gemini2api — text chat, streaming, and function/tool calling, for both Gemini models and third-party models configured in API Management; streaming events strictly follow the official protocol order (fixing two terminal events a known reference implementation omits: `response.output_text.done` / `response.function_call_arguments.done`); no server-side multi-turn state — `previous_response_id` returns a clear error instead of silently faking continuity, since clients like Codex CLI resend the full conversation history themselves |
| 2026-06-23 00:00:00 | v1.6.25 - 🎚️ Gemini fallback toggle in API Management: one-click enable/disable of the Gemini→third-party fallback chain, takes effect instantly and persists (previously required editing .env and restarting); the toggle only controls fallback — third-party models are always reachable directly and always listed in /v1/models |
| 2026-06-22 20:06:08 | v1.6.24 - 🧩 Custom Gem support: new "Gem Management" page in the admin panel lets you list / create / edit / delete your own custom Gems; expose any Gem as a model name — any OpenAI-compatible client calling that model name will converse using that Gem's persona; each Gem is bound to its owner account (calls go only to the bound account, not round-robined); deleting a Gem automatically removes its model-name mapping |

---

## 🌟 Core Features

> 📖 Detailed usage guide: [USAGE.md](USAGE.md)

### 🔌 Triple Protocol Compatibility

- Single service provides OpenAI, Claude, and Gemini SDK formats simultaneously
- SSE streaming (OpenAI / Claude) + Chunked JSON (Gemini)
- Function calling supported across all three formats
- Deep Research multi-step research capability

### 🔐 Security & Authentication

- Auto-generated API Keys (`sk-` prefix + 32 random characters)
- Supports both `Authorization: Bearer` and `x-api-key` authentication
- Auto-generated on first deployment, customizable by users

### 🔄 Multi-Account Load Balancing & Cookie Self-Healing

- **Multi-account load balancing**: Supports round-robin and failover strategies
- Per-account concurrency control prevents single account overload
- Automatic health marking for failed accounts, auto-skip unhealthy ones
- Background cookie rotation for seamless renewal
- Hot-update Cookie API without container restart
- Dynamic account add/remove via API
- Health check history for web panel data support

### 🛡 Anti-Detection & Protocol Spoofing

- **TLS fingerprint consistency**: UA, Sec-Ch-Ua, curl_cffi impersonate always synchronized (Chrome 124)
- **Dynamic request headers**: Arranged in Chrome's real order, dynamically adjust Sec-Fetch-* based on request type
- **Complete cookie persistence**: Auto-capture all response cookies and persist to disk across restarts
- **Cookie domain isolation**: Clear session cookies before each request to prevent cross-domain conflicts
- **Chrome version auto-sync**: Poll Google version API every 24 hours, auto-update fingerprint on new version
- **Request time jitter**: Simulate human operation intervals (navigation 200-800ms / API 50-300ms / cookie rotation 1-3s)
- **Version fallback strategy**: Auto-use nearest available version when curl_cffi doesn't support latest Chrome

### 🖥 Web Management Panel

- Chinese visual management interface with API Key authentication
- Top-right control bar: theme toggle, service restart, logout
- Dashboard: real-time uptime counter, QR code cards (image zoom support), system info (version/Python/OS/memory/CPU/PID/mode), config management, account status overview, available models list
- **Hot-update resources**: `api/` directory volume mount, QR code images and text config changes take effect on page refresh without container rebuild
- Account management: add/delete accounts, update individual cookies, health checks
- **Settings page**: visual runtime config management (performance, rate limiting, health checks, account management), changes take effect immediately
- **Model mapping**: map request model names to actual models (e.g., gpt-4o → gemini-2.5-pro)
- **API Key management**: centralized third-party model API Key management (OpenAI/Anthropic/Gemini/OpenRouter/custom), import/export support
- Playground: online API testing
- Real-time logs: structured table display, direction filter, text search, pagination (15 per page), JSON detail panel, disk persistence (survives restart)
- Dark/light theme toggle, responsive mobile adaptation

### 🔀 Unified Forwarding Engine

- Auto-forward requests for models not in Gemini Web's available list to matching Provider from API Key pool
- Direct OpenAI-compatible format forwarding (including streaming), bidirectional Anthropic conversion
- `/openai/v1/models` auto-aggregates Gemini Web models + third-party models from API Key pool
- Single interface, single key to call all major models
- Third-party auto-fallback (`FALLBACK_ENABLED`, off by default): when any Gemini model errors or returns an empty response, automatically retry natively with a third-party model from the API Key pool — transparent to the client, still using just one model name; by default automatically uses all "chat-capable" third-party models in the pool (excludes non-chat models such as image/video), random round-robin, switching to the next on failure; `FALLBACK_MODELS` optionally specifies them precisely

### ⚡ High-Performance Architecture

- Python asyncio + curl_cffi, fully non-blocking pipeline
- Chrome TLS fingerprint spoofing + auto version tracking, significantly extended session lifetime
- Pydantic strong type validation, automatic request parameter validation
- Modular design, independent routing files for each API format
- Automatic retry with exponential backoff

---

## 📋 System Requirements

| Dependency | Version | Notes |
|-----------|---------|-------|
| Python | 3.12+ | Recommended 3.12, older versions untested |
| Docker | 20.10+ | Optional, Docker deployment recommended |
| Google Account | — | Must have normal access to [gemini.google.com](https://gemini.google.com) |
| Browser | Chrome / Edge | For cookie extraction (deployment only) |

> [!TIP]
> Docker deployment requires no local Python installation, just Docker and valid cookies.

---

## ⚡ Quick Deployment

> 📖 Detailed deployment guide: [DEPLOY.md](DEPLOY.md)

> **Prerequisite**: You need a Google account with normal Gemini access.

### 1. Get Cookies

1. Open Chrome or Edge browser and visit [gemini.google.com](https://gemini.google.com)
2. Log in with your Google account and verify Gemini works normally
3. Press `F12` to open Developer Tools
4. Click the **Application** tab at the top
5. In the left sidebar, find **Cookies** -> click `https://gemini.google.com`
6. Find these two values in the cookie list:

| Cookie Name | Description |
|-------------|-------------|
| `__Secure-1PSID` | Long string starting with `g.`, typically dozens of characters |
| `__Secure-1PSIDTS` | Shorter string |

7. Recommended to operate in incognito mode, close the window immediately after getting values to avoid cookie rotation issues

> [!TIP]
> Search for `__Secure-1P` in the search box for quick filtering. Double-click the Value column to copy the full value.

> [!WARNING]
> Cookies expire over time. If the service suddenly stops working, check if cookies have expired first.

### 2. Docker Deployment

```bash
# Clone repository
git clone https://github.com/xwteam/gemini2api.git
cd gemini2api

# Create environment file
cp .env.example .env
```

Edit `.env` file and add your cookies:

```env
GEMINI_PSID=g.a000xxx...(paste your full __Secure-1PSID value)
GEMINI_PSIDTS=sidts-xxx...(paste your full __Secure-1PSIDTS value)
```

> [!IMPORTANT]
> Important notes:
> - Values don't need quotes
> - No extra spaces or newlines
> - Ensure you copy the complete value, don't miss the end

Start the service:

```bash
docker compose up -d
```

Check logs to confirm successful startup:

```bash
docker compose logs -f
# "Account pool ready: 1/1 active" means account pool is ready
# "SNlM0e not found" means cookie is invalid, need to get new one
```

### Multi-Account Configuration (Optional)

To use multiple Google accounts for load balancing, create `accounts.json`:

```json
{
  "accounts": [
    {
      "id": "account-0",
      "psid": "g.a000xxx...",
      "psidts": "sidts-xxx...",
      "label": "Main Account"
    },
    {
      "id": "account-1",
      "psid": "g.a000yyy...",
      "psidts": "sidts-yyy...",
      "label": "Backup Account"
    }
  ]
}
```

> [!TIP]
> Without `accounts.json`, the service automatically uses single-account mode from `.env`. You can also dynamically add accounts via `POST /admin/accounts` API at runtime.

### Cookie Auto-Keep-Alive

gemini2api has built-in cookie auto-rotation: refresh `__Secure-1PSIDTS` every 5 minutes via Google RotateCookies API, combined with batchexecute heartbeat to simulate browser activity and extend session lifetime.

To manually update cookies, use the Web panel's "Account Management" → "Update Cookie" without restarting the service.

> [!NOTE]
> Cookie lifetime is affected by Google's risk control policies. Datacenter IPs typically last several hours. If cookies expire frequently, consider using residential IPs or adding more accounts for rotation.

### 3. Verification

```bash
# Health check
curl http://localhost:5918/health
# {"status":"ok","service":"gemini2api"}

# View available models (requires API Key, check logs on first startup)
curl http://localhost:5918/openai/v1/models \
  -H "Authorization: Bearer sk-your-api-key"

# Send test request
curl -X POST http://localhost:5918/openai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-api-key" \
  -d '{"model":"gemini-2.0-flash","messages":[{"role":"user","content":"hi"}]}'
```

Seeing AI response text means deployment succeeded. If you get 401, check your API Key.

---

## 🧪 Integration Examples

> [!NOTE]
> All API requests require an API Key. Two authentication methods supported:
> - `Authorization: Bearer sk-xxx` (recommended, compatible with OpenAI/Claude SDKs)
> - `x-api-key: sk-xxx`
>
> API Key is auto-generated on first startup and written to `.env`, visible in logs or manually editable.

<details>
<summary><b>OpenAI SDK (Python)</b></summary>

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-your-api-key",
    base_url="http://localhost:5918/openai/v1"
)

for chunk in client.chat.completions.create(
    model="gemini-2.0-flash",
    messages=[{"role": "user", "content": "Explain relativity in three sentences"}],
    stream=True
):
    print(chunk.choices[0].delta.content or "", end="")
```

</details>

<details>
<summary><b>Claude SDK (Python)</b></summary>

```python
import anthropic

client = anthropic.Anthropic(
    api_key="sk-your-api-key",
    base_url="http://localhost:5918/claude"
)

msg = client.messages.create(
    model="gemini-2.0-flash",
    max_tokens=4096,
    messages=[{"role": "user", "content": "Write a Python quicksort implementation"}]
)
print(msg.content[0].text)
```

</details>

<details>
<summary><b>cURL</b></summary>

```bash
# Non-streaming request
curl -X POST http://localhost:5918/openai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-api-key" \
  -d '{"model":"gemini-2.0-flash","messages":[{"role":"user","content":"Hi"}]}'

# Streaming request
curl -X POST http://localhost:5918/openai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-api-key" \
  -d '{"model":"gemini-2.0-flash","messages":[{"role":"user","content":"Hi"}],"stream":true}'
```

</details>

<details>
<summary><b>Function Calling</b></summary>

```python
response = client.chat.completions.create(
    model="gemini-2.0-flash",
    messages=[{"role": "user", "content": "What's the weather in Beijing today"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"]
            }
        }
    }]
)
```

</details>

---

## 📡 API Endpoints

> 📖 Detailed API documentation: [API.md](API.md)

### OpenAI Compatible (`/openai/v1`)

| Method | Endpoint | Function |
|--------|----------|----------|
| GET | `/models` | Available models list |
| POST | `/chat/completions` | Chat completion (streaming + tool calling) |
| POST | `/responses` | OpenAI Responses API (text / streaming / tool calling; used by newer clients like Codex CLI) |

### Claude Compatible (`/claude/v1`)

| Method | Endpoint | Function |
|--------|----------|----------|
| GET | `/models` | Models list |
| GET | `/models/{id}` | Model details |
| POST | `/messages` | Message generation (streaming + tool calling) |
| POST | `/messages/count_tokens` | Token count estimation |

### Gemini Native (`/gemini/v1beta`)

| Method | Endpoint | Function |
|--------|----------|----------|
| GET | `/models` | Models list |
| POST | `/models/{m}:generateContent` | Content generation |
| POST | `/models/{m}:streamGenerateContent` | Streaming generation (Chunked JSON) |

### Admin Interface (`/admin`)

> Full admin endpoints (with request/response examples) are in [API.md](API.md); the table below is the complete list.

| Method | Endpoint | Function |
|--------|----------|----------|
| GET | `/status` | Service status (account pool overview + rotation strategy) |
| GET | `/system-info` | System info (version/Python/OS/memory/CPU/PID/run mode) |
| GET | `/accounts` | All accounts list and status |
| POST | `/accounts` | Dynamically add new account |
| DELETE | `/accounts/{id}` | Remove account |
| GET | `/accounts/{id}/check` | Check single account status |
| GET | `/check-account` | Check all accounts status |
| POST | `/reload-cookies` | Hot-update cookies (no container restart) |
| PUT | `/accounts/{id}/cookies` | Update cookies for a specific account |
| GET | `/health-history` | Recent health check records |
| GET | `/usage-stats/summary` | Usage statistics summary |
| GET | `/usage-stats/history` | Historical trend data |
| GET | `/settings` | Get current editable config (grouped) |
| POST | `/settings` | Batch-update config (writes .env + hot-reloads memory) |
| GET | `/api-keys` | API Key list (keys masked) |
| GET | `/api-keys/catalog` | Provider catalog (built-in model lists) |
| POST | `/api-keys` | Add API Key |
| DELETE | `/api-keys/{id}` | Delete API Key |
| PATCH | `/api-keys/{id}/status` | Toggle Key status (enable/disable) |
| PATCH | `/api-keys/{id}/label` | Edit Key label |
| POST | `/api-keys/import` | Bulk-import Keys |
| GET | `/api-keys/export` | Export all Keys (masked by default, `?reveal=true` for plaintext) |
| POST | `/api-keys/batch-delete` | Bulk-delete |
| POST | `/api-keys/models` | Probe available models for a given Provider/base_url |
| GET | `/verify` | Verify API Key validity (used for login) |
| POST | `/restart` | Restart service (one-click restart from top-right of panel) |
| GET | `/check-update` | Check whether a new version is available |
| POST | `/update` | Trigger update to the latest version |
| GET | `/logs` | Structured log pagination query |
| GET | `/logs/state` | Log recording state |
| POST | `/logs/state` | Update log recording state |
| POST | `/logs/clear` | Clear logs |
| GET | `/logs/{id}` | Single log detail |
| GET | `/model-mapping` | Get all model mappings |
| POST | `/model-mapping` | Add/update model mapping |
| DELETE | `/model-mapping/{alias}` | Delete model mapping |
| GET | `/web-chats` | List sessions accumulated on the Gemini web side per account (read-only) |
| POST | `/cleanup-web-chats` | Manually trigger cleanup of expired web sessions (runs asynchronously in background) |

---

## ⚙ Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GEMINI_PSID` | ✅ | — | Browser `__Secure-1PSID` |
| `GEMINI_PSIDTS` | ✅ | — | Browser `__Secure-1PSIDTS` |
| `API_KEY` | ❌ | Auto-generated | API access key (`sk-` prefix, auto-generated on first startup if empty) |
| `REFRESH_INTERVAL` | ❌ | `5` | Cookie refresh interval (minutes) |
| `MAX_RETRIES` | ❌ | `3` | Retry count on failure (exponential backoff) |
| `PORT` | ❌ | `5918` | Service port |
| `LOG_LEVEL` | ❌ | `info` | Log level (debug/info/warning/error) |
| `RATE_LIMIT_ENABLED` | ❌ | `false` | Enable rate limiting |
| `RATE_LIMIT_WINDOW` | ❌ | `60` | Rate limit window (seconds) |
| `RATE_LIMIT_MAX` | ❌ | `10` | Max requests per window |
| `HEALTH_CHECK_ENABLED` | ❌ | `true` | Enable scheduled account health checks |
| `HEALTH_CHECK_INTERVAL` | ❌ | `5` | Check interval (minutes) |
| `ACCOUNTS_FILE` | ❌ | `accounts.json` | Multi-account config file path (falls back to single-account mode from env vars if absent) |
| `ROTATION_STRATEGY` | ❌ | `round-robin` | Rotation strategy: `round-robin` / `failover` |
| `MAX_CONCURRENT_PER_ACCOUNT` | ❌ | `8` | Max concurrent requests per account |
| `ACQUIRE_TIMEOUT` | ❌ | `60.0` | Max time (seconds) to queue for a free slot when at full concurrency before erroring |
| `SAME_ACCOUNT_5XX_RETRIES` | ❌ | `1` | Quick same-account retries on 5xx (no long backoff); failover to another account if still failing |
| `FAILOVER_COOLDOWN` | ❌ | `30.0` | Cooldown (seconds) for an account rate-limited by 5xx, during which it is not preferred |
| `FINGERPRINT_CONFIG_PATH` | ❌ | `data/fingerprint.json` | Fingerprint config file path |
| `VERSION_SYNC_ENABLED` | ❌ | `true` | Enable Chrome version auto-sync |
| `VERSION_SYNC_INTERVAL` | ❌ | `24` | Version sync interval (hours) |
| `JITTER_ENABLED` | ❌ | `true` | Enable request time jitter (simulate human behavior) |
| `USAGE_STATS_ENABLED` | ❌ | `true` | Enable usage statistics (time-series snapshots + persistence) |
| `USAGE_STATS_INTERVAL` | ❌ | `300` | Snapshot collection interval (seconds) |
| `USAGE_STATS_RETENTION_DAYS` | ❌ | `30` | Historical data retention (days) |
| `MODEL_WHITELIST` | ❌ | — | Model whitelist (comma-separated; empty = no filtering; when set, filters each `/models` list) |
| `CHAT_CLEANUP_ENABLED` | ❌ | `true` | Enable auto-cleanup of Gemini web sessions |
| `CHAT_CLEANUP_KEEP_HOURS` | ❌ | `24.0` | Web session retention (hours); older ones are cleaned up |
| `CHAT_CLEANUP_INTERVAL_HOURS` | ❌ | `6.0` | Auto-cleanup task run interval (hours) |
| `CHAT_CLEANUP_SKIP_PINNED` | ❌ | `true` | Skip pinned sessions during cleanup |
| `ADMIN_API_KEY` | ❌ | — | Separate auth key for the admin panel / `/admin` (empty falls back to `API_KEY`) |
| `CORS_ALLOW_ORIGINS` | ❌ | `*` | CORS allowed origins (comma-separated; `*` means all) |
| `CORS_ALLOW_CREDENTIALS` | ❌ | `true` | Whether CORS allows credentials |
| `IMAGE_DOWNLOAD_SIZE_SUFFIX` | ❌ | `=s2048` | Generated-image download size suffix (`=s0` for full-resolution original) |
| `IMAGE_DOWNLOAD_TIMEOUT` | ❌ | `25.0` | Per-image download HTTP timeout (seconds) |
| `FALLBACK_ENABLED` | ❌ | `false` | Enable Gemini → third-party fallback: when any Gemini model (flash/pro/thinking) errors or returns an empty response, automatically retry natively with a third-party model from the API Key pool |
| `FALLBACK_MODELS` | ❌ | — | Fallback models (comma-separated, tried in order); empty = automatically use all "chat-capable" third-party models in the pool (excludes non-chat models such as image/video/audio/embedding by name) with random round-robin, switching to the next one whenever one fails (errors or empty) |

---

## ⚠ Important Notes

1. **Cookie Expiration**: Google cookies expire periodically (typically hours to days). The service has built-in auto-refresh, but if your account is logged out or password changed, you need new cookies.

2. **Streaming Output**: All API endpoints stream by default. When `stream: false`, the service still receives streaming data internally and returns complete JSON after collection.

3. **Model Availability**: Available models depend on your Google account permissions. Free and Gemini Advanced accounts see different models. The service auto-detects on startup.

4. **Request Frequency**: Even with rate limiting disabled (`RATE_LIMIT_ENABLED=false`), Google has its own limits. High-frequency requests may trigger CAPTCHAs or temporary bans. Control request frequency appropriately.

5. **Network Environment**: The deployment server must have direct access to `gemini.google.com`. Some regions may need proxy configuration.

---

## 🗺 Roadmap

- [x] OpenAI / Claude / Gemini triple format compatibility
- [x] Streaming responses + function calling
- [x] Deep Research multi-step research
- [x] Docker deployment
- [x] API Key authentication
- [x] Cookie hot-update API
- [x] Scheduled account health checks
- [x] Multi-account rotation (load balancing)
- [x] Web management panel
- [x] Anti-detection & protocol spoofing
- [x] Settings page (visual config management)
- [x] API Key management (third-party model keys)
- [x] Unified forwarding engine (single interface for all models)
- [x] Model mapping (alias → actual model)
- [x] Auto-cleanup of accumulated web sessions (periodically delete old sessions, keep pinned)
- [ ] Image/file upload support
- [x] [issues #2](https://github.com/xwteam/gemini2api/issues/2) Custom Gemini Gem support (panel list / create / update / delete + expose as a model name)
- [x] [issues #6](https://github.com/xwteam/gemini2api/issues/6) [#7](https://github.com/xwteam/gemini2api/issues/7) Native Gemini extended thinking (enable via `reasoning_effort`; reasoning streams frame-by-frame before the answer + "Thinking" toggle in the Playground panel + one-click off, never affects normal chat)
- [x] Gemini fallback toggle in API Management (instant on/off with persistence, no .env edit or restart needed)

---

## ☕ Support & Contribute

Find this helpful? Buy the author a coffee or join the WeChat group for support. For full details, see [SPONSORS.md](SPONSORS.md).

PRs and Issues welcome.

1. Fork this repository
2. Create a branch `git checkout -b feature/your-feature`
3. Commit code `git commit -m "feat: add something"`
4. Push and create a Pull Request

---

## 🙏 Acknowledgments

Thanks to everyone who submitted bug reports, logs, compatibility feedback, and feature suggestions through [Issues](https://github.com/xwteam/gemini2api/issues). Your feedback directly drove the development of Cookie persistence, multi-account rotation, model selection, multi-language support, and the Web panel.

---

## 📄 License

This project uses [Non-Commercial License](../../LICENSE):

- **Allowed**: Personal learning, research, self-hosted deployment
- **Prohibited**: Any commercial use including selling, reselling, paid proxies, commercial product integration

This project is not affiliated with Google. Users assume all risks and must comply with Google's Terms of Service.

---

<div align="center">
  <sub>Built with Python + FastAPI + curl_cffi | Powered by Gemini Web</sub>
</div>

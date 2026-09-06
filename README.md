# Inspiration Fetch

Frontend and backend services can be managed together with the cross-platform Python scripts in the project root.

## Central Configuration

All runtime configuration is stored in the root `.env` file. Start from the safe template:

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Important settings:

| Setting | Purpose |
| --- | --- |
| `APP_CRAWL_DELAY_SLOTH` | Delay per accepted video for sloth collection mode |
| `APP_CRAWL_DELAY_HUMAN` | Delay per accepted video for human collection mode |
| `APP_CRAWL_DELAY_DEFAULT` | Delay per accepted video for default collection mode |
| `APP_CRAWL_DELAY_FLASH` | Delay per accepted video for flash collection mode |
| `APP_DOUYIN_CRAWL_HEADLESS` | Whether Douyin collection uses a headless browser (default: false) |
| `APP_SECURITY_VERIFICATION_TIMEOUT_SECONDS` | Time allowed to manually complete a platform security challenge (default: 180 seconds) |
| `APP_ADMIN_USERNAME` | Initial administrator username |
| `APP_ADMIN_PASSWORD` | Initial administrator password |
| `APP_MIMO_API_KEY` | Xiaomi MiMo API key |
| `APP_DATABASE_URL` | Shared database connection URL |
| `APP_ANALYSIS_SCAN_INTERVAL_SECONDS` | Inspiration analysis scan interval in seconds |

Administrator credentials are only used when that administrator does not already exist. Changing them does not rename an existing account or reset its password.

The current database adapter supports `sqlite:///` URLs. `APP_DATABASE_URL` provides a stable configuration boundary for a future PostgreSQL or remote database adapter, but changing the URL scheme alone does not add remote database support.

Restart all services after changing `.env`:

```bash
python3 restart.py
```

## Prerequisites

- Python 3.9 or later
- Node.js and npm available on `PATH`
- Internet access on the first startup (to download Python, npm, and Chromium dependencies)

`start.py` and `restart.py` are first-run friendly: they create both Python virtual
environments, install each `requirements.txt`, install Playwright Chromium for
platform login/collection, install frontend packages from `package-lock.json`, and
create `.env` from `.env.example` when it is missing. On later runs, dependencies
are only reinstalled when the corresponding requirements or lock file changes.

The scripts detect `.venv/bin/python` on macOS/Linux and `.venv\Scripts\python.exe`
on Windows. If Python or Node.js is absent, they stop with an installation hint.

## Service Management

macOS and Linux:

```bash
python3 start.py
python3 stop.py
python3 restart.py
```

Windows PowerShell or Command Prompt:

```powershell
py start.py
py stop.py
py restart.py
```

Use `python` instead of `py` on Windows if the Python launcher is not installed.

The services run in the background at:

- Frontend: `http://127.0.0.1:4200`
- Backend: `http://127.0.0.1:8080`
- Analyzer: `http://127.0.0.1:8090`

Runtime state and logs are stored in `.runtime/`:

```text
.runtime/backend.log
.runtime/frontend.log
.runtime/analyzer.log
.runtime/services.json
```

The stop script only targets process IDs recorded by the start script. On Windows it stops the complete process tree with `taskkill`; on macOS/Linux it stops the managed process group.

## Development Conventions（开发约定）

**本项目所有采集相关改动必须遵循 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)（成功案例）的实现思路**，不得自行发明替代方案。要点清单：

1. **签名**：抖音 A-Bogus 用 PyExecJS 编译 `fetch-api/libs/douyin.js` 调用 `sign_datail`（对齐上游 `get_a_bogus_from_js`），禁止依赖页面注入 `byted_acrawler`；小红书用 `xhshow` 签名并带 `x-b3-traceid`（对齐上游 `playwright_sign.py`）。
2. **接口与参数**：抖音搜索用 `/aweme/v1/web/general/search/single/` 及上游 `search_info_by_keyword` 的完整参数（`search_channel=aweme_video_web`、`search_source=tab_search`、`from_group_id`、`count=15`）；`/v1/web/general/search` 家族**不加 a_bogus**（上游 `fix: dy search`）；时间/排序筛选用 `filter_selected` JSON。
3. **登录态**：复用登录时的持久化 profile（`launch_persistent_context`）；采集浏览器默认 headless（`APP_DOUYIN_CRAWL_HEADLESS=true`，不打扰用户）；cookie 一律从**活跃浏览器会话**实时获取（`context_cookie_str`），磁盘持久化缺失时从 `storage.json` 快照注入（`ensure_profile_cookies`）——禁止回退到"只读快照、不启动浏览器"的方案。
4. **请求层**：httpx 必须携带完整浏览器请求头（`sec-ch-ua` / `sec-fetch-*` / `Accept-Language` 等），msToken 实时取自页面 `localStorage`（`xmst`）。
5. **健壮性**：单条视频失败跳过继续、限流跳过关键词、失败重试退避（对齐上游 skip-on-error 语义）。
6. **采集节奏**：保留速度档位（sloth/human/default/flash）+ 随机抖动，节奏由 `.env` 的 `APP_CRAWL_DELAY_*` 控制。

## CDP 模式（抖音采集，MediaCrawler 默认方案）

开启后，采集/登录复用**你自己日常使用的 Chrome**（真实登录态 + 真实指纹，风控概率最低），不再依赖扫码快照。**推荐用项目自带的一键脚本**：

```bash
# Windows PowerShell（先关闭所有 Chrome 窗口，避免单实例拦截调试端口）
py start_chrome_debug.py
# macOS / Linux
python3 start_chrome_debug.py
```

脚本会用独立调试 profile（`.chrome-debug/`，已在 `.gitignore`）启动带 `--remote-debugging-port=9222` 的 Chrome 并打开抖音。然后在弹出的 Chrome 窗口里扫码登录抖音即可。

> 说明：Chrome 136+ 的安全策略要求 `--remote-debugging-port` 必须搭配独立 `--user-data-dir` 才会生效，所以脚本不使用你的默认 profile——登录态存在 `.chrome-debug/`，长期有效，且不影响你日常 Chrome。

手动方式（二选一）：
1. Chrome 地址栏打开 `chrome://inspect/#remote-debugging`，勾选 **"Allow remote debugging for this browser instance"**
2. 或命令行启动：`chrome.exe --remote-debugging-port=9222 --user-data-dir=<独立目录>`

确认 `.env`：`APP_DOUYIN_CDP_ENABLED=true`、`APP_DOUYIN_CDP_URL=http://127.0.0.1:9222`。

行为说明：
- 采集时会在调试 Chrome 中打开抖音页取登录态与 msToken，随后 httpx 请求采集；**不会关闭你的浏览器**
- 登录扫码在调试 Chrome 中进行（登录态直接进调试 profile，不再导出 storage.json 快照）
- **CDP 不可用时自动回退**到 headless profile 模式（`APP_DOUYIN_CRAWL_HEADLESS=true`），功能不受影响
- 端口探测快速失败（3 秒），不会拖慢采集

## 团队使用说明

- **每台机器**：`git clone` 项目 → `py start.py`（首次自动装依赖）→ 运行 `py start_chrome_debug.py` 开调试 Chrome → 在调试 Chrome 里扫码登录抖音/小红书 → 采集页即可使用。
- **账号体系**：每位成员注册自己的账号，由管理员（root）在"用户中心"审批。采集数据按用户隔离（`fetch-api/data/auth/{user_id}/`、数据库按 `user_id` 过滤）。
- **CDP 登录态隔离**：`.chrome-debug/` 在每台机器本地，登录态互不可见；已在 `.gitignore`，不会误提交。
- **敏感配置**：`.env`（管理员密码、MiMo API Key、内部 token）在 `.gitignore`，请勿提交；新成员从 `.env.example` 复制并各自填写。
- **常见问题**：
  - 采集提示"平台接口未返回任何候选视频…被风控"→ 通常是因为**调试 Chrome 被关闭**（9222 未监听）导致回退 headless 模式触发风控。**保持调试 Chrome 窗口运行**即可；若已关闭，重新运行 `py start_chrome_debug.py`。
  - 调试 Chrome 打不开 9222 → 先关闭全部 Chrome 窗口再运行 `start_chrome_debug.py`（Chrome 单实例会拦截调试端口）。
  - 日志排查：`.runtime/backend.log`（已含采集与签名日志）。

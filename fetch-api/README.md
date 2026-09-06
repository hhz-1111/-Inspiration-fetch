# Fetch API

A lightweight FastAPI service for platform authorization, browser-based content collection, and persistent analysis queues.

## Technology Stack

- FastAPI and Uvicorn
- SQLite
- Playwright with Chromium
- PBKDF2-HMAC-SHA256 password hashing
- HttpOnly cookie-based sessions

## Application Flow

### User authentication

Users register with a username and password. New accounts remain in a pending state until an administrator approves them. Approved users receive a seven-day HttpOnly session cookie after signing in.

Administrators can:

- View registered users
- Approve or disable accounts
- Reset user passwords
- Delete users and their associated data

### Platform authorization

Platform authorization is isolated by application user.

1. The frontend checks whether the current user has a valid platform session.
2. The user explicitly starts the platform login flow.
3. Playwright opens a visible Chromium window.
4. The user completes QR-code or interactive login in that window.
5. The backend saves the authenticated browser storage state only after login is confirmed.
6. The browser closes and later collection tasks reuse the saved state.

Authorization files are stored under:

```text
data/auth/{user_id}/{platform}.json
```

These files contain sensitive session data and must not be committed or exposed through an API.

### Content collection

Collection requests are persisted in SQLite and executed as in-process asynchronous tasks. Playwright opens the platform search page with the current user's saved authorization state, extracts video metadata, and stores the results.

The frontend polls the task endpoint until the task is completed or failed. Tasks interrupted by a backend restart are marked as failed during startup.

### Analysis queue

Users can add collected videos to a persistent analysis queue. Queue entries are grouped by keyword in the frontend. Adding the same video more than once is idempotent, and removing an entry only removes it from the analysis queue, not from the original collection history.

## Data Isolation

The following data is scoped to the authenticated application user:

- Platform authorization state
- Playwright login sessions
- Collection tasks
- Collected videos
- Analysis queue entries
- Keyword groups

The backend derives the user ID from the server-side session. Business endpoints do not accept a user ID from the frontend.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Create the central environment file from the repository root:

```bash
cp ../.env.example ../.env
```

Start the API:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

The API is available at `http://127.0.0.1:8080`. Interactive API documentation is available at `http://127.0.0.1:8080/docs`.

The Angular development server proxies `/api` requests to this backend.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `APP_FETCH_API_HOST` | `127.0.0.1` | API bind address |
| `APP_FETCH_API_PORT` | `8080` | API port |
| `APP_CORS_ORIGINS` | Local frontend origins | Comma-separated allowed origins |
| `APP_AUTH_BROWSER_HEADLESS` | `false` | Whether the authorization browser runs without a visible window |
| `APP_CRAWL_BROWSER_HEADLESS` | `true` | Runs collection tasks in the background without a visible browser window |
| `APP_DOUYIN_CRAWL_HEADLESS` | `false` | Controls headless mode specifically for Douyin collection tasks |
| `APP_SECURITY_VERIFICATION_TIMEOUT_SECONDS` | `180` | Time allowed for manually completing a platform security challenge |
| `APP_COOKIE_SECURE` | `false` | Restricts session cookies to HTTPS when enabled |

Use `APP_COOKIE_SECURE=true` behind HTTPS in production.

## Storage

```text
data/fetch.db       SQLite database
data/auth/          Per-user Playwright authorization state
```

Both locations contain private application data and are excluded from version control.

## Platform Maintenance

Platform page structure and risk-control behavior can change without notice. Login QR selectors and collection selectors are centralized in `app/platforms.py`.

Only collect content that the application is authorized to access, and comply with the applicable platform rules.

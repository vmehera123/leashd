# WebUI

leashd ships with a browser-based interface as an alternative to Telegram. The WebUI connects to the same daemon over WebSocket, providing streaming responses, approval prompts, interactions, and tool activity — all from a browser tab.

## Quick Start

```bash
leashd webui enable    # prompts for API key and port
leashd start           # start daemon with WebUI enabled
```

Open `http://localhost:8080` (or your configured port) and enter your API key.

## Configuration

### CLI Commands

| Command | Description |
|---|---|
| `leashd webui` / `leashd webui show` | Show WebUI status (enabled, host, port) |
| `leashd webui enable` | Enable WebUI, set API key and port |
| `leashd webui disable` | Disable WebUI |
| `leashd webui url` | Print the WebUI URL |

### Environment Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `LEASHD_WEB_ENABLED` | `bool` | `false` | Enable the WebUI connector |
| `LEASHD_WEB_HOST` | `str` | `"0.0.0.0"` | Host to bind the HTTP server |
| `LEASHD_WEB_PORT` | `int` | `8080` | Port for the HTTP/WebSocket server |
| `LEASHD_WEB_API_KEY` | `str \| None` | `None` | API key for authentication (required) |
| `LEASHD_WEB_CORS_ORIGINS` | `list[str]` | `""` | Comma-separated allowed CORS origins. Empty = no cross-origin (secure default). |
| `LEASHD_WEB_DEV_MODE` | `bool` | `false` | Enable dev mode (disables caching, adds debug headers) |
| `LEASHD_WEB_TELEGRAM_NOTIFY` | `bool` | `false` | Send Telegram cross-notifications with deep links when user is away from browser |

These can be set in `~/.leashd/config.yaml`, `.env`, or as environment variables. The `leashd webui enable` command manages them for you.

## Authentication

The WebUI uses API key authentication at two levels:

1. **WebSocket auth** — after connecting, the client sends an `auth` message with the API key. The server validates it and returns `auth_ok` (with `chat_id` and `session_id`) or `auth_error`.
2. **REST API auth** — endpoints require the `X-Api-Key` header. The `/api/health` and `/api/status` endpoints are public.

API keys are compared using constant-time comparison (`hmac.compare_digest`) to prevent timing attacks. Failed WebSocket auth attempts are rate-limited per IP (5 failures → 60-second lockout).

The API key is stored in `sessionStorage` for automatic reconnection within the same browser tab. It is never sent to external services.

## WebSocket Protocol

The WebUI communicates over a single WebSocket connection at `/ws`. Messages are JSON objects with `type` and `payload` fields.

### Client → Server

| Type | Payload | Description |
|---|---|---|
| `auth` | `{ api_key, session_id? }` | Authenticate after WebSocket connect |
| `message` | `{ text }` | Send a user message or slash command |
| `approval_response` | `{ approval_id, approved }` | Respond to an approval request |
| `interaction_response` | `{ interaction_id, answer }` | Answer a question or plan review |
| `interrupt_response` | `{ interrupt_id, send_now }` | Respond to an interrupt prompt |
| `ping` | `{}` | Keep-alive ping (every 25s) |

### Server → Client

| Type | Payload | Description |
|---|---|---|
| `auth_ok` | `{ chat_id, session_id }` | Authentication succeeded |
| `auth_error` | `{ reason }` | Authentication failed |
| `message` | `{ text, message_id?, buttons? }` | Complete message from the agent |
| `stream_token` | `{ text, message_id }` | Streaming update (replaces previous content for this message_id) |
| `message_complete` | `{ message_id }` | Streaming finished for a message |
| `message_delete` | `{ message_id }` | Delete a message from the UI |
| `tool_start` | `{ tool, command, message_id }` | Tool execution started |
| `tool_end` | `{}` | Tool execution finished |
| `approval_request` | `{ request_id, tool, description }` | Request human approval |
| `approval_resolved` | `{ request_id }` | Approval resolved (by another client or auto-approver) |
| `question` | `{ interaction_id, question, header, options }` | Agent asks a question |
| `plan_review` | `{ interaction_id, description }` | Plan review prompt |
| `interrupt_prompt` | `{ interrupt_id, message_preview, message_id }` | New message while agent is running |
| `task_update` | `{ phase, status, description }` | Task orchestrator phase change |
| `status` | `{ typing? }` | Status indicator |
| `error` | `{ reason }` | Error message |
| `pending_state` | `{ approvals, questions, interrupts }` | Re-sends all pending interactions after reconnect |
| `config_updated` | `{ key, value }` | Server-side config change notification |
| `reload` | `{}` | Signal client to reload (e.g., after daemon config reload) |
| `history` | `{ messages }` | Message history replay |
| `pong` | `{}` | Response to ping |

## Streaming

When the agent generates a response, the WebUI receives incremental `stream_token` messages. Each token carries the full accumulated text (not a delta), so the client simply replaces the message content. A blinking cursor indicates active streaming. When the agent finishes, a `message_complete` message removes the cursor.

## Approval & Interaction Flow

When the agent needs human approval:

1. Server sends `approval_request` with tool name and description
2. WebUI shows a modal with **Approve** and **Deny** buttons
3. User clicks a button → client sends `approval_response`
4. Server sends `approval_resolved` to close the modal

Approval modals cannot be dismissed with Escape or by clicking outside — they require explicit action. Question and interrupt modals can be dismissed with Escape.

If multiple approvals arrive simultaneously, they are queued and shown one at a time.

## Push Notifications

leashd supports a layered notification system so you never miss an approval:

1. **Web Push** — Service Worker delivers lock-screen alerts even when the browser is closed. Uses VAPID key authentication. VAPID keys are stored in `~/.leashd/vapid_keys.json` and subscriptions in `~/.leashd/push_subscriptions.json`.
2. **In-page notifications** — Web Notification API with an audio chime and tab title flash when the tab is in the background.
3. **Telegram cross-notification** — optional deep links sent to your phone when you're away from the browser. Enable with `LEASHD_WEB_TELEGRAM_NOTIFY=true`.

Push notification endpoints:

| Endpoint | Method | Description |
|---|---|---|
| `/api/push/vapid-key` | GET | Get the server's VAPID public key |
| `/api/push/subscribe` | POST | Subscribe to push notifications (`{ subscription, chat_id }`) |
| `/api/push/subscribe` | DELETE | Unsubscribe (`{ chat_id }`) |
| `/api/push/test` | POST | Send a test push notification (`{ chat_id }`) |

All push endpoints require the `X-Api-Key` header.

## PWA Support

The WebUI is installable as a Progressive Web App on iOS, Android, and desktop. Add it to your home screen for a standalone app experience with proper safe-area handling on notched devices.

To install: in Chrome or Safari, tap "Add to Home Screen" (mobile) or "Install" (desktop).

Push notifications require PWA installation on iOS — Safari in-browser does not support Web Push. The app detects this and prompts you to install first.

## Color Themes

27 color themes are available, each with dark and light variants. Select from the Settings page in the WebUI. Themes include: Dracula, Monokai, Catppuccin, Nord, Synthwave, Matrix, Solarized, One Dark, Gruvbox, and more.

The theme preference is stored in `localStorage` and applied immediately without page reload.

## Seamless Reconnection

The WebUI handles disconnections gracefully:

- **Pending state recovery** — after reconnect, the server sends a `pending_state` message re-delivering all pending approvals, questions, and plan reviews so nothing is lost.
- **Grace period** — a 120-second disconnect grace period keeps your session alive through sleep/wake cycles and network blips.
- **Instant reconnect** — the Page Visibility API triggers immediate reconnection when you unlock your phone or switch back to the tab.
- **Session cache** — pending interactions are cached in `sessionStorage` so they survive page reloads.

## Tunnel (Remote Access)

Access the WebUI from your phone or any device:

```bash
leashd webui tunnel                           # ngrok (default)
leashd webui tunnel --provider cloudflare      # Cloudflare Tunnel
leashd webui tunnel --provider tailscale       # Tailscale Funnel
```

The tunnel provider CLI must be installed separately. When exposed publicly, your `LEASHD_WEB_API_KEY` is your authentication layer — choose a strong key. Failed auth attempts are rate-limited (5 failures → 60s lockout).

## Mobile Browser Support

The WebUI is responsive and works on mobile browsers. The layout adapts to small screens with:

- Full-width message bubbles
- Compact header with truncated working directory
- Full-width modal dialogs
- Touch-friendly button sizes

## Simultaneous Telegram + WebUI

When both Telegram and WebUI are configured, leashd runs them simultaneously via the `MultiConnector`. Messages are routed by `chat_id` prefix — WebUI sessions use `web:` prefix, Telegram sessions use numeric chat IDs. Both connectors share the same Engine, safety pipeline, and session store.

## Troubleshooting

### Connection Issues

- **"Connection unstable" indicator** — the WebUI monitors ping/pong timing. If no pong is received within ~50 seconds, the connection dot turns yellow. This usually indicates network issues or server load.
- **Repeated disconnects** — the WebUI reconnects automatically with exponential backoff (1s → 30s max, with random jitter). Check that the daemon is running with `leashd status`.
- **Auth failures on reconnect** — the API key is stored in `sessionStorage`. If you clear browser data, you'll need to re-enter it.

### CORS

If accessing the WebUI from a different origin (e.g., a reverse proxy on a different domain), configure allowed origins:

```bash
LEASHD_WEB_CORS_ORIGINS=https://my-proxy.example.com
```

### Port Conflicts

If port 8080 is in use, change it:

```bash
leashd webui enable   # prompts for port
# or set directly:
LEASHD_WEB_PORT=9090
```

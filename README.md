# Gemini Enterprise Web Proxy (`v1.0.0`)

An OpenAI-compatible local API gateway for **Gemini Enterprise / Vertex AI Agentspace** web portals, built with **FastAPI** and **Playwright**. It enables AI coding harnesses and clients—such as **Hermes Agent**, **OpenCode**, and **Cursor**—to use your authenticated Gemini web session with real-time SSE streaming, full tool-calling support, multi-session delta synchronization, and multimodal uploads.

**Educational Purpose**: Created to demonstrate how to bridge web-based enterprise AI portals to standard OpenAI-compatible developer tools using resilient browser automation.

---

## ✨ Key Features (`v1.0.0`)

- **OpenAI-Compatible API**: Drop-in support for `POST /v1/chat/completions`, `GET /v1/models`, `POST /v1/sessions/reset`, and `GET /health`.
- **Real-Time SSE Streaming & Tool-Call Holdback**: Streams incremental Markdown deltas in real time while holding back `<tool_call>` blocks and incomplete Markdown table rows so raw XML never leaks into assistant prose.
- **Resilient Tool Calling**: Translates OpenAI JSON Schema tool definitions into a structured `<tool_call>` XML protocol and parses single or parallel tool invocations back into standard OpenAI `tool_calls` payloads.
- **Deep Shadow-DOM Markdown Extractor**: Pierces nested `ucs-*` shadow roots to reconstruct clean GitHub-Flavored Markdown (fenced code blocks with language detection, inline code, tables, nested lists, headings, blockquotes, and links) while stripping portal UI noise (`[[section:...]]`, `suggested_questions`, `<a2a_datapart_json>`, skill badges, and progress spinners).
- **Multi-Tab Session Pool & Delta Sync**: Tracks conversation history hashes per session. Append-only follow-up turns send **only the delta messages** to the existing browser tab (`DELTA` mode), while history edits/branches automatically reset the tab (`RESET+FULL` mode). Idle tabs are automatically evicted via TTL and LRU limits.
- **Multimodal & File Attachments**: Supports base64 `image_url` uploads via browser file input and inline base64 document/file decoding.
- **Configurable Portal URL & Bearer Auth**: Easily configure `TARGET_URL` and `API_KEY` via your local `config.properties`.
- **Client Disconnect Abort**: Automatically clicks the Gemini UI Stop button if the HTTP client disconnects mid-generation.

---

## 📋 Requirements

- Python 3.10+ (tested on Python 3.13)
- Google Chrome installed
- Valid account with access to your Gemini web portal (`TARGET_URL`)

---

## 🚀 Installation & Setup

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/lalalalahu/gemini-ent-web-proxy.git
cd gemini-ent-web-proxy

pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Portal URL & Authentication (`config.properties`)

Copy the example configuration file and set your target portal URL and optional Bearer token:

```bash
cp config.properties.example config.properties
```

Edit `config.properties`:

```ini
# Target Gemini Enterprise / Vertex AI Agentspace Web Portal URL
TARGET_URL=https://your-gemini-portal.example.com

# Leave empty to run without authentication (binds to 127.0.0.1 by default)
# Set a value to require 'Authorization: Bearer <API_KEY>' on /v1/* endpoints
API_KEY=your-secret-key
```

### 3. First Run & Persistent SSO Login

```bash
python run.py
```

On the first run (when `~/.gemini-service/logged-in` does not exist):
1. A visible Chrome window opens and navigates to `TARGET_URL`.
2. Log in with your account credentials / SSO.
3. Once the Gemini editor (`ucs-prosemirror-editor`) is detected, the proxy saves your session profile in `~/.gemini-service/chrome-profile` and automatically restarts in **headless mode**.

### 4. Gemini Portal Skill Setup (Recommended for Tool Calling)

For reliable local tool execution (`read_file`, `write_file`, `terminal`, etc.), add the universal tool-calling skill in [`skills/universal.md`](skills/universal.md) to your Gemini Enterprise custom skills as **`universal`**.

---

## 🔌 Client Configuration

### Hermes Agent / Generic OpenAI Client

- **Base URL**: `http://localhost:8080/v1`
- **API Key**: Value from `config.properties` (or `not-needed` if `API_KEY` is empty)
- **Model**: `Auto` (or any model returned by `/v1/models`)

### OpenCode (`~/.config/opencode/opencode.json`)

```json
{
  "providers": {
    "00bx-gemini": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://localhost:8080/v1",
        "apiKey": "your-secret-key"
      },
      "models": {
        "Auto": {}
      }
    }
  }
}
```

---

## 📡 API Endpoints

| Method | Endpoint | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | No | Returns gateway readiness (`ready` / `initializing`), `version` (`1.0.0`), active/dirty session counts, and `available_models`. |
| `GET` | `/v1/models` | Bearer | Returns the OpenAI-compatible model list dynamically fetched from the Gemini web UI. |
| `POST` | `/v1/chat/completions` | Bearer | OpenAI Chat Completions endpoint supporting `stream: true/false`, `tools`, `tool_choice`, `stream_options.include_usage`, multimodal `image_url`, and session tracking. |
| `POST` | `/v1/sessions/reset` | Bearer | Resets a specific session (`?session_id=...`) or closes and clears all active browser tabs when omitted. |

### Quick `curl` Example

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-key" \
  -d '{
    "model": "Auto",
    "stream": false,
    "session_id": "demo-session-1",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## ⚙️ Configuration & Environment Variables

All settings can be placed in `config.properties` or exported as environment variables:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TARGET_URL` | `https://gemini.google.com` *(or from `config.properties`)* | Target Gemini web portal URL |
| `API_KEY` | *(from `config.properties`)* | Bearer token for `/v1/*` routes |
| `HOST` | `0.0.0.0` (if `API_KEY` set) / `127.0.0.1` | Bind address when running `server.py` |
| `PORT` | `8080` | HTTP listen port |
| `MAX_OPEN_TABS` | `10` | Max concurrent browser session tabs before LRU eviction |
| `SESSION_IDLE_TTL_SECONDS` | `900` | Idle session timeout in seconds (15 minutes) |
| `INITIAL_RESPONSE_GRACE_SECONDS` | `2.0` | Minimum wait after initial content appears |
| `INTERMEDIATE_STATUS_GRACE_SECONDS` | `45.0` | Grace window when waiting on interim retrieval status (`"I will search..."` or `[[section:...]]`) |
| `FINAL_RESPONSE_QUIET_SECONDS` | `3.5` | Quiet window with no DOM text changes or active spinners before finalizing a turn |

---

## 🧪 Testing

The project supports both **offline unit tests** (pure Python logic for Markdown cleaning, `<tool_call>` parsing, and delta hash tracking) and **live E2E integration tests** against a running proxy on `127.0.0.1:8080`:

```bash
# Run fast offline unit tests only (no browser required)
python test_proxy.py TestProxyUnitLogic

# Run the full suite (Unit + Live E2E against localhost:8080)
python test_proxy.py
```

---

## 🛠️ Troubleshooting

### Session Expired or Stuck on Login
Delete the persistent login flag or profile directory and re-authenticate:

```bash
rm -rf ~/.gemini-service
python run.py
```

### Conversation Isolation
To ensure separate client chats use dedicated browser tabs, pass a consistent identifier via `session_id`, `conversation_id`, `user`, or headers (`X-Session-ID`, `X-Conversation-ID`, `X-OpenCode-Session-ID`, `X-Thread-ID`). When omitted, the proxy automatically matches conversation lineage using SHA-256 message prefix hashes.

---

## 📁 Project Structure

```text
gemini-ent-web-proxy/
├── server.py                  # FastAPI gateway, Playwright pool, Shadow-DOM extractor & SSE streamer
├── run.py                     # Uvicorn launcher (0.0.0.0:8080)
├── test_proxy.py              # Offline unit tests & live E2E integration test suite
├── config.properties.example  # Template for local TARGET_URL and API_KEY configuration
├── skills/
│   └── universal.md           # Universal tool-calling skill instruction for Gemini Enterprise
├── requirements.txt           # Python dependencies
├── setup.sh                   # Quick setup script
└── README.md                  # Documentation
```

## 📄 License

MIT License - See [LICENSE](LICENSE) for details.

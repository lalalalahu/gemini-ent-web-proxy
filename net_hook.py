import os
import re
from typing import Any, Dict, Optional


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


NETWORK_HOOK_ENABLED: bool = _env_bool("NETWORK_HOOK_ENABLED", True)
FAST_NET_SETTLE_SECONDS: float = float(os.getenv("FAST_NET_SETTLE_SECONDS", "0.8"))
DOM_POLL_INTERVAL_SEC: float = float(os.getenv("DOM_POLL_INTERVAL_SEC", "0.18"))

FETCH_HOOK_INIT_SCRIPT = """(() => {
    if (window.__geminiNetHookInstalled) return;
    window.__geminiNetHookInstalled = true;

    const state = {
        activeStreams: 0,
        totalStreamsStarted: 0,
        totalStreamsEnded: 0,
        lastStreamStartAt: 0,
        lastStreamEndAt: 0,
        lastErrorStatus: 0,
        lastErrorAt: 0,
    };
    window.__geminiNetState = state;

    const origFetch = window.fetch;
    if (typeof origFetch !== 'function') return;

    window.fetch = async function(...args) {
        let urlStr = '';
        try {
            const req = args[0];
            urlStr = (typeof req === 'string') ? req : (req && req.url ? req.url : '');
        } catch (e) {}

        const resp = await origFetch.apply(this, args);
        try {
            if (!resp.ok && (resp.status === 401 || resp.status === 403 || resp.status === 429)) {
                state.lastErrorStatus = resp.status;
                state.lastErrorAt = Date.now();
            }

            const ct = (resp.headers && resp.headers.get('content-type') || '').toLowerCase();
            const lowerUrl = (urlStr || '').toLowerCase();
            const looksStreaming = (
                ct.includes('text/event-stream') ||
                ct.includes('application/x-ndjson') ||
                ct.includes('application/octet-stream') ||
                lowerUrl.includes('stream') ||
                lowerUrl.includes('assist') ||
                lowerUrl.includes('generate')
            );

            if (looksStreaming && resp.body && typeof resp.body.tee === 'function') {
                const [clientBranch, monitorBranch] = resp.body.tee();
                state.activeStreams += 1;
                state.totalStreamsStarted += 1;
                state.lastStreamStartAt = Date.now();

                const reader = monitorBranch.getReader();
                (async () => {
                    try {
                        while (true) {
                            const { done } = await reader.read();
                            if (done) break;
                        }
                    } catch (e) {
                    } finally {
                        state.activeStreams = Math.max(0, state.activeStreams - 1);
                        state.totalStreamsEnded += 1;
                        state.lastStreamEndAt = Date.now();
                    }
                })();

                const wrapped = new Response(clientBranch, {
                    status: resp.status,
                    statusText: resp.statusText,
                    headers: resp.headers,
                });
                try {
                    Object.defineProperty(wrapped, 'url', { value: resp.url });
                } catch (e) {}
                return wrapped;
            }
        } catch (e) {
            // Never interfere with the page's own fetch response
        }
        return resp;
    };
})();"""

FENCE_LINE_RE = re.compile(r"(?m)^[ \t]*(`{3,})")
OPEN_TOOL_CALL_RE = re.compile(r"(?m)^[ \t]*<tool_call\b", re.IGNORECASE)
CLOSE_TOOL_CALL_RE = re.compile(r"(?m)^[ \t]*</tool_call>[ \t]*$", re.IGNORECASE)
INCOMPLETE_TABLE_TAIL_RE = re.compile(r"(?m)\n[ \t]*\|[^\n]*\Z")


def has_unclosed_syntax(text: str) -> bool:
    """Return True if text has unclosed code fences, unclosed <tool_call> tags, or a trailing incomplete table row."""
    if not text:
        return False

    in_fence = False
    fence_len = 0
    for line in text.splitlines():
        m = FENCE_LINE_RE.match(line)
        if m:
            ticks = len(m.group(1))
            if not in_fence:
                in_fence = True
                fence_len = ticks
            elif ticks >= fence_len and line.strip() == m.group(1):
                in_fence = False
                fence_len = 0
    if in_fence:
        return True

    opens = len(OPEN_TOOL_CALL_RE.findall(text))
    closes = len(CLOSE_TOOL_CALL_RE.findall(text))
    if opens > closes:
        return True

    if INCOMPLETE_TABLE_TAIL_RE.search(text):
        return True

    return False


async def get_net_state(page: Any) -> Dict[str, Any]:
    """Read current window.__geminiNetState safely from the browser page."""
    default_state = {
        "activeStreams": 0,
        "totalStreamsStarted": 0,
        "totalStreamsEnded": 0,
        "lastErrorStatus": 0,
    }
    if not NETWORK_HOOK_ENABLED or page is None:
        return default_state
    try:
        state = await page.evaluate("() => window.__geminiNetState || null")
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return default_state


def should_fast_exit(
    *,
    net_state: Optional[Dict[str, Any]],
    ended_before: int,
    net_settled_at: Optional[float],
    now: float,
    quiet_since: float,
    is_generating: bool,
    has_active_work: bool,
    is_pending_section: bool,
    is_intermediate: bool,
    current_text: str,
    has_seen_generation_start: bool,
    settle_seconds: float = FAST_NET_SETTLE_SECONDS,
) -> bool:
    """
    Conservative 6-condition fast-exit check:
    1. Network hook observed stream completion for this turn and activeStreams == 0.
    2. DOM shows generation stopped (!is_generating && !has_active_work).
    3. Not an intermediate status or pending [[section:...]] marker.
    4. No unclosed code fences, <tool_call> tags, or trailing incomplete table rows.
    5. Both DOM text and network stream have been settled for >= settle_seconds.
    6. Non-empty content and generation has started.
    """
    if not NETWORK_HOOK_ENABLED or not net_state:
        return False
    if not has_seen_generation_start or not current_text or not current_text.strip():
        return False
    if is_generating or has_active_work:
        return False
    if is_pending_section or is_intermediate:
        return False
    if net_state.get("activeStreams", 0) > 0:
        return False
    if net_state.get("totalStreamsEnded", 0) <= ended_before:
        return False
    if net_settled_at is None:
        return False
    if (now - net_settled_at) < settle_seconds or (now - quiet_since) < settle_seconds:
        return False
    if has_unclosed_syntax(current_text):
        return False
    return True

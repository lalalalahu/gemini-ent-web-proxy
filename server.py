import os
import re
import time
import json
import base64
import asyncio
import hashlib
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any, Union, Tuple, Set
from dotenv import load_dotenv

from playwright_stealth import Stealth
from fastapi import FastAPI, HTTPException, Depends, Request, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright, Page, BrowserContext

# ==============================================================================
# CONFIGURATION & CONSTANTS
# ==============================================================================
VERSION = "1.0.0"
AVAILABLE_MODELS = ["Auto"]

DATA_DIR = Path.home() / ".gemini-service"
PROFILE_DIR = DATA_DIR / "chrome-profile"
IMAGE_DIR = DATA_DIR / "images"
LOGIN_FLAG = DATA_DIR / "logged-in"
BASE_DIR = Path(__file__).resolve().parent
PROPERTIES_FILE = BASE_DIR / "config.properties"

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(dotenv_path=PROPERTIES_FILE, override=True)

TARGET_URL = os.getenv("TARGET_URL", "https://gemini.google.com")
API_KEY = os.getenv("API_KEY")

if not API_KEY:
    print(
        "⚠️ WARNING: API_KEY is not set or config.properties is missing! Running"
        " without authentication (bound to 127.0.0.1 by default)."
    )
else:
    print("🔒 API Key protection enabled from config.properties.")

security = HTTPBearer(auto_error=False)


def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(security),
):
    if not API_KEY:
        return True

    if not credentials or not secrets.compare_digest(
        credentials.credentials, API_KEY
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return credentials.credentials


# ==============================================================================
# RUNTIME STATE & SESSION MANAGEMENT
# ==============================================================================
playwright_instance = None
context: Optional[BrowserContext] = None
cleanup_task: Optional[asyncio.Task] = None
session_pages: Dict[str, Page] = {}
page_locks: Dict[str, asyncio.Lock] = {}
session_creation_locks: Dict[str, asyncio.Lock] = {}
session_history_hashes: Dict[str, List[str]] = {}
session_tools_hashes: Dict[str, str] = {}
dirty_sessions: Set[str] = set()
session_last_used: Dict[str, float] = {}
is_ready = False

MAX_OPEN_TABS = int(os.getenv("MAX_OPEN_TABS", "10"))
SESSION_IDLE_TTL_SECONDS = float(os.getenv("SESSION_IDLE_TTL_SECONDS", "900"))  # 15 mins
IMAGE_TTL_SECONDS = float(os.getenv("IMAGE_TTL_SECONDS", "86400"))  # 24 hours
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "300"))  # 5 mins
MAX_FULL_SYNC_CHARS = int(os.getenv("MAX_FULL_SYNC_CHARS", "75000"))
MAX_SINGLE_TOOL_CHARS = int(os.getenv("MAX_SINGLE_TOOL_CHARS", "30000"))

# Tuned timers: Resistant to mid-stream pauses on long 10-point responses and image generation
INITIAL_RESPONSE_GRACE_SECONDS = float(os.getenv("INITIAL_RESPONSE_GRACE_SECONDS", "2.0"))
INTERMEDIATE_STATUS_GRACE_SECONDS = float(os.getenv("INTERMEDIATE_STATUS_GRACE_SECONDS", "45.0"))
FINAL_RESPONSE_QUIET_SECONDS = float(os.getenv("FINAL_RESPONSE_QUIET_SECONDS", "3.5"))

INTERMEDIATE_STATUS_RE = re.compile(
    r"^\s*(?:i(?:'ll| will)|let me)\s+(?:load|activate|enable|search|check|look(?:\s+up)?|retrieve|gather|analy[sz]e|inspect|examine|read|review|query|find)\b",
    re.IGNORECASE,
)
IMAGE_CAPTION_ANNOUNCE_RE = re.compile(
    r"(?:image\s+generated\s+by|generating\s+image|creating\s+image)",
    re.IGNORECASE,
)

TRAILING_SECTION_RE = re.compile(r"\[\[section:[^\]]*\]\]\s*$", re.IGNORECASE)

# Line-anchored regexes to detect standalone or fenced <tool_call> blocks without matching mid-line prose or JSON string contents
STANDALONE_TOOL_CALL_OPEN_RE = re.compile(
    r"(?m)^[ \t]*(?:`{3,}(?:xml|json|tool_call)?[ \t]*\r?\n)?[ \t]*<tool_call\b",
    re.IGNORECASE,
)
PARTIAL_TOOL_CALL_TAIL_RE = re.compile(
    r"(?m)^[ \t]*(?:`{1,4}(?:xml|json|tool_call)?[ \t]*\r?\n)?[ \t]*(?:`{1,4}(?:xml|json|tool_call)?|<[a-z_/]*)\Z",
    re.IGNORECASE,
)
LINE_XML_TOOL_CALL_RE = re.compile(
    r"(?m)^[ \t]*(?:`{3,}(?:xml|json|tool_call)?[ \t]*\r?\n)?[ \t]*<tool_call>[ \t]*\r?\n?([\s\S]*?)^[ \t]*</tool_call>[ \t]*(?:\r?\n[ \t]*`{3,}[^\r\n]*)?(?:\r?\n[ \t]*`{3,}[ \t]*)?$",
    re.IGNORECASE,
)
PROTECTED_BLOCKS_RE = re.compile(
    r"((`{3,})[\s\S]*?\2|^[ \t]*<tool_call\b[\s\S]*?^[ \t]*</tool_call>[ \t]*$|(`+)[^`\n]+\3)",
    re.IGNORECASE | re.MULTILINE,
)
NON_TOOL_CODE_BLOCK_RE = re.compile(
    r"((`{3,})(?!(?:xml|json|tool_call)\b)[a-zA-Z0-9_+-]+[^\n]*\n[\s\S]*?\2|(`+)[^`\n]+\3)",
    re.IGNORECASE,
)
INTERNAL_PORTAL_TOOLS = {
    "transfer_to_agent",
    "load_skill",
    "load_skill_resource",
    "list_skills",
    "google_search_tool",
    "search_support_docs",
    "get_environment_status",
    "generate_memories",
    "invalid_tool_call_notifier",
    "tool_name",
}
DATAPART_JSON_RE = re.compile(
    r"(?<!`)<a2a_datapart_json>(?:(?!</?a2a_datapart_json>)[\s\S])*?</a2a_datapart_json>(?!`)",
    re.IGNORECASE,
)
INCOMPLETE_TABLE_ROW_TAIL_RE = re.compile(r"(?m)\n[ \t]*\|[^\n]*\Z")


def _mask_non_tool_code_blocks(text: str) -> str:
    """Replace inline code spans and non-tool fenced code blocks (e.g. ```python) with spaces of identical length."""
    if not text:
        return ""
    return NON_TOOL_CODE_BLOCK_RE.sub(lambda m: " " * len(m.group(0)), text)


def has_pending_section_marker(raw_text: str) -> bool:
    """True when the raw turn text ends with a [[section:...]] marker awaiting internal tool execution."""
    if not raw_text:
        return False
    stripped = DATAPART_JSON_RE.sub("", raw_text)
    stripped = re.sub(r"(?m)^```suggested_questions\b[\s\S]*?^```\s*", "", stripped, flags=re.IGNORECASE).strip()
    return bool(TRAILING_SECTION_RE.search(stripped))


def is_intermediate_status(text: str) -> bool:
    """True when Gemini has announced a tool/retrieval action, not a final answer."""
    if not text:
        return False
    stripped = text.strip()
    lower = stripped.lower()
    if "<tool_call" in lower or "</tool_call>" in lower:
        return False
    if len(stripped) > 280 or "\n\n" in stripped:
        return False
    return bool(INTERMEDIATE_STATUS_RE.match(stripped))


# ==============================================================================
# SHADOW-DOM-PIERCING STATUS & MARKDOWN EXTRACTOR
# ==============================================================================
JS_STATUS_AND_EXTRACTOR = """(turnEl) => {
    // Deep Shadow-DOM + Slot + same-origin Iframe query helper so elements inside ucs-* shadow roots are never missed
    function querySelectorAllDeep(root, selector) {
        const results = [];
        const visited = new Set();
        function walk(node) {
            if (!node || visited.has(node)) return;
            visited.add(node);
            if (node.nodeType === Node.ELEMENT_NODE || node.nodeType === Node.DOCUMENT_NODE || node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                if (node.querySelectorAll) {
                    try {
                        node.querySelectorAll(selector).forEach(el => {
                            if (!results.includes(el)) results.push(el);
                        });
                    } catch (e) {}
                }
                if (node.shadowRoot) walk(node.shadowRoot);
                if (node.tagName && node.tagName.toLowerCase() === 'slot' && typeof node.assignedNodes === 'function') {
                    try {
                        node.assignedNodes({ flatten: true }).forEach(n => walk(n));
                    } catch (e) {}
                }
                if (node.tagName && node.tagName.toLowerCase() === 'iframe') {
                    try {
                        if (node.contentDocument) walk(node.contentDocument);
                    } catch (e) {}
                }
                if (node.children) {
                    for (let child of node.children) walk(child);
                }
            }
        }
        walk(root);
        return results;
    }

    const stopButtons = querySelectorAllDeep(
        document,
        '.send-button.stop, md-icon-button.send-button.stop, [data-aria-label="Stop"], [aria-label="Stop"], button[aria-label*="Stop"]'
    );
    const hasStop = stopButtons.some(btn => btn.offsetParent !== null || btn.getClientRects().length > 0);

    let hasStopIcon = false;
    querySelectorAllDeep(document, '.send-button md-icon, md-icon-button md-icon, md-icon').forEach(icon => {
        if ((icon.textContent || '').trim().toLowerCase() === 'stop') {
            hasStopIcon = true;
        }
    });

    const nl = String.fromCharCode(10);
    const backtick = String.fromCharCode(96);
    const fence = backtick + backtick + backtick;

    function extractPlainText(node) {
        if (!node) return '';
        if (node.nodeType === Node.TEXT_NODE) {
            return node.textContent || '';
        }
        let out = '';
        if (node.shadowRoot) out += extractPlainText(node.shadowRoot);
        if (node.childNodes) {
            for (let c of node.childNodes) out += extractPlainText(c);
        }
        return out;
    }

    function detectCodeLanguage(node) {
        const cls = ((node.className && typeof node.className === 'string') ? node.className : '') + ' ' +
                    ((node.firstElementChild && typeof node.firstElementChild.className === 'string') ? node.firstElementChild.className : '');
        const match = cls.match(/language-([a-zA-Z0-9_+-]+)/i) || cls.match(/lang-([a-zA-Z0-9_+-]+)/i);
        if (match) return match[1];
        const dataLang = node.getAttribute('data-language') || node.getAttribute('lang') || '';
        return dataLang.trim();
    }

    function getDeepMarkdown(node, inPre = false, listType = null, listDepth = 0, olIndex = 1) {
        if (!node) return '';
        let tag = '';
        if (node.nodeType === Node.ELEMENT_NODE) {
            const cls = (typeof node.className === 'string') ? node.className.toLowerCase() : '';
            tag = (node.tagName || '').toLowerCase();
            const role = node.getAttribute('role') || '';

            // Skip controls, icons, and progress noise
            if (cls.includes('working-on-it-footer') || cls.includes('working-on-it-spark') || tag === 'ucs-lottie-animation') return '';
            if (['svg', 'button', 'menu', 'md-menu', 'md-icon', 'img', 'style', 'script'].includes(tag) || tag.includes('button') || tag.includes('chip')) return '';
            if (role === 'progressbar') return '';
            if (node.getAttribute('aria-hidden') === 'true' || cls.includes('sr-only')) return '';

            // Skip user query blocks, skill badges, and UI chrome, but DO NOT skip assistant content sections
            const isExcludedClass = [
                'question-block', 'question-wrapper', 'user-query', 'user-message',
                'suggested-question', 'suggestions', 'followup', 'follow-up',
                'diagnostic', 'thought-bubble', 'thinking', 'work-status', 'loading-spinner',
                'spark-icon', 'table-actions', 'citation-slot', 'code-header', 'copy-button',
                'skill-used', 'skill-chip', 'skill-badge', 'skill-card', 'skill-invocation', 'skill-header'
            ].some(c => cls.includes(c));
            if (
                isExcludedClass ||
                tag.includes('suggestion') ||
                tag.includes('followup') ||
                tag.includes('thinking') ||
                tag.includes('skill')
            ) return '';

            // Handle fenced code blocks (<pre> or custom code block elements)
            if (tag === 'pre' || cls.includes('code-block') || tag === 'ucs-code-block') {
                const codeEl = node.querySelector('code') || node;
                const lang = detectCodeLanguage(codeEl) || detectCodeLanguage(node);
                const rawCode = extractPlainText(codeEl).replace(/\\r/g, '').replace(/^\\n+|\\n+$/g, '');
                if (!rawCode) return '';
                if (tag === 'pre' && !node.querySelector('code') && !cls.includes('code') && !rawCode.includes(nl)) {
                    return rawCode + nl;
                }
                // Use 4-backtick fence if the code itself contains triple backticks
                const activeFence = rawCode.includes(fence) ? (fence + backtick) : fence;
                return nl + activeFence + lang + nl + rawCode + nl + activeFence + nl;
            }

            // Handle inline <code>
            if (tag === 'code' && !inPre) {
                const inlineCode = extractPlainText(node).trim();
                if (!inlineCode) return '';
                if (inlineCode.includes(nl)) {
                    const lang = detectCodeLanguage(node);
                    const activeFence = inlineCode.includes(fence) ? (fence + backtick) : fence;
                    return nl + activeFence + lang + nl + inlineCode + nl + activeFence + nl;
                }
                if (inlineCode.includes(fence)) {
                    return fence + backtick + ' ' + inlineCode + ' ' + fence + backtick;
                }
                if (inlineCode.includes(backtick)) {
                    return backtick + backtick + ' ' + inlineCode + ' ' + backtick + backtick;
                }
                return backtick + inlineCode + backtick;
            }
        }

        if (node.nodeType === Node.TEXT_NODE) {
            const txt = node.textContent || '';
            const trimmedLower = txt.trim().toLowerCase();
            if (trimmedLower === 'spark') return '';
            if (/^working on the request\\s*(\\(\\d+\\s*s\\))?$/i.test(txt.trim())) return '';
            if (/^skill used(?:\\s*[a-z0-9_.:-]+)?$/i.test(txt.trim())) return '';
            // Ignore inter-element HTML indentation text nodes so they never trigger 4-space Markdown code blocks
            if (/^[ \\t]*\\r?\\n[ \\t\\r\\n]*$/.test(txt)) return '';
            return txt;
        }

        if (tag === 'br') return nl;
        if (tag === 'hr') return nl + '---' + nl;

        // Explicitly extract <table> so DOM indentation never splits headers or rows
        if (tag === 'table') {
            const rows = [];
            function collectRows(n) {
                if (!n) return;
                if (n.shadowRoot) collectRows(n.shadowRoot);
                if (n.childNodes) {
                    for (let c of n.childNodes) {
                        if (c.nodeType === Node.ELEMENT_NODE) {
                            const cTag = (c.tagName || '').toLowerCase();
                            if (cTag === 'tr') {
                                rows.push(c);
                            } else if (['thead', 'tbody', 'tfoot', 'slot'].includes(cTag)) {
                                collectRows(c);
                            }
                        }
                    }
                }
            }
            collectRows(node);
            if (rows.length === 0) return '';

            const tableLines = [];
            rows.forEach((tr, rowIdx) => {
                const cells = Array.from(tr.children || []).filter(
                    c => c.tagName && ['TD', 'TH'].includes(c.tagName.toUpperCase())
                );
                if (cells.length === 0) return;
                const cellStrings = cells.map(cell => {
                    let cellMd = '';
                    if (cell.shadowRoot) cellMd += getDeepMarkdown(cell.shadowRoot, inPre, listType, listDepth, 1);
                    if (cell.childNodes) {
                        for (let ch of cell.childNodes) {
                            cellMd += getDeepMarkdown(ch, inPre, listType, listDepth, 1);
                        }
                    }
                    return cellMd.trim().replace(/\\s*\\n+\\s*/g, ' ');
                });
                tableLines.push('| ' + cellStrings.join(' | ') + ' |');
                if (rowIdx === 0) {
                    tableLines.push('| ' + cells.map(() => ':---').join(' | ') + ' |');
                }
            });
            return tableLines.length > 0 ? (nl + nl + tableLines.join(nl) + nl + nl) : '';
        }

        const isListContainer = (tag === 'ul' || tag === 'ol');
        const nextListType = (tag === 'ul') ? 'ul' : (tag === 'ol' ? 'ol' : listType);
        const nextListDepth = isListContainer ? listDepth + 1 : listDepth;

        let text = '';
        if (node.shadowRoot) {
            text += getDeepMarkdown(node.shadowRoot, inPre, nextListType, nextListDepth, 1);
        }
        if (node.childNodes) {
            let currentOlIdx = 1;
            for (let child of node.childNodes) {
                const childTag = (child.nodeType === Node.ELEMENT_NODE && child.tagName) ? child.tagName.toLowerCase() : '';
                text += getDeepMarkdown(child, inPre, nextListType, nextListDepth, currentOlIdx);
                if (childTag === 'li') currentOlIdx++;
            }
        }

        // Format Markdown structural tags
        if (tag === 'h1') return nl + '# ' + text.trim() + nl;
        if (tag === 'h2') return nl + '## ' + text.trim() + nl;
        if (tag === 'h3') return nl + '### ' + text.trim() + nl;
        if (tag === 'h4') return nl + '#### ' + text.trim() + nl;
        if (tag === 'h5') return nl + '##### ' + text.trim() + nl;
        if (tag === 'h6') return nl + '###### ' + text.trim() + nl;
        if (tag === 'strong' || tag === 'b') return text.trim() ? '**' + text.trim() + '**' : '';
        if (tag === 'em' || tag === 'i') return text.trim() ? '*' + text.trim() + '*' : '';
        if (tag === 'a') {
            const href = node.getAttribute('href');
            return (href && text.trim() && !href.startsWith('javascript:')) ? '[' + text.trim() + '](' + href + ')' : text;
        }
        if (tag === 'li') {
            const indent = '  '.repeat(Math.max(0, listDepth - 1));
            const prefix = (listType === 'ol') ? (olIndex + '. ') : '- ';
            return indent + prefix + text.trim() + nl;
        }
        if (tag === 'blockquote') {
            return nl + text.trim().split(nl).map(line => '> ' + line).join(nl) + nl;
        }
        if (['p', 'div', 'ul', 'ol'].includes(tag)) {
            return text + nl;
        }
        return text;
    }

    function filterTopLevelNodes(nodes) {
        return nodes.filter((node, i) => !nodes.some((other, j) => {
            if (i === j) return false;
            let cur = node.parentNode || node.host;
            while (cur) {
                if (cur === other) return true;
                cur = cur.parentNode || cur.host;
            }
            return false;
        }));
    }

    let rawText = "";
    const seenBlocks = new Set();
    function appendUniqueBlock(md) {
        const trimmed = (md || '').trim();
        if (!trimmed) return;
        if (seenBlocks.has(trimmed)) return;
        seenBlocks.add(trimmed);
        rawText += trimmed + nl + nl;
    }
    // Collect top-level summary / streamer blocks across light and shadow DOM inside turnEl
    const summaries = filterTopLevelNodes(
        querySelectorAllDeep(turnEl, 'ucs-summary, .summary, [data-message-author-role="assistant"]')
    );
    if (summaries.length > 0) {
        summaries.forEach(s => {
            appendUniqueBlock(getDeepMarkdown(s));
        });
    } else {
        const streamers = filterTopLevelNodes(querySelectorAllDeep(turnEl, 'ucs-text-streamer'));
        if (streamers.length > 0) {
            streamers.forEach(s => {
                appendUniqueBlock(getDeepMarkdown(s));
            });
        } else {
            rawText = getDeepMarkdown(turnEl);
        }
    }

    const busyElements = querySelectorAllDeep(
        turnEl,
        '[aria-busy="true"], [role="progressbar"], ucs-lottie-animation, .working-on-it-spark'
    ).filter(el => el.offsetParent !== null || (typeof el.getClientRects === 'function' && el.getClientRects().length > 0));

    // Detect assistant-generated images/canvas inside turnEl (excluding user-uploaded images, skill/citation badges, and UI icons)
    const MIN_GEN_IMG_DIM = 200;
    function isExcludedImageContext(el) {
        let cur = el;
        while (cur && cur !== turnEl) {
            if (cur.nodeType === Node.ELEMENT_NODE) {
                const c = (typeof cur.className === 'string') ? cur.className.toLowerCase() : '';
                const t = (cur.tagName || '').toLowerCase();
                const role = (cur.getAttribute && cur.getAttribute('data-message-author-role')) || '';
                if (
                    role === 'user' ||
                    t.includes('user-query') ||
                    t.includes('skill') ||
                    t.includes('citation') ||
                    t.includes('source') ||
                    t.includes('suggestion') ||
                    t.includes('followup') ||
                    t.includes('thinking') ||
                    t.includes('icon') ||
                    t.includes('button') ||
                    t.includes('chip') ||
                    [
                        'question-block', 'question-wrapper', 'user-query', 'user-message',
                        'skill-used', 'skill-chip', 'skill-badge', 'skill-card', 'skill-invocation', 'skill-header',
                        'citation', 'source', 'grounding', 'reference', 'chip', 'badge',
                        'avatar', 'favicon', 'icon', 'logo', 'spark', 'emoji',
                        'thought-bubble', 'thinking', 'work-status', 'working-on-it', 'diagnostic',
                        'suggested-question', 'suggestions', 'followup', 'follow-up',
                        'code-header', 'table-actions', 'toolbar', 'menu'
                    ].some(k => c.includes(k))
                ) {
                    return true;
                }
            }
            cur = cur.parentNode || cur.host;
        }
        return false;
    }

    function isUiIconUrlOrMeta(src, alt, cls) {
        const s = (src || '').toLowerCase();
        const a = (alt || '').toLowerCase();
        const c = (cls || '').toLowerCase();
        if (s.startsWith('data:image/svg') || s.endsWith('.svg') || s.includes('.svg?')) return true;
        if (['gstatic.com/images/branding', 'ssl.gstatic.com/ui', 'www.google.com/s2/favicons'].some(p => s.includes(p))) return true;
        if (['avatar', 'favicon', 'spark', 'emoji', 'icon', 'logo', 'badge', 'skill', 'citation', 'source-icon', 'cloud-icon'].some(k => c.includes(k) || a.includes(k))) return true;
        return false;
    }

    const allMedia = querySelectorAllDeep(turnEl, 'img, canvas, [style*="background-image"]');
    const generatedImgs = [];

    for (const el of allMedia) {
        if (isExcludedImageContext(el)) continue;
        const tag = (el.tagName || '').toLowerCase();
        const rect = (typeof el.getBoundingClientRect === 'function') ? el.getBoundingClientRect() : { width: 0, height: 0 };
        const cls = (typeof el.className === 'string') ? el.className.toLowerCase() : '';

        if (tag === 'img') {
            const src = (el.currentSrc || el.getAttribute('src') || el.getAttribute('data-src') || '').trim();
            const alt = (el.getAttribute('alt') || '').toLowerCase();
            if (isUiIconUrlOrMeta(src, alt, cls)) continue;

            const nw = el.naturalWidth || 0;
            const nh = el.naturalHeight || 0;
            const w = Math.max(nw, rect.width || el.width || 0);
            const h = Math.max(nh, rect.height || el.height || 0);

            // Reject any loaded image whose natural or rendered dimensions are smaller than MIN_GEN_IMG_DIM (e.g. 64x64 cloud icons)
            if (el.complete && nw > 0 && nh > 0 && (nw < MIN_GEN_IMG_DIM || nh < MIN_GEN_IMG_DIM)) continue;
            if (el.complete && (w < MIN_GEN_IMG_DIM || h < MIN_GEN_IMG_DIM)) continue;

            if (!src || !el.complete || nw === 0) {
                continue;
            }
            if (w >= MIN_GEN_IMG_DIM && h >= MIN_GEN_IMG_DIM) {
                generatedImgs.push({ el, sig: src.slice(0, 48) + ':' + w + 'x' + h });
            }
        } else if (tag === 'canvas') {
            const w = Math.max(el.width || 0, rect.width || 0);
            const h = Math.max(el.height || 0, rect.height || 0);
            if (w >= MIN_GEN_IMG_DIM && h >= MIN_GEN_IMG_DIM) {
                generatedImgs.push({ el, sig: 'canvas:' + w + 'x' + h });
            }
        } else {
            const bg = (el.style && el.style.backgroundImage) || '';
            if (bg.toLowerCase().includes('url(')) {
                const bgUrl = bg.replace(/^.*url\\(\\s*["']?/i, '').replace(/["']?\\s*\\).*$/i, '').trim();
                if (bgUrl && !isUiIconUrlOrMeta(bgUrl, '', cls)) {
                    const w = rect.width || el.clientWidth || 0;
                    const h = rect.height || el.clientHeight || 0;
                    if (w >= MIN_GEN_IMG_DIM && h >= MIN_GEN_IMG_DIM) {
                        generatedImgs.push({ el, sig: 'bg:' + bgUrl.slice(0, 48) + ':' + w + 'x' + h });
                    }
                }
            }
        }
    }

    const imageSignature = generatedImgs.map(item => item.sig).join('|');

    const hasActiveWork = busyElements.length > 0;

    return {
        isGenerating: hasStop || hasStopIcon,
        hasActiveWork,
        text: rawText,
        length: rawText.length,
        imageCount: generatedImgs.length,
        imageSignature
    };
}"""

JS_EXTRACT_TURN_IMAGES = """async (turnEl) => {
    function querySelectorAllDeep(root, selector) {
        const results = [];
        const visited = new Set();
        function walk(node) {
            if (!node || visited.has(node)) return;
            visited.add(node);
            if (node.nodeType === Node.ELEMENT_NODE || node.nodeType === Node.DOCUMENT_NODE || node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                if (node.querySelectorAll) {
                    try {
                        node.querySelectorAll(selector).forEach(el => {
                            if (!results.includes(el)) results.push(el);
                        });
                    } catch (e) {}
                }
                if (node.shadowRoot) walk(node.shadowRoot);
                if (node.tagName && node.tagName.toLowerCase() === 'slot' && typeof node.assignedNodes === 'function') {
                    try {
                        node.assignedNodes({ flatten: true }).forEach(n => walk(n));
                    } catch (e) {}
                }
                if (node.tagName && node.tagName.toLowerCase() === 'iframe') {
                    try {
                        if (node.contentDocument) walk(node.contentDocument);
                    } catch (e) {}
                }
                if (node.children) {
                    for (let child of node.children) walk(child);
                }
            }
        }
        walk(root);
        return results;
    }

    const MIN_GEN_IMG_DIM = 200;
    function isExcludedImageContext(el) {
        let cur = el;
        while (cur && cur !== turnEl) {
            if (cur.nodeType === Node.ELEMENT_NODE) {
                const c = (typeof cur.className === 'string') ? cur.className.toLowerCase() : '';
                const t = (cur.tagName || '').toLowerCase();
                const role = (cur.getAttribute && cur.getAttribute('data-message-author-role')) || '';
                if (
                    role === 'user' ||
                    t.includes('user-query') ||
                    t.includes('skill') ||
                    t.includes('citation') ||
                    t.includes('source') ||
                    t.includes('suggestion') ||
                    t.includes('followup') ||
                    t.includes('thinking') ||
                    t.includes('icon') ||
                    t.includes('button') ||
                    t.includes('chip') ||
                    [
                        'question-block', 'question-wrapper', 'user-query', 'user-message',
                        'skill-used', 'skill-chip', 'skill-badge', 'skill-card', 'skill-invocation', 'skill-header',
                        'citation', 'source', 'grounding', 'reference', 'chip', 'badge',
                        'avatar', 'favicon', 'icon', 'logo', 'spark', 'emoji',
                        'thought-bubble', 'thinking', 'work-status', 'working-on-it', 'diagnostic',
                        'suggested-question', 'suggestions', 'followup', 'follow-up',
                        'code-header', 'table-actions', 'toolbar', 'menu'
                    ].some(k => c.includes(k))
                ) {
                    return true;
                }
            }
            cur = cur.parentNode || cur.host;
        }
        return false;
    }

    function isUiIconUrlOrMeta(src, alt, cls) {
        const s = (src || '').toLowerCase();
        const a = (alt || '').toLowerCase();
        const c = (cls || '').toLowerCase();
        if (s.startsWith('data:image/svg') || s.endsWith('.svg') || s.includes('.svg?')) return true;
        if (['gstatic.com/images/branding', 'ssl.gstatic.com/ui', 'www.google.com/s2/favicons'].some(p => s.includes(p))) return true;
        if (['avatar', 'favicon', 'spark', 'emoji', 'icon', 'logo', 'badge', 'skill', 'citation', 'source-icon', 'cloud-icon'].some(k => c.includes(k) || a.includes(k))) return true;
        return false;
    }

    async function blobToDataUrl(blob) {
        if (!blob || blob.size === 0) return null;
        return await new Promise((resolve) => {
            const reader = new FileReader();
            reader.onloadend = () => resolve(reader.result);
            reader.onerror = () => resolve(null);
            reader.readAsDataURL(blob);
        });
    }

    async function urlToDataUrl(src) {
        if (!src) return null;
        if (src.startsWith('data:image/')) return src;
        for (const opts of [{ credentials: 'same-origin' }, { credentials: 'include' }, { credentials: 'omit' }]) {
            try {
                const resp = await fetch(src, opts);
                if (resp.ok) {
                    const blob = await resp.blob();
                    const dataUrl = await blobToDataUrl(blob);
                    if (dataUrl && dataUrl.startsWith('data:image/')) return dataUrl;
                }
            } catch (e) {}
        }
        return null;
    }

    async function imgToDataUrl(img) {
        const src = (img.currentSrc || img.src || img.getAttribute('src') || img.getAttribute('data-src') || '').trim();
        const fetched = await urlToDataUrl(src);
        if (fetched) return fetched;
        try {
            const canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth || img.width || 512;
            canvas.height = img.naturalHeight || img.height || 512;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0);
            return canvas.toDataURL('image/png');
        } catch (e) {
            return null;
        }
    }

    const allMedia = querySelectorAllDeep(turnEl, 'img, canvas, [style*="background-image"]');
    const results = [];
    const seenKeys = new Set();
    let extractIdx = 0;

    for (const el of allMedia) {
        if (isExcludedImageContext(el)) continue;
        const tag = (el.tagName || '').toLowerCase();
        const rect = (typeof el.getBoundingClientRect === 'function') ? el.getBoundingClientRect() : { width: 0, height: 0 };
        const cls = (typeof el.className === 'string') ? el.className.toLowerCase() : '';

        if (tag === 'img') {
            const src = (el.currentSrc || el.getAttribute('src') || el.getAttribute('data-src') || '').trim();
            if (!src || seenKeys.has(src)) continue;
            const alt = (el.getAttribute('alt') || '').toLowerCase();
            if (isUiIconUrlOrMeta(src, alt, cls)) continue;

            const nw = el.naturalWidth || 0;
            const nh = el.naturalHeight || 0;
            const w = Math.max(nw, rect.width || el.width || 0);
            const h = Math.max(nh, rect.height || el.height || 0);
            if (nw > 0 && nh > 0 && (nw < MIN_GEN_IMG_DIM || nh < MIN_GEN_IMG_DIM)) continue;
            if (w < MIN_GEN_IMG_DIM || h < MIN_GEN_IMG_DIM) continue;

            seenKeys.add(src);
            const extractId = 'ge-img-' + (++extractIdx) + '-' + Date.now();
            try { el.setAttribute('data-ge-extract-id', extractId); } catch (e) {}
            try { el.scrollIntoView({ block: 'nearest' }); } catch (e) {}
            const dataUrl = await imgToDataUrl(el);
            results.push({
                extractId,
                tag: 'img',
                src,
                dataUrl: (dataUrl && dataUrl.startsWith('data:image/')) ? dataUrl : null,
                alt: el.getAttribute('alt') || 'Generated Image',
                width: w,
                height: h
            });
        } else if (tag === 'canvas') {
            const w = Math.max(el.width || 0, rect.width || 0);
            const h = Math.max(el.height || 0, rect.height || 0);
            if (w < MIN_GEN_IMG_DIM || h < MIN_GEN_IMG_DIM) continue;
            const extractId = 'ge-img-' + (++extractIdx) + '-' + Date.now();
            try { el.setAttribute('data-ge-extract-id', extractId); } catch (e) {}
            try { el.scrollIntoView({ block: 'nearest' }); } catch (e) {}
            let dataUrl = null;
            try { dataUrl = el.toDataURL('image/png'); } catch (e) {}
            results.push({
                extractId,
                tag: 'canvas',
                src: '',
                dataUrl: (dataUrl && dataUrl.startsWith('data:image/')) ? dataUrl : null,
                alt: 'Generated Image',
                width: w,
                height: h
            });
        } else {
            const bg = (el.style && el.style.backgroundImage) || '';
            if (bg.toLowerCase().includes('url(')) {
                const src = bg.replace(/^.*url\\(\\s*["']?/i, '').replace(/["']?\\s*\\).*$/i, '').trim();
                if (src && !isUiIconUrlOrMeta(src, '', cls) && !seenKeys.has(src)) {
                    const w = rect.width || el.clientWidth || 0;
                    const h = rect.height || el.clientHeight || 0;
                    if (w >= MIN_GEN_IMG_DIM && h >= MIN_GEN_IMG_DIM) {
                        seenKeys.add(src);
                        const extractId = 'ge-img-' + (++extractIdx) + '-' + Date.now();
                        try { el.setAttribute('data-ge-extract-id', extractId); } catch (e) {}
                        try { el.scrollIntoView({ block: 'nearest' }); } catch (e) {}
                        const dataUrl = await urlToDataUrl(src);
                        results.push({
                            extractId,
                            tag: 'bg',
                            src,
                            dataUrl: (dataUrl && dataUrl.startsWith('data:image/')) ? dataUrl : null,
                            alt: 'Generated Image',
                            width: w,
                            height: h
                        });
                    }
                }
            }
        }
    }

    // Fallback scan: if no standard <img>/<canvas> matched, inspect all elements in turnEl for computed background-image or large media containers
    if (results.length === 0) {
        const allEls = querySelectorAllDeep(turnEl, '*');
        for (const el of allEls) {
            if (isExcludedImageContext(el)) continue;
            const rect = (typeof el.getBoundingClientRect === 'function') ? el.getBoundingClientRect() : { width: 0, height: 0 };
            const w = Math.round(rect.width || 0);
            const h = Math.round(rect.height || 0);
            if (w < MIN_GEN_IMG_DIM || h < MIN_GEN_IMG_DIM) continue;
            let csBg = '';
            try { csBg = (window.getComputedStyle(el).backgroundImage || ''); } catch (e) {}
            if (csBg && csBg !== 'none' && csBg.toLowerCase().includes('url(')) {
                const src = csBg.replace(/^.*url\\(\\s*["']?/i, '').replace(/["']?\\s*\\).*$/i, '').trim();
                if (src && !src.startsWith('data:image/svg') && !src.endsWith('.svg') && !seenKeys.has(src)) {
                    seenKeys.add(src);
                    const extractId = 'ge-img-' + (++extractIdx) + '-' + Date.now();
                    try { el.setAttribute('data-ge-extract-id', extractId); } catch (e) {}
                    try { el.scrollIntoView({ block: 'nearest' }); } catch (e) {}
                    const dataUrl = await urlToDataUrl(src);
                    results.push({
                        extractId,
                        tag: 'computed-bg:' + (el.tagName || '').toLowerCase(),
                        src,
                        dataUrl: (dataUrl && dataUrl.startsWith('data:image/')) ? dataUrl : null,
                        alt: 'Generated Image',
                        width: w,
                        height: h
                    });
                    continue;
                }
            }
            const tag = (el.tagName || '').toLowerCase();
            const cls = (typeof el.className === 'string') ? el.className.toLowerCase() : '';
            if (
                tag.includes('image') ||
                tag.includes('imagen') ||
                tag.includes('media') ||
                tag === 'figure' ||
                cls.includes('generated-image') ||
                cls.includes('image-card') ||
                cls.includes('media-card')
            ) {
                const key = tag + ':' + w + 'x' + h;
                if (!seenKeys.has(key)) {
                    seenKeys.add(key);
                    const extractId = 'ge-img-' + (++extractIdx) + '-' + Date.now();
                    try { el.setAttribute('data-ge-extract-id', extractId); } catch (e) {}
                    try { el.scrollIntoView({ block: 'nearest' }); } catch (e) {}
                    results.push({
                        extractId,
                        tag: 'container:' + tag,
                        src: '',
                        dataUrl: null,
                        alt: 'Generated Image',
                        width: w,
                        height: h
                    });
                }
            }
        }
    }
    return results;
}"""


def _unescape_prose_underscores(text: str) -> str:
    r"""Unescape Markdown `\_` in plain prose while preserving code blocks and `<tool_call>` JSON."""
    if not text or r"\_" not in text:
        return text
    out = []
    last_end = 0
    for m in PROTECTED_BLOCKS_RE.finditer(text):
        out.append(text[last_end : m.start()].replace(r"\_", "_"))
        out.append(m.group(0))
        last_end = m.end()
    out.append(text[last_end:].replace(r"\_", "_"))
    return "".join(out)


def _clean_unprotected_prose_segment(seg: str) -> str:
    """Clean portal UI noise from unprotected prose without touching code blocks, inline code, or tool calls."""
    if not seg:
        return ""
    seg = re.sub(r"(?m)^\[\[section:[^\]]*\]\]\r?\n?", "", seg)
    seg = DATAPART_JSON_RE.sub("", seg)
    seg = re.sub(r"(?im)^[ \t]*Working on the request[ \t]*(?:\(\d+\s*s\))?[ \t]*\r?\n?", "", seg)
    seg = re.sub(r"(?i)\bWorking on the request[ \t]*\(\d+\s*s\)[ \t]*", "", seg)
    # Strip portal "Skill used<Name>" badge lines and preceding internal skill-load narration
    seg = re.sub(r"(?im)^[ \t]*Skill used[ \t]*[A-Za-z0-9_.:-]*[ \t]*\r?\n?", "", seg)
    seg = re.sub(r"(?im)^[ \t]*I will load the [`'\"]?universal[`'\"]? skill[^\r\n]*\r?\n?", "", seg)
    seg = re.sub(r"(?m)^([ \t]*)`{3,}(?=[A-Z][a-zA-Z]*[ \t]+\w)", r"\1", seg)
    seg = re.sub(r"(?m)^[ \t]*(?:`{3,})?['\"\]\[\{\}]{3,}[ \t]*\r?\n?", "", seg)
    return seg.replace(r"\_", "_")


def clean_response_text(raw_text: str) -> str:
    """
    Cleans up structural artifacts and portal-injected suffixes while preserving code blocks and inline code.
    Applies portal tag/heading stripping strictly to unprotected prose segments so literal mentions
    of `<a2a_datapart_json>` or `## Suggestions` inside tables/code/tests never truncate the response!
    """
    if not raw_text:
        return ""
    text = raw_text.replace("\r", "")
    # 0. Strip portal's leading internal skill-load narration line when followed by [[section:...]] or Skill used
    text = re.sub(
        r"(?im)^\s*(?:i(?:'ll| will)|let me)\s+load\s+the\s+[`'\"]?universal[`'\"]?\s+skill[^\n]*\n+(?=\s*(?:\[\[section:|Skill used))",
        "",
        text,
    )
    # 1. Strip portal-injected ```suggested_questions blocks at line start
    text = re.sub(r"(?m)^```suggested_questions\b[\s\S]*?^```\s*", "", text, flags=re.IGNORECASE)
    # 2. Clean unprotected prose segments while leaving fenced blocks, inline code, and <tool_call> intact
    segments: List[Tuple[bool, str]] = []
    last_end = 0
    for m in PROTECTED_BLOCKS_RE.finditer(text):
        if m.start() > last_end:
            segments.append((False, _clean_unprotected_prose_segment(text[last_end : m.start()])))
        segments.append((True, m.group(0)))
        last_end = m.end()
    if last_end < len(text):
        segments.append((False, _clean_unprotected_prose_segment(text[last_end:])))
    # 3. Strip trailing `# Suggestions` / `## Suggestions` section only when on its own heading line in final prose
    if segments:
        is_prot, tail_text = segments[-1]
        if not is_prot:
            cleaned_tail = re.sub(
                r"(?im)(?:\n+|\A)[ \t]*#+\s*suggestions[ \t]*\n[\s\S]*\Z",
                "",
                tail_text,
            )
            segments[-1] = (False, cleaned_tail)
    text = "".join(seg for _, seg in segments)
    text = re.sub(r"(?m)^[ \t]+$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse consecutive identical single-line paragraphs (e.g. duplicate 'Image generated by Nano Banana 2.' captions)
    text = re.sub(r"(?m)^([^\n`]{3,160})(?:\n[ \t]*)+\1(?=\n|$)", r"\1", text)
    return text.strip()


def sanitize_outgoing_prompt(prompt_text: str) -> str:
    """
    Neutralize literal portal directives in user/assistant prompts before pasting into the web UI
    so the Gemini Enterprise frontend doesn't swallow sections or suggested_questions blocks!
    """
    if not prompt_text:
        return ""
    # Insert zero-width space inside [[section: and ```suggested_questions so web UI doesn't parse them as widgets
    sanitized = prompt_text.replace("[[section:", "[\u200b[section:")
    sanitized = sanitized.replace("```suggested_questions", "```\u200bsuggested_questions")
    return sanitized


def estimate_tokens(text: str) -> int:
    """Estimate OpenAI token count (~4 chars per token) so IDE context managers don't compact early."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# ==============================================================================
# PYDANTIC SCHEMAS (FULL HARNESS / IDE COMPATIBILITY)
# ==============================================================================
class Message(BaseModel):
    role: str
    content: Optional[Union[str, List[Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class FunctionDef(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None


class Tool(BaseModel):
    type: str = "function"
    function: FunctionDef


class ChatRequest(BaseModel):
    model: Optional[str] = "gemini-enterprise"
    messages: List[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Any] = None
    parallel_tool_calls: Optional[bool] = True
    response_format: Optional[Dict[str, Any]] = None
    stream_options: Optional[Dict[str, Any]] = None
    user: Optional[str] = None
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None


# ==============================================================================
# MULTIMODAL & MESSAGE HELPERS
# ==============================================================================
def get_content_text(content: Any) -> str:
    """Extract text from string or OpenAI multimodal content array (including file/document parts)."""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                ctype = c.get("type", "")
                if ctype in ("text", "input_text"):
                    parts.append(c.get("text", ""))
                elif ctype in ("file", "document", "input_file"):
                    file_info = c.get("file") or c.get("document") or c
                    fname = file_info.get("filename") or file_info.get("name") or "attachment"
                    fdata = file_info.get("file_data") or file_info.get("data") or file_info.get("content") or ""
                    if fdata.startswith("data:") and ";base64," in fdata:
                        try:
                            b64_part = fdata.split(";base64,", 1)[1]
                            decoded = base64.b64decode(b64_part).decode("utf-8", errors="replace")
                            parts.append(f"\n--- Attached File: {fname} ---\n{decoded}\n--- End of {fname} ---\n")
                        except Exception:
                            pass
                    elif isinstance(fdata, str) and fdata:
                        parts.append(f"\n--- Attached File: {fname} ---\n{fdata}\n--- End of {fname} ---\n")
        return "\n".join(parts)
    return ""


def extract_images_from_messages(messages: List[Message]) -> List[Tuple[str, bytes]]:
    """Extract base64 data-URI images from the latest user turn for browser upload."""
    images: List[Tuple[str, bytes]] = []
    for msg in reversed(messages):
        if msg.role == "user":
            if isinstance(msg.content, list):
                for idx, item in enumerate(msg.content):
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        url = item.get("image_url", {}).get("url", "")
                        match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", url)
                        if match:
                            mime_type, b64_data = match.groups()
                            ext = mime_type.split("/")[-1].split("+")[0]
                            try:
                                raw_bytes = base64.b64decode(b64_data)
                                images.append((f"upload_{idx}.{ext}", raw_bytes))
                            except Exception:
                                pass
            break
    return images


def hash_message(msg: Message) -> str:
    """Compute a deterministic hash for a single message to track conversation delta state."""
    normalized_tool_calls = None
    if msg.tool_calls:
        normalized_tool_calls = []
        for tc in msg.tool_calls:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            raw_args = fn.get("arguments", "{}")
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                parsed_args = raw_args
            normalized_tool_calls.append({
                "name": fn.get("name", ""),
                "arguments": parsed_args,
            })
    raw_content = "" if (msg.role == "assistant" and normalized_tool_calls) else get_content_text(msg.content)
    normalized_content = re.sub(r"\s+", " ", raw_content).strip()
    payload = {
        "role": msg.role,
        "content": normalized_content,
        "tool_call_id": msg.tool_call_id,
        "name": msg.name,
        "tool_calls": normalized_tool_calls,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def is_history_continuation(
    prev_hashes: List[str],
    current_hashes: List[str],
    messages: List[Message],
) -> bool:
    """
    True when `current_hashes` is an append-only continuation of `prev_hashes`.
    Tolerates client-side normalization or truncation of the last assistant turn at `len(prev_hashes) - 1`.
    """
    if not prev_hashes or len(current_hashes) < len(prev_hashes):
        return False
    if current_hashes[: len(prev_hashes)] == prev_hashes:
        return True
    if (
        len(prev_hashes) >= 2
        and current_hashes[: len(prev_hashes) - 1] == prev_hashes[: len(prev_hashes) - 1]
        and messages[len(prev_hashes) - 1].role == "assistant"
    ):
        return True
    return False


def hash_tools(tools: Optional[List[Tool]], tool_choice: Optional[Any] = None) -> str:
    """Compute a deterministic hash for the toolset to detect mid-session tool changes."""
    if not tools or tool_choice == "none":
        return "none"
    serialized = [
        {
            "name": t.function.name,
            "description": t.function.description,
            "parameters": t.function.parameters,
        }
        for t in tools
    ]
    return hashlib.sha256(json.dumps(serialized, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def get_session_id(req: ChatRequest, request: Request) -> str:
    """
    Resolve a stable browser-tab session ID when switching between sessions in Hermes / OpenCode / Cursor:
    1. Explicit session ID header/body field if provided by the harness.
    2. Longest message-hash prefix lineage match against active `session_history_hashes` tabs.
    3. Fallback to deterministic hash of system prompt + first user message.
    """
    candidates = [
        req.session_id,
        req.conversation_id,
        req.user,
        request.headers.get("x-session-id"),
        request.headers.get("x-conversation-id"),
        request.headers.get("x-opencode-session-id"),
        request.headers.get("x-thread-id"),
    ]
    explicit_key = next((str(value).strip() for value in candidates if value and str(value).strip()), None)
    if explicit_key:
        digest = hashlib.sha256(explicit_key.encode("utf-8")).hexdigest()[:24]
        return f"chat-{digest}"

    # Lineage matching: match incoming message hash prefix against existing active sessions
    incoming_hashes = [hash_message(m) for m in req.messages]
    best_sid = None
    best_match_len = 0

    for sid, prev_hashes in list(session_history_hashes.items()):
        if not prev_hashes:
            continue
        # Case A: Incoming messages are an append-only continuation of `sid`
        if is_history_continuation(prev_hashes, incoming_hashes, req.messages):
            if len(prev_hashes) > best_match_len:
                best_match_len = len(prev_hashes)
                best_sid = sid

    if best_sid:
        return best_sid

    # Fallback anchor: system prompt + first user message
    sys_msg = next((m for m in req.messages if m.role in ("system", "developer")), None)
    first_user = next((m for m in req.messages if m.role == "user"), None)
    sys_text = get_content_text(sys_msg.content)[:256] if sys_msg else ""
    user_text = get_content_text(first_user.content) if first_user else ""
    client_key = f"{sys_text}||{user_text}".strip()

    if not client_key:
        client_key = json.dumps(
            [{"role": m.role, "content": get_content_text(m.content)} for m in req.messages],
            sort_keys=True,
        )

    digest = hashlib.sha256(client_key.encode("utf-8")).hexdigest()[:24]
    candidate_sid = f"chat-{digest}"

    # Avoid colliding with an already multi-turn session if the user starts a brand-new 1-turn chat with the same greeting
    user_turn_count = sum(1 for m in req.messages if m.role == "user")
    if (
        user_turn_count <= 1
        and candidate_sid in session_history_hashes
        and len(session_history_hashes[candidate_sid]) > len(incoming_hashes)
    ):
        fork_digest = hashlib.sha256(f"{client_key}||{time.time()}".encode("utf-8")).hexdigest()[:24]
        return f"chat-{fork_digest}"

    return candidate_sid


# ==============================================================================
# TOOL CALLING PROMPT & PARSER
# ==============================================================================
def format_tools_prompt(
    tools: Optional[List[Tool]],
    tool_choice: Optional[Any] = None,
    parallel_tool_calls: bool = True,
) -> str:
    """Serialize full JSON Schema definitions and tool_choice rules for IDE harnesses."""
    if not tools or tool_choice == "none":
        return ""

    lines = [
        "## MANDATORY TOOL CALLING SYSTEM",
        "You have access to the local environment, IDE, and filesystem through the tools defined below.",
        "When you need to read, write, create, edit files, search, or execute commands, you MUST call the appropriate tool.",
        "",
        "### OUTPUT FORMAT RULES:",
        "To execute a tool call, output valid JSON inside `<tool_call>` tags wrapped in a ```xml code block (so Markdown never alters underscores, asterisks, or backslashes inside JSON strings):",
        "```xml",
        "<tool_call>",
        '{"name": "tool_name", "arguments": {"param_key": "param_value"}}',
        "</tool_call>",
        "```",
        "",
        "- Always place `<tool_call>` and `</tool_call>` on their own lines.",
        "- Always provide complete, valid JSON arguments matching the tool's parameter schema.",
        "- Use `<tool_call>` ONLY for the external client tools explicitly listed under `DEFINITIONS OF AVAILABLE TOOLS` below. NEVER wrap built-in portal capabilities or specialist agent handoffs (e.g., `transfer_to_agent`, `imagen_agent`, `videogen_agent`, `load_skill`) in `<tool_call>` tags.",
        "- If a JSON string argument must mention literal `<tool_call>` or `</tool_call>` tags, escape the angle brackets as `\\u003ctool_call\\u003e` and `\\u003c/tool_call\\u003e` (never escape backticks as `\\u0060`; normal backticks are valid inside JSON strings).",
        "- Never output placeholders like `USE_CODE_BLOCK_ABOVE`.",
    ]

    if parallel_tool_calls:
        lines.append("- You may emit multiple `<tool_call>...</tool_call>` blocks in a single turn if multiple independent actions are needed, then stop and wait for the results.")
    else:
        lines.append("- Call only ONE tool at a time, then stop and wait for the tool output.")

    if tool_choice == "required":
        lines.append("- CRITICAL REQUIREMENT: `tool_choice` is set to `required`. You MUST output at least one `<tool_call>` in this response.")
    elif isinstance(tool_choice, dict):
        forced_name = tool_choice.get("function", {}).get("name") or tool_choice.get("name")
        if forced_name:
            lines.append(f"- CRITICAL REQUIREMENT: You MUST call the tool `{forced_name}` in this response.")

    lines.append("\n### DEFINITIONS OF AVAILABLE TOOLS:")
    for tool in tools:
        func = tool.function
        schema_str = json.dumps(func.parameters or {"type": "object", "properties": {}}, ensure_ascii=False, indent=2)
        lines.append(f"\n#### Tool: `{func.name}`")
        if func.description:
            lines.append(f"Description: {func.description}")
        lines.append(f"Parameters JSON Schema:\n{schema_str}")

    return "\n".join(lines)


def build_tool_id_map(messages: List[Message]) -> Dict[str, str]:
    """Map tool_call_id -> function name across all assistant messages in the conversation."""
    id_map: Dict[str, str] = {}
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                tc_id = tc.get("id")
                fn_name = tc.get("function", {}).get("name")
                if tc_id and fn_name:
                    id_map[tc_id] = fn_name
    return id_map


def format_single_message(msg: Message, tool_id_map: Dict[str, str], max_tool_chars: int = MAX_SINGLE_TOOL_CHARS) -> str:
    """Format a single OpenAI message consistently with our `<tool_call>` protocol."""
    role = msg.role
    content = get_content_text(msg.content)

    if role in ("system", "developer"):
        return f"System Instructions:\n{content}"
    elif role == "user":
        return f"User:\n{content}"
    elif role == "assistant":
        parts = []
        if content:
            parts.append(content)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "unknown_tool")
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    parsed_args = raw_args
                call_json = json.dumps({"name": name, "arguments": parsed_args}, ensure_ascii=False)
                parts.append(f"```xml\n<tool_call>\n{call_json}\n</tool_call>\n```")
        body = "\n".join(parts)
        return f"Assistant:\n{body}"
    elif role == "tool":
        tool_name = msg.name or (tool_id_map.get(msg.tool_call_id, "tool") if msg.tool_call_id else "tool")
        id_suffix = f" [call_id: {msg.tool_call_id}]" if msg.tool_call_id else ""
        if max_tool_chars and len(content) > max_tool_chars:
            head_len = int(max_tool_chars * 0.75)
            tail_len = max_tool_chars - head_len
            omitted = len(content) - max_tool_chars
            content = (
                f"{content[:head_len]}\n\n"
                f"[... {omitted} chars truncated to fit portal single-message size limit ...]\n\n"
                f"{content[-tail_len:]}"
            )
        return f"Tool Result ({tool_name}{id_suffix}):\n{content}"
    return f"{role.capitalize()}:\n{content}"


def _compact_full_sync_messages(
    messages: List[Message],
    tool_id_map: Dict[str, str],
    header_parts: List[str],
    footer_parts: List[str],
    max_total_chars: int = MAX_FULL_SYNC_CHARS,
) -> List[str]:
    """
    Ensure a full-transcript sync (NEW or RESET+FULL) never exceeds the Gemini web portal's
    single-message character limit (~80k chars), which otherwise triggers:
    'This request exceeds the size limit. Try starting a new conversation...'
    """
    raw_msg_strs = [format_single_message(m, tool_id_map) for m in messages]
    fixed_len = sum(len(p) + 2 for p in header_parts) + sum(len(p) + 2 for p in footer_parts)
    total_len = fixed_len + sum(len(s) + 2 for s in raw_msg_strs)
    if total_len <= max_total_chars:
        return raw_msg_strs

    # Pass 1: Aggressively compact older tool/assistant messages outside the last 6 messages
    recent_keep = min(6, len(messages))
    cutoff = len(messages) - recent_keep
    compacted_strs = list(raw_msg_strs)
    for i in range(cutoff):
        m = messages[i]
        if m.role not in ("system", "developer") and len(compacted_strs[i]) > 1500:
            s = compacted_strs[i]
            compacted_strs[i] = f"{s[:1000]}\n[... {len(s) - 1300} chars compacted ...]\n{s[-300:]}"

    total_len = fixed_len + sum(len(s) + 2 for s in compacted_strs)
    if total_len <= max_total_chars:
        return compacted_strs

    # Pass 2: Keep system/developer messages + as many of the most recent messages as fit budget
    sys_indices = [i for i, m in enumerate(messages) if m.role in ("system", "developer")]
    other_indices = [i for i, m in enumerate(messages) if m.role not in ("system", "developer")]

    sys_strs = [compacted_strs[i] for i in sys_indices]
    budget_left = max_total_chars - fixed_len - sum(len(s) + 2 for s in sys_strs) - 160

    kept_tail_rev: List[str] = []
    for idx in reversed(other_indices):
        s = compacted_strs[idx]
        if not kept_tail_rev and len(s) > max(2000, budget_left):
            # Even the very last message is huge; cap its middle so it fits
            keep_c = max(2000, budget_left)
            h_c = int(keep_c * 0.7)
            t_c = keep_c - h_c
            s = f"{s[:h_c]}\n\n[... {len(s) - keep_c} chars truncated to fit portal limit ...]\n\n{s[-t_c:]}"
        if len(s) + 2 <= budget_left or not kept_tail_rev:
            kept_tail_rev.append(s)
            budget_left -= len(s) + 2
        else:
            break

    kept_tail = list(reversed(kept_tail_rev))
    omitted_count = len(other_indices) - len(kept_tail)
    if omitted_count > 0:
        notice = f"[System Note: {omitted_count} earlier conversation turns omitted during tab resync to fit portal message size limit.]"
        return sys_strs + [notice] + kept_tail
    return sys_strs + kept_tail


def prepare_turn_prompt(
    session_id: str,
    req: ChatRequest,
) -> Tuple[str, bool, List[str], str]:
    """
    Determine whether we can send only the delta messages to an existing browser tab
    or whether we need a fresh tab / full transcript sync.
    Returns: (prompt_text_to_send, needs_page_reset, new_history_hashes, new_tools_hash)
    """
    current_hashes = [hash_message(m) for m in req.messages]
    current_tools_hash = hash_tools(req.tools, req.tool_choice)
    prev_hashes = session_history_hashes.get(session_id, [])
    prev_tools_hash = session_tools_hashes.get(session_id, "none")
    tool_id_map = build_tool_id_map(req.messages)

    is_dirty = session_id in dirty_sessions

    is_prefix_match = (
        not is_dirty
        and len(prev_hashes) > 0
        and len(current_hashes) > len(prev_hashes)
        and is_history_continuation(prev_hashes, current_hashes, req.messages)
    )

    if is_prefix_match:
        delta_messages = req.messages[len(prev_hashes) :]
        if delta_messages and delta_messages[0].role == "assistant" and len(delta_messages) > 1:
            delta_messages = delta_messages[1:]
        formatted_parts = []

        if current_tools_hash != prev_tools_hash and req.tools and req.tool_choice != "none":
            formatted_parts.append(
                "### UPDATED TOOLS AVAILABLE FOR THIS SESSION:\n"
                + format_tools_prompt(
                    req.tools,
                    tool_choice=req.tool_choice,
                    parallel_tool_calls=req.parallel_tool_calls if req.parallel_tool_calls is not None else True,
                )
            )

        formatted_parts.extend(format_single_message(m, tool_id_map) for m in delta_messages)

        if req.tool_choice == "required":
            formatted_parts.append("Reminder: You MUST output a `<tool_call>` in this turn.")
        elif isinstance(req.tool_choice, dict):
            forced_name = req.tool_choice.get("function", {}).get("name") or req.tool_choice.get("name")
            if forced_name:
                formatted_parts.append(f"Reminder: You MUST call the `{forced_name}` tool in this turn.")
        if req.response_format and req.response_format.get("type") == "json_object":
            formatted_parts.append("CRITICAL: Output ONLY a valid JSON object.")

        return sanitize_outgoing_prompt("\n\n".join(formatted_parts)), False, current_hashes, current_tools_hash

    needs_reset = len(prev_hashes) > 0 or is_dirty
    header_parts = []
    footer_parts = []

    if req.tools and req.tool_choice != "none":
        header_parts.append(
            format_tools_prompt(
                req.tools,
                tool_choice=req.tool_choice,
                parallel_tool_calls=req.parallel_tool_calls if req.parallel_tool_calls is not None else True,
            )
        )

    if req.response_format and req.response_format.get("type") == "json_object":
        footer_parts.append("CRITICAL: Respond with ONLY valid JSON matching the requested schema. Do not wrap in markdown code blocks.")

    msg_parts = _compact_full_sync_messages(req.messages, tool_id_map, header_parts, footer_parts)
    formatted_parts = header_parts + msg_parts + footer_parts

    return sanitize_outgoing_prompt("\n\n".join(formatted_parts)), needs_reset, current_hashes, current_tools_hash


def _repair_invalid_json_escapes(s: str) -> str:
    r"""Escape lone backslashes that are not valid JSON escapes (e.g. `\_` from Markdown)."""
    return re.sub(r'(?<!\\)\\(?![/\"\\bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', s)


def _try_parse_json_object(raw_str: str) -> Optional[Any]:
    r"""Resiliently parse a JSON string that may contain markdown fences, invalid `\_` escapes, or unescaped newlines."""
    s = raw_str.strip()
    s = re.sub(r"^`{3,}(?:json|xml|tool_call)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*`{3,}$", "", s)
    s = s.strip()

    for candidate in (s, _repair_invalid_json_escapes(s)):
        try:
            return json.loads(candidate, strict=False)
        except Exception:
            pass

    first_brace = s.find("{")
    last_brace = s.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        sub = s[first_brace : last_brace + 1]
        for candidate in (sub, _repair_invalid_json_escapes(sub)):
            try:
                return json.loads(candidate, strict=False)
            except Exception:
                pass
    return None


def parse_tool_calls(response: str, allowed_tools: Optional[List[Tool]] = None) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """
    Extracts tool calls from the response text with resilient JSON recovery.
    Uses line-anchored `<tool_call>` matching first so inline `<tool_call>` inside prose or JSON strings never splits blocks.
    Returns: (cleaned_content_text, list_of_openai_tool_calls)
    """
    if not response:
        return "", None

    cleaned = _unescape_prose_underscores(response)
    raw_tools: List[Dict[str, Any]] = []
    allowed_names = {t.function.name for t in allowed_tools} if allowed_tools else None

    # 1. Primary: Line-anchored <tool_call> ... </tool_call> outside non-tool code blocks & inline code
    masked_cleaned = _mask_non_tool_code_blocks(cleaned)
    matches = list(LINE_XML_TOOL_CALL_RE.finditer(masked_cleaned))
    if not matches:
        fallback_xml_pattern = re.compile(
            r"(?m)^[ \t]*<tool_call>\s*([\s\S]*?)\s*</tool_call>[ \t]*$",
            re.IGNORECASE,
        )
        matches = list(fallback_xml_pattern.finditer(masked_cleaned))
    if matches:
        ranges_to_remove = []
        for m in matches:
            inner_raw = cleaned[m.start(1) : m.end(1)]
            parsed = _try_parse_json_object(inner_raw)
            if isinstance(parsed, dict) and "name" in parsed:
                tname = str(parsed.get("name", "")).strip()
                is_internal = tname in INTERNAL_PORTAL_TOOLS or tname.startswith("call:")
                if not is_internal and (not allowed_names or tname in allowed_names):
                    raw_tools.append(parsed)
                    ranges_to_remove.append(m.span())
                elif is_internal:
                    # Strip accidental portal-internal <tool_call> blocks from assistant prose
                    ranges_to_remove.append(m.span())
            elif isinstance(parsed, list):
                valid_items = []
                any_internal = False
                for item in parsed:
                    if isinstance(item, dict) and "name" in item:
                        tname = str(item.get("name", "")).strip()
                        is_internal = tname in INTERNAL_PORTAL_TOOLS or tname.startswith("call:")
                        if is_internal:
                            any_internal = True
                        elif not allowed_names or tname in allowed_names:
                            valid_items.append(item)
                if valid_items:
                    raw_tools.extend(valid_items)
                    ranges_to_remove.append(m.span())
                elif any_internal:
                    ranges_to_remove.append(m.span())

        for start, end in reversed(ranges_to_remove):
            cleaned = cleaned[:start] + cleaned[end:]
        cleaned = _clean_unprotected_prose_segment(cleaned) if "`" not in cleaned else cleaned
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    # 2. Fallback: ```tool_call or ```json containing {"name": ..., "arguments": ...}
    if not raw_tools:
        masked_cleaned = _mask_non_tool_code_blocks(cleaned)
        fence_pattern = re.compile(r"```(?:tool_call|json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)
        ranges_to_remove = []
        for m in fence_pattern.finditer(masked_cleaned):
            inner_raw = cleaned[m.start(1) : m.end(1)]
            parsed = _try_parse_json_object(inner_raw)
            if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
                if not allowed_names or parsed.get("name") in allowed_names:
                    raw_tools.append(parsed)
                    ranges_to_remove.append(m.span())
        for start, end in reversed(ranges_to_remove):
            cleaned = cleaned[:start] + cleaned[end:]
        cleaned = cleaned.strip()

    # 3. Fallback: Standalone {"tool_calls": [...]}
    if not raw_tools and '"tool_calls"' in cleaned:
        parsed = _try_parse_json_object(cleaned)
        if isinstance(parsed, dict) and isinstance(parsed.get("tool_calls"), list):
            for tc in parsed["tool_calls"]:
                if isinstance(tc, dict) and "name" in tc:
                    tname = str(tc.get("name", "")).strip()
                    is_internal = tname in INTERNAL_PORTAL_TOOLS or tname.startswith("call:")
                    if not is_internal and (not allowed_names or tname in allowed_names):
                        raw_tools.append(tc)
            if raw_tools:
                cleaned = ""

    if not raw_tools:
        return cleaned, None

    code_blocks = re.findall(r"```(?:\w+)?\n([\s\S]*?)```", response)

    formatted_tool_calls = []
    ts_ms = int(time.time() * 1000)
    for idx, tc in enumerate(raw_tools):
        name = tc.get("name")
        args = tc.get("arguments", {})
        if isinstance(args, str):
            parsed_args = _try_parse_json_object(args)
            args = parsed_args if isinstance(parsed_args, dict) else {"raw": args}
        elif args is None:
            args = {}

        if code_blocks and isinstance(args, dict):
            if args.get("content") == "USE_CODE_BLOCK_ABOVE" and len(code_blocks) >= 1:
                args["content"] = code_blocks[-1]
            if args.get("oldString") == "USE_OLD_CODE_ABOVE" and len(code_blocks) >= 2:
                args["oldString"] = code_blocks[-2]
                args["newString"] = code_blocks[-1]
            elif args.get("newString") == "USE_NEW_CODE_ABOVE" and len(code_blocks) >= 1:
                args["newString"] = code_blocks[-1]

        if any(
            existing["function"]["name"] == name
            and json.loads(existing["function"]["arguments"]) == args
            for existing in formatted_tool_calls
        ):
            continue

        formatted_tool_calls.append(
            {
                "id": f"call_{ts_ms}_{len(formatted_tool_calls)}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )

    return cleaned, formatted_tool_calls


# ==============================================================================
# BROWSER & SESSION POOL MANAGEMENT
# ==============================================================================
async def check_logged_in(page: Page, timeout: int = 5000) -> bool:
    try:
        await page.wait_for_selector("ucs-prosemirror-editor", timeout=timeout)
        return True
    except Exception:
        return False


def is_browser_closed_error(exc: BaseException) -> bool:
    """Return True if an exception indicates the Playwright driver, browser, context, or page is closed."""
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "connection closed",
            "target page, context or browser has been closed",
            "browser has been closed",
            "context or browser has been closed",
            "targetclosederror",
            "target closed",
            "socket.send() raised exception",
            "pipe is being closed",
            "broken pipe",
        )
    )


async def stop_active_generation(page: Page):
    """Click the Stop button in the UI (piercing Shadow DOM) if a client disconnects mid-generation."""
    if not is_ready or context is None or page is None or page.is_closed():
        return
    try:
        await page.evaluate(
            """() => {
            function findStop(root) {
                if (!root) return false;
                if (root.querySelectorAll) {
                    const btns = root.querySelectorAll('.send-button.stop, md-icon-button.send-button.stop, [data-aria-label="Stop"], [aria-label="Stop"], button[aria-label*="Stop"]');
                    for (const b of btns) {
                        b.click();
                        return true;
                    }
                }
                if (root.shadowRoot && findStop(root.shadowRoot)) return true;
                if (root.children) {
                    for (const c of root.children) {
                        if (findStop(c)) return true;
                    }
                }
                return false;
            }
            findStop(document);
        }"""
        )
        await asyncio.sleep(0.3)
    except Exception as e:
        print(f"  [Debug] Failed to click Stop button on disconnect: {e}", flush=True)


async def fetch_available_models(page: Page):
    global AVAILABLE_MODELS
    try:
        print("  [Debug] Fetching available models from UI...", flush=True)
        await page.locator("#model-selector-menu-anchor, .action-model-selector").first.click()
        await asyncio.sleep(1.0)

        items = await page.locator('md-menu-item, [role="menuitem"]').all()
        fetched_models = []

        for item in items:
            if await item.is_visible():
                raw_text = await item.evaluate(
                    """(el) => {
                    function getDeepText(node) {
                        if (!node) return '';
                        if (node.nodeType === Node.ELEMENT_NODE) {
                            const tag = (node.tagName || '').toLowerCase();
                            if (tag === 'md-icon') return '';
                        }
                        let text = '';
                        if (node.nodeType === Node.TEXT_NODE) {
                            text += node.textContent;
                        } else if (node.nodeType === Node.ELEMENT_NODE) {
                            const tag = (node.tagName || '').toLowerCase();
                            if (['p', 'div', 'br', 'li', 'span'].includes(tag)) {
                                text += '\\n';
                            }
                        }
                        if (node.shadowRoot) text += getDeepText(node.shadowRoot);
                        if (node.childNodes) {
                            for (let child of node.childNodes) text += getDeepText(child);
                        }
                        return text;
                    }
                    return getDeepText(el).trim();
                }"""
                )

                if not raw_text:
                    continue

                lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
                if lines:
                    model_name = lines[0]
                    if (
                        model_name
                        and model_name not in fetched_models
                        and model_name.lower() not in ["check", "close"]
                    ):
                        fetched_models.append(model_name)

        if fetched_models:
            AVAILABLE_MODELS = fetched_models
            print(f"  [Debug] Successfully fetched models: {AVAILABLE_MODELS}", flush=True)
        else:
            print("  [Debug] Fetched models list was empty. Using defaults.", flush=True)

        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

    except Exception as e:
        print(f"  [Debug] Failed to fetch dynamic models: {e}. Using defaults.", flush=True)


async def cleanup_idle_sessions_loop():
    """Background task to evict idle or excess browser tabs and prevent Chrome OOM."""
    while True:
        await asyncio.sleep(60)
        try:
            now = time.monotonic()
            expired_ids = [
                sid
                for sid, last_used in list(session_last_used.items())
                if (now - last_used) > SESSION_IDLE_TTL_SECONDS
            ]
            for sid in expired_ids:
                lock = page_locks.get(sid)
                if lock and lock.locked():
                    continue
                page = session_pages.pop(sid, None)
                session_last_used.pop(sid, None)
                session_history_hashes.pop(sid, None)
                session_tools_hashes.pop(sid, None)
                dirty_sessions.discard(sid)
                page_locks.pop(sid, None)
                if page and not page.is_closed():
                    print(f"🧹 [Cleanup] Closing idle session tab: {sid[-8:]}", flush=True)
                    await page.close()

            if len(session_pages) > MAX_OPEN_TABS:
                sorted_sessions = sorted(session_last_used.items(), key=lambda x: x[1])
                for sid, _ in sorted_sessions:
                    if len(session_pages) <= MAX_OPEN_TABS:
                        break
                    lock = page_locks.get(sid)
                    if lock and lock.locked():
                        continue
                    page = session_pages.pop(sid, None)
                    session_last_used.pop(sid, None)
                    session_history_hashes.pop(sid, None)
                    session_tools_hashes.pop(sid, None)
                    dirty_sessions.discard(sid)
                    page_locks.pop(sid, None)
                    if page and not page.is_closed():
                        print(f"🧹 [Cleanup] Evicting LRU session tab: {sid[-8:]}", flush=True)
                        await page.close()

            # Purge cached generated images older than IMAGE_TTL_SECONDS
            now_wall = time.time()
            for img_file in IMAGE_DIR.glob("img_*"):
                try:
                    if (now_wall - img_file.stat().st_mtime) > IMAGE_TTL_SECONDS:
                        img_file.unlink(missing_ok=True)
                except Exception:
                    pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"  [Debug] Session cleanup warning: {e}", flush=True)


async def init_browser():
    global playwright_instance, context, cleanup_task, is_ready

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    first_time = not LOGIN_FLAG.exists()

    if first_time:
        print("\n" + "=" * 50)
        print("  FIRST TIME SETUP - Please log into account via SSO")
        print("=" * 50 + "\n")
    else:
        print("🚀 Starting service in headless mode...")

    playwright_instance = await async_playwright().start()

    headless_args = [] if first_time else ["--headless=new"]

    context = await playwright_instance.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        channel="chrome",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            *headless_args,
        ],
        viewport={"width": 1280, "height": 900},
    )

    page = await context.new_page()
    await Stealth().apply_stealth_async(page)
    await page.goto(TARGET_URL)

    if first_time:
        print("📌 Browser opened - please log into your account")
        print("   Waiting for login...\n")
        for i in range(150):
            if await check_logged_in(page, timeout=3000):
                LOGIN_FLAG.write_text("ok")
                print("\n✅ Login saved! Restarting in headless mode...\n")
                await page.close()
                await context.close()
                await playwright_instance.stop()
                return await init_browser()
            await asyncio.sleep(2)
            if i % 15 == 0 and i > 0:
                print(f"   Still waiting... ({i*2}s)")

        print("❌ Login timeout")
        return

    print(f"  [Debug] Headless browser landed on URL: {page.url}", flush=True)
    try:
        await asyncio.sleep(3)
        account_btn = await page.query_selector(
            'div[data-identifier], .lCoei, [data-email], [data-authuser="0"]'
        )
        if account_btn and (
            "signin" in page.url or "ServiceLogin" in page.url or "account" in page.url
        ):
            print("  → Bypassing 'Choose an account' screen in headless mode...", flush=True)
            await account_btn.click()
            await asyncio.sleep(5)
    except Exception:
        pass

    if not await check_logged_in(page, timeout=25000):
        print(f"❌ Session expired or not ready. Stuck on: {page.url}", flush=True)
        print("Deleting login flag, please restart", flush=True)
        LOGIN_FLAG.unlink(missing_ok=True)
        return

    await fetch_available_models(page)
    await page.close()

    cleanup_task = asyncio.create_task(cleanup_idle_sessions_loop())

    is_ready = True
    print("✅ Service ready! Local gateway is online.")
    print("🎯 API: http://localhost:8080/v1/chat/completions\n")


async def shutdown_browser():
    """Gracefully close all browser tabs, persistent context, and Playwright instance."""
    global playwright_instance, context, cleanup_task, is_ready
    is_ready = False
    if cleanup_task and not cleanup_task.done():
        cleanup_task.cancel()
    for sid, page in list(session_pages.items()):
        try:
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass
    session_pages.clear()
    if context:
        try:
            await context.close()
        except Exception:
            pass
        context = None
    if playwright_instance:
        try:
            await playwright_instance.stop()
        except Exception:
            pass
        playwright_instance = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(init_browser())
    yield
    if not task.done():
        task.cancel()
    await shutdown_browser()


app = FastAPI(title="Gemini Enterprise Local Gateway", version=VERSION, lifespan=lifespan)


async def wait_for_editor_ready(page: Page, timeout_ms: int = 30000) -> bool:
    """
    Poll for `ucs-prosemirror-editor` while automatically bypassing Google/SSO
    'Choose an account' screens if redirected during tab initialization or reset.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    clicked_account = False

    while time.monotonic() < deadline:
        if page.is_closed():
            return False

        try:
            editor = page.locator("ucs-prosemirror-editor").first
            if await editor.count() > 0 and await editor.is_visible():
                return True
        except Exception:
            pass

        try:
            url = page.url or ""
            if not clicked_account and any(k in url for k in ("signin", "ServiceLogin", "account")):
                account_btn = await page.query_selector(
                    'div[data-identifier], .lCoei, [data-email], [data-authuser="0"]'
                )
                if account_btn:
                    print("  → Bypassing 'Choose an account' screen...", flush=True)
                    clicked_account = True
                    await account_btn.click()
                    await asyncio.sleep(2.5)
                    continue
        except Exception as e:
            print(f"  [Debug] Account chooser check warning: {e}", flush=True)

        await asyncio.sleep(0.5)

    return False


async def get_or_create_session_page(session_id: str = "default", force_reset: bool = False) -> Page:
    """
    Get or reset the persistent Vertex browser tab for a client conversation.
    NOTE: Must be called while holding `page_locks[session_id]`.
    """
    global session_pages, session_creation_locks

    creation_lock = session_creation_locks.setdefault(session_id, asyncio.Lock())
    async with creation_lock:
        page = session_pages.get(session_id)
        session_last_used[session_id] = time.monotonic()

        if page and not page.is_closed():
            if not (force_reset or session_id in dirty_sessions):
                if await wait_for_editor_ready(page, timeout_ms=5000):
                    return page
                print(f"  ⚠️ Existing session tab {session_id[-8:]} editor not responsive; replacing tab...", flush=True)
            else:
                print(f"  🔄 Resetting session tab (dirty/rewind): {session_id[-8:]}", flush=True)

            # Always close dirty/unresponsive tab and replace with a clean tab to avoid SPA/stream hangs
            session_pages.pop(session_id, None)
            dirty_sessions.discard(session_id)
            session_history_hashes.pop(session_id, None)
            session_tools_hashes.pop(session_id, None)
            try:
                await stop_active_generation(page)
            except Exception:
                pass
            try:
                await page.close()
            except Exception:
                pass
            page = None

        session_pages.pop(session_id, None)
        session_history_hashes.pop(session_id, None)
        session_tools_hashes.pop(session_id, None)
        dirty_sessions.discard(session_id)

        last_err = None
        for attempt in range(1, 3):
            new_page: Optional[Page] = None
            try:
                print(f"  → Opening session tab: {session_id[-8:]} (attempt {attempt}/2)", flush=True)
                new_page = await context.new_page()
                await Stealth().apply_stealth_async(new_page)
                await new_page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)

                if await wait_for_editor_ready(new_page, timeout_ms=30000):
                    session_pages[session_id] = new_page
                    session_last_used[session_id] = time.monotonic()
                    print(f"  ✓ Session {session_id[-8:]} ready", flush=True)
                    return new_page

                raise TimeoutError(f"Timed out waiting for ucs-prosemirror-editor (URL: {new_page.url})")
            except Exception as e:
                last_err = e
                print(f"  ⚠️ Session tab init attempt {attempt} failed: {e}", flush=True)
                if new_page and not new_page.is_closed():
                    try:
                        await new_page.close()
                    except Exception:
                        pass
                await asyncio.sleep(1.0)

        raise HTTPException(
            status_code=503,
            detail=f"Failed to initialize Gemini session tab after 2 attempts: {last_err}",
        )


async def switch_model(page: Page, target_model: str):
    if not target_model or target_model.lower() in ("gemini-enterprise", "auto", "default"):
        return

    try:
        label_locator = page.locator(".model-selector-label")
        if await label_locator.count() > 0:
            current_label = await label_locator.first.inner_text()
            if target_model.lower() in current_label.lower():
                return

            await page.locator("#model-selector-menu-anchor, .action-model-selector").first.click()
            await asyncio.sleep(0.8)

            target_regex = re.compile(re.escape(target_model), re.IGNORECASE)
            options = page.locator('md-menu-item, [role="menuitem"]').filter(has_text=target_regex)

            clicked = False
            if await options.count() > 0:
                for i in range(await options.count()):
                    if await options.nth(i).is_visible():
                        await options.nth(i).click()
                        clicked = True
                        break

            if not clicked:
                fallback_options = page.locator("ucs-model-selector").get_by_text(target_regex)
                if await fallback_options.count() > 0:
                    for i in range(await fallback_options.count()):
                        class_name = await fallback_options.nth(i).evaluate("(el) => el.className || ''")
                        if "model-selector-label" not in class_name and await fallback_options.nth(i).is_visible():
                            await fallback_options.nth(i).click()
                            clicked = True
                            break

            if clicked:
                await asyncio.sleep(0.8)
            else:
                await page.keyboard.press("Escape")

    except Exception as e:
        print(f"  [Debug] Error switching model: {e}", flush=True)


# ==============================================================================
# FAST PROMPT INSERTION & MULTIMODAL UPLOAD (WITH TURN-BOUNDARY VERIFICATION)
# ==============================================================================
async def submit_prompt_to_ui(
    page: Page,
    text: str,
    images: Optional[List[Tuple[str, bytes]]] = None,
) -> int:
    """Insert text via synthetic ClipboardEvent (with fallback), submit, and verify turn start."""
    existing_responses = await page.query_selector_all(".turn")
    response_count_before = len(existing_responses)

    if images:
        temp_files = []
        try:
            file_input = page.locator('input[type="file"]').first
            if await file_input.count() > 0:
                for filename, raw_bytes in images:
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f"_{filename}")
                    tmp.write(raw_bytes)
                    tmp.close()
                    temp_files.append(tmp.name)
                await file_input.set_input_files(temp_files)
                await asyncio.sleep(1.0)
        except Exception as e:
            print(f"  [Debug] Image attachment warning: {e}", flush=True)
        finally:
            for fpath in temp_files:
                try:
                    os.unlink(fpath)
                except Exception:
                    pass

    try:
        input_box = page.locator(
            "ucs-prosemirror-editor#agent-search-prosemirror-editor, ucs-prosemirror-editor"
        ).first
        await input_box.wait_for(state="visible", timeout=10000)
        await input_box.click()
        await asyncio.sleep(0.15)

        await page.evaluate(
            """([box, txt]) => {
            const editable = (box.shadowRoot && box.shadowRoot.querySelector('[contenteditable="true"]')) || box;
            editable.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
            try {
                const dt = new DataTransfer();
                dt.setData('text/plain', txt);
                const pasteEvent = new ClipboardEvent('paste', {
                    clipboardData: dt,
                    bubbles: true,
                    cancelable: true
                });
                editable.dispatchEvent(pasteEvent);
                if (!editable.textContent || editable.textContent.trim().length === 0) {
                    document.execCommand('insertText', false, txt);
                }
            } catch (err) {
                document.execCommand('insertText', false, txt);
            }
        }""",
            [await input_box.element_handle(), text],
        )

        await asyncio.sleep(0.2)
    except Exception as e:
        print(f"Error targeting chat box: {e}", flush=True)

    async def _trigger_submit():
        try:
            send_button = page.locator('.send-button.submit, button[aria-label="Submit"]').first
            if await send_button.count() > 0 and await send_button.is_visible():
                await send_button.click(timeout=2000)
            else:
                await page.keyboard.press("Enter")
        except Exception:
            await page.keyboard.press("Enter")

    await _trigger_submit()

    verify_start = time.monotonic()
    retried_submit = False
    while (time.monotonic() - verify_start) < 5.0:
        await asyncio.sleep(0.25)
        current_turns = await page.query_selector_all(".turn")
        if len(current_turns) > response_count_before:
            break
        if not retried_submit and (time.monotonic() - verify_start) >= 2.0:
            retried_submit = True
            print("  [Debug] Turn count did not increment after 2s; retrying submit...", flush=True)
            await _trigger_submit()

    return response_count_before


# ==============================================================================
# CORE EXTRACTION & REAL-TIME STREAMING
# ==============================================================================
MIN_GENERATED_IMAGE_BYTES = 4096
MIN_GENERATED_IMAGE_DIM = 200


PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")


def get_image_dimensions(raw_bytes: bytes) -> Optional[Tuple[int, int]]:
    """Parse width and height from PNG, JPEG, GIF, or WebP headers without external imaging libraries."""
    if not raw_bytes or len(raw_bytes) < 24:
        return None
    # PNG: 8-byte signature + IHDR chunk (width/height at bytes 16..24)
    if raw_bytes.startswith(PNG_MAGIC) and raw_bytes[12:16] == b"IHDR":
        w = int.from_bytes(raw_bytes[16:20], "big")
        h = int.from_bytes(raw_bytes[20:24], "big")
        return (w, h) if w > 0 and h > 0 else None
    # GIF: 'GIF87a' or 'GIF89a' (width/height little-endian at bytes 6..10)
    if raw_bytes[:6] in (b"GIF87a", b"GIF89a"):
        w = int.from_bytes(raw_bytes[6:8], "little")
        h = int.from_bytes(raw_bytes[8:10], "little")
        return (w, h) if w > 0 and h > 0 else None
    # WebP: RIFF....WEBP
    if raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP" and len(raw_bytes) >= 30:
        chunk = raw_bytes[12:16]
        if chunk == b"VP8X":
            w = 1 + int.from_bytes(raw_bytes[24:27], "little")
            h = 1 + int.from_bytes(raw_bytes[27:30], "little")
            return (w, h)
        if chunk == b"VP8 " and len(raw_bytes) >= 30:
            w = int.from_bytes(raw_bytes[26:28], "little") & 0x3FFF
            h = int.from_bytes(raw_bytes[28:30], "little") & 0x3FFF
            return (w, h) if w > 0 and h > 0 else None
        if chunk == b"VP8L" and len(raw_bytes) >= 25:
            b0, b1, b2, b3 = raw_bytes[21], raw_bytes[22], raw_bytes[23], raw_bytes[24]
            w = 1 + (((b1 & 0x3F) << 8) | b0)
            h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
            return (w, h)
    # JPEG: scan SOF markers
    if raw_bytes.startswith(b"\xff\xd8"):
        idx = 2
        n = len(raw_bytes)
        while idx + 8 < n:
            if raw_bytes[idx] != 0xFF:
                idx += 1
                continue
            while idx < n and raw_bytes[idx] == 0xFF:
                idx += 1
            if idx >= n:
                break
            marker = raw_bytes[idx]
            idx += 1
            if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7):
                continue
            if idx + 2 > n:
                break
            seg_len = int.from_bytes(raw_bytes[idx : idx + 2], "big")
            if seg_len < 2:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if idx + 7 <= n:
                    h = int.from_bytes(raw_bytes[idx + 3 : idx + 5], "big")
                    w = int.from_bytes(raw_bytes[idx + 5 : idx + 7], "big")
                    return (w, h) if w > 0 and h > 0 else None
                break
            idx += seg_len
    return None


def is_valid_generated_image_bytes(
    raw_bytes: Optional[bytes],
    min_bytes: int = MIN_GENERATED_IMAGE_BYTES,
    min_dim: int = MIN_GENERATED_IMAGE_DIM,
) -> bool:
    """Return True only if raw_bytes is a real generated image (>= 4KB and >= 200x200 px), rejecting 64x64 UI icons."""
    if not raw_bytes or len(raw_bytes) < min_bytes:
        return False
    dims = get_image_dimensions(raw_bytes)
    if dims is not None:
        w, h = dims
        if w < min_dim or h < min_dim:
            return False
    return True


def is_hermes_client(messages: List[Message]) -> bool:
    """Detect if the requesting client is Hermes Desktop (supports native MEDIA: file rendering)."""
    for m in messages:
        if m.role in ("system", "developer"):
            txt = get_content_text(m.content)
            if "MEDIA:" in txt or "Hermes desktop app" in txt:
                return True
    return False


async def extract_and_save_turn_images(
    turn_handle: Any,
    page: Optional[Page] = None,
    request: Optional[Request] = None,
    emit_media_directive: bool = False,
    expect_image: bool = False,
) -> str:
    """Extract generated images from the finished DOM turn, save to IMAGE_DIR, and return Markdown/MEDIA links."""
    extracted: List[Dict[str, Any]] = []
    max_attempts = 4 if expect_image else 1
    for attempt in range(max_attempts):
        try:
            res = await turn_handle.evaluate(JS_EXTRACT_TURN_IMAGES)
            if isinstance(res, list) and len(res) > 0:
                extracted = res
                break
        except Exception as e:
            print(f"  [Debug] Failed to extract generated images from turn (attempt {attempt + 1}): {e}", flush=True)
        if expect_image and page is not None:
            try:
                # Also check document.body in case the portal mounted the generated image card outside .turn
                body_res = await page.evaluate(f"({JS_EXTRACT_TURN_IMAGES})(document.body)")
                if isinstance(body_res, list) and len(body_res) > 0:
                    extracted = [ body_res[-1] ]
                    break
            except Exception:
                pass
        if attempt + 1 < max_attempts:
            await asyncio.sleep(0.8)

    base_url = str(request.base_url).rstrip("/") if request is not None else "http://127.0.0.1:8080"
    md_Blocks = []

    if not extracted and expect_image and page is not None:
        # Tier 4 Fallback: If 'Image generated by ...' was announced in text but no DOM <img>/<canvas> matched
        # (e.g. closed Shadow DOM or custom renderer), capture a visual screenshot of the turn's summary/card.
        try:
            shot_bytes = await turn_handle.screenshot(type="png", timeout=10000)
            if shot_bytes and len(shot_bytes) >= 1024:
                digest = hashlib.sha256(shot_bytes).hexdigest()[:24]
                fname = f"img_{digest}.png"
                fpath = IMAGE_DIR / fname
                if not fpath.exists():
                    fpath.write_bytes(shot_bytes)
                img_url = f"{base_url}/v1/images/{fname}"
                print(f"  🖼️ [Image] Captured turn card fallback screenshot ({len(shot_bytes)} bytes)", flush=True)
                if emit_media_directive:
                    return f"MEDIA:{fpath.resolve().as_posix()}"
                return f"![Generated Image]({img_url})"
        except Exception as e:
            print(f"  [Debug] Tier-4 turn screenshot fallback failed: {e}", flush=True)
        return ""

    if not extracted or not isinstance(extracted, list):
        return ""

    for idx, item in enumerate(extracted):
        if not isinstance(item, dict):
            continue
        data_url = item.get("dataUrl") or ""
        src = (item.get("src") or "").strip()
        extract_id = item.get("extractId") or ""
        alt = item.get("alt") or f"Generated Image {idx + 1}"
        raw_bytes: Optional[bytes] = None
        ext = "png"

        # Tier 1: Direct data:image/... URI returned from in-page JS
        match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", data_url)
        if match:
            mime_type, b64_data = match.groups()
            ext = mime_type.split("/")[-1].split("+")[0].lower()
            try:
                raw_bytes = base64.b64decode(b64_data)
            except Exception:
                raw_bytes = None

        # Tier 2: Playwright browser network request (bypasses CORS while sharing browser session cookies)
        if not is_valid_generated_image_bytes(raw_bytes) and page is not None and src.startswith(("http://", "https://")):
            try:
                resp = await page.context.request.get(src, timeout=15000)
                if resp.ok:
                    body = await resp.body()
                    if is_valid_generated_image_bytes(body):
                        raw_bytes = body
                        ct = (resp.headers.get("content-type") or "").lower()
                        if "jpeg" in ct or "jpg" in ct:
                            ext = "jpg"
                        elif "webp" in ct:
                            ext = "webp"
                        elif "gif" in ct:
                            ext = "gif"
                        else:
                            ext = "png"
                        print(f"  🖼️ [Image] Fetched cross-origin image via Playwright request ({len(raw_bytes)} bytes)", flush=True)
            except Exception as e:
                print(f"  [Debug] Tier-2 Playwright request fetch failed for {src[:60]}: {e}", flush=True)

        # Tier 3: Playwright compositor element screenshot (pierces Shadow DOM and ignores canvas tainting)
        if not is_valid_generated_image_bytes(raw_bytes) and page is not None and extract_id:
            try:
                loc = page.locator(f'[data-ge-extract-id="{extract_id}"]').first
                if await loc.count() > 0:
                    shot_bytes = await loc.screenshot(type="png", timeout=10000)
                    if is_valid_generated_image_bytes(shot_bytes):
                        raw_bytes = shot_bytes
                        ext = "png"
                        print(f"  🖼️ [Image] Captured image via Playwright element screenshot ({len(raw_bytes)} bytes)", flush=True)
            except Exception as e:
                print(f"  [Debug] Tier-3 element screenshot fallback failed for {extract_id}: {e}", flush=True)

        if not is_valid_generated_image_bytes(raw_bytes):
            print(f"  ⚠️ [Image] Rejected small/invalid media (tag={item.get('tag')}, size={len(raw_bytes) if raw_bytes else 0}, src={src[:60]})", flush=True)
            continue

        if ext == "jpeg":
            ext = "jpg"
        elif ext not in ("png", "jpg", "webp", "gif"):
            ext = "png"
        try:
            digest = hashlib.sha256(raw_bytes).hexdigest()[:24]
            fname = f"img_{digest}.{ext}"
            fpath = IMAGE_DIR / fname
            if not fpath.exists():
                fpath.write_bytes(raw_bytes)
            img_url = f"{base_url}/v1/images/{fname}"
            if emit_media_directive:
                md_Blocks.append(f"MEDIA:{fpath.resolve().as_posix()}")
            else:
                md_Blocks.append(f"![{alt}]({img_url})")
        except Exception as e:
            print(f"  [Debug] Error saving extracted image: {e}", flush=True)

    return "\n\n".join(md_Blocks)


async def send_to_gemini(
    session_id: str,
    page: Page,
    text: str,
    model: Optional[str] = None,
    images: Optional[List[Tuple[str, bytes]]] = None,
    request: Optional[Request] = None,
    emit_media_directive: bool = False,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> str:
    if model:
        await switch_model(page, model)

    dirty_sessions.add(session_id)
    response_count_before = await submit_prompt_to_ui(page, text, images=images)

    response_text = None
    start_time = time.monotonic()
    previous_state_key = ""
    first_content_at: Optional[float] = None
    last_text_change_at: Optional[float] = None
    last_active_work_at: Optional[float] = None
    has_seen_generation_start = False
    has_seen_active_work = False

    consecutive_poll_errors = 0
    while (time.monotonic() - start_time) < timeout:
        if not is_ready or context is None or page.is_closed():
            dirty_sessions.add(session_id)
            raise HTTPException(status_code=503, detail="Browser session closed or server shutting down.")

        if request is not None and await request.is_disconnected():
            print(f"🛑 [Abort] Client disconnected during non-streaming turn on {session_id[-8:]}", flush=True)
            await stop_active_generation(page)
            raise HTTPException(status_code=499, detail="Client disconnected.")

        try:
            response_divs = await page.query_selector_all(".turn")
            consecutive_poll_errors = 0
            current_count = len(response_divs)

            if current_count > response_count_before:
                last_turn = response_divs[-1]
                state = await last_turn.evaluate(JS_STATUS_AND_EXTRACTOR)

                is_generating = state.get("isGenerating", False)
                has_active_work = state.get("hasActiveWork", False)
                raw_turn_text = state.get("text", "")
                image_count = state.get("imageCount", 0)
                image_sig = state.get("imageSignature", "")
                is_pending_section = has_pending_section_marker(raw_turn_text)
                current_text = clean_response_text(raw_turn_text)
                has_content = bool(current_text) or image_count > 0
                now = time.monotonic()
                state_key = f"{current_text}||{image_sig}"
                text_changed = state_key != previous_state_key
                previous_state_key = state_key

                if has_content and first_content_at is None:
                    first_content_at = now
                if text_changed:
                    last_text_change_at = now
                if has_active_work:
                    has_seen_active_work = True
                    last_active_work_at = now

                if has_content or is_generating or has_active_work or is_pending_section:
                    has_seen_generation_start = True

                if (
                    has_seen_generation_start
                    and has_content
                    and not is_generating
                    and not has_active_work
                    and first_content_at is not None
                    and last_text_change_at is not None
                ):
                    grace_seconds = (
                        INTERMEDIATE_STATUS_GRACE_SECONDS
                        if (is_intermediate_status(current_text) or is_pending_section)
                        and not has_seen_active_work
                        and image_count == 0
                        else (
                            max(INITIAL_RESPONSE_GRACE_SECONDS, 6.0)
                            if IMAGE_CAPTION_ANNOUNCE_RE.search(current_text) and image_count == 0
                            else INITIAL_RESPONSE_GRACE_SECONDS
                        )
                    )
                    quiet_since = max(
                        last_text_change_at,
                        last_active_work_at or last_text_change_at,
                    )
                    if (
                        now - first_content_at >= grace_seconds
                        and now - quiet_since >= FINAL_RESPONSE_QUIET_SECONDS
                    ):
                        img_md = ""
                        caption_announces_image = bool(IMAGE_CAPTION_ANNOUNCE_RE.search(current_text))
                        if image_count > 0 or caption_announces_image:
                            img_md = await extract_and_save_turn_images(
                                last_turn,
                                page=page,
                                request=request,
                                emit_media_directive=emit_media_directive,
                                expect_image=caption_announces_image,
                            )
                        if current_text and img_md:
                            response_text = f"{current_text}\n\n{img_md}"
                        else:
                            response_text = current_text or img_md
                        break
        except asyncio.CancelledError:
            dirty_sessions.add(session_id)
            raise
        except Exception as e:
            consecutive_poll_errors += 1
            if is_browser_closed_error(e) or not is_ready or context is None or page.is_closed() or consecutive_poll_errors >= 5:
                dirty_sessions.add(session_id)
                print(f"🛑 [Abort] Browser connection closed during polling on {session_id[-8:]}: {e}", flush=True)
                raise HTTPException(status_code=503, detail="Browser connection closed during generation.")
            print(f"  [Debug] Error polling response: {e}", flush=True)

        await asyncio.sleep(0.35)

    if not response_text:
        await stop_active_generation(page)
        raise HTTPException(status_code=504, detail="Gateway timed out waiting for AI response.")

    return response_text


def get_unemitted_suffix(emitted: str, current: str) -> str:
    """Return only the un-emitted tail of `current` when DOM reflow slightly alters `emitted`."""
    if not emitted:
        return current
    if current.startswith(emitted):
        return current[len(emitted) :]
    common_len = 0
    for a, b in zip(emitted, current):
        if a == b:
            common_len += 1
        else:
            break
    # Try matching progressively smaller suffix windows near the end of `emitted`
    for trim_end in (0, 2, 4, 8):
        end_pos = len(emitted) - trim_end
        if end_pos <= common_len:
            continue
        for win_len in (40, 24, 14):
            if end_pos - win_len < 0:
                continue
            anchor = emitted[end_pos - win_len : end_pos]
            idx = current.find(anchor, max(0, common_len - 20))
            if idx != -1:
                return current[idx + len(anchor) :]
    return current[common_len:]


async def stream_from_gemini(
    session_id: str,
    req: ChatRequest,
    request: Request,
    initial_hashes: List[str],
    images: Optional[List[Tuple[str, bytes]]] = None,
    include_usage: bool = False,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
):
    """Yield an OpenAI-compatible incremental SSE stream with real-time delta chunks and tool-call holdback."""
    completion_id = f"chatcmpl-{int(time.time() * 1000)}"
    created = int(time.time())
    model = req.model
    tools = req.tools if req.tool_choice != "none" else None
    emit_media = is_hermes_client(req.messages)

    def event(
        delta: Dict[str, Any],
        finish_reason: Optional[str] = None,
        usage: Optional[Dict[str, int]] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model or "gemini-enterprise",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
        if usage is not None:
            payload["usage"] = usage
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    session_lock = page_locks.setdefault(session_id, asyncio.Lock())
    async with session_lock:
        session_last_used[session_id] = time.monotonic()
        text_to_send, needs_reset, initial_hashes, new_tools_hash = prepare_turn_prompt(session_id, req)
        try:
            page = await get_or_create_session_page(session_id, force_reset=needs_reset)

            sync_mode = "RESET+FULL" if needs_reset else ("DELTA" if session_id in session_history_hashes else "NEW")
            print(
                f"📥 [{time.strftime('%H:%M:%S')}] Chat {session_id[-8:]} ({sync_mode}): "
                f"{len(req.messages)} msgs | {len(text_to_send)} chars (Stream: True)",
                flush=True,
            )

            if model:
                await switch_model(page, model)

            dirty_sessions.add(session_id)
            response_count_before = await submit_prompt_to_ui(page, text_to_send, images=images)
        except Exception as e:
            dirty_sessions.add(session_id)
            print(f"❌ [Stream Init Error] Session {session_id[-8:]} failed during setup: {e}", flush=True)
            yield event({"role": "assistant", "content": f"\n\n[Gateway Error: Failed to prepare browser session: {e}]"})
            yield event({}, finish_reason="stop")
            yield "data: [DONE]\n\n"
            return

        start_time = time.monotonic()
        previous_state_key = ""
        emitted_text = ""
        role_announced = False
        first_content_at: Optional[float] = None
        last_text_change_at: Optional[float] = None
        last_active_work_at: Optional[float] = None
        last_keepalive_at = start_time
        has_seen_generation_start = False
        has_seen_active_work = False

        consecutive_poll_errors = 0
        while (time.monotonic() - start_time) < timeout:
            if not is_ready or context is None or page.is_closed():
                dirty_sessions.add(session_id)
                print(f"🛑 [Stream Abort] Browser session closed or server shutting down on {session_id[-8:]}.", flush=True)
                return

            if await request.is_disconnected():
                print(f"🛑 [Stream Abort] Client disconnected on {session_id[-8:]}; stopping browser generation.", flush=True)
                await stop_active_generation(page)
                return

            try:
                response_divs = await page.query_selector_all(".turn")
                consecutive_poll_errors = 0
                current_count = len(response_divs)

                if current_count > response_count_before:
                    last_turn = response_divs[-1]
                    state = await last_turn.evaluate(JS_STATUS_AND_EXTRACTOR)

                    is_generating = state.get("isGenerating", False)
                    has_active_work = state.get("hasActiveWork", False)
                    raw_turn_text = state.get("text", "")
                    image_count = state.get("imageCount", 0)
                    image_sig = state.get("imageSignature", "")
                    is_pending_section = has_pending_section_marker(raw_turn_text)
                    current_text = clean_response_text(raw_turn_text)
                    has_content = bool(current_text) or image_count > 0
                    now = time.monotonic()
                    state_key = f"{current_text}||{image_sig}"
                    text_changed = state_key != previous_state_key
                    previous_state_key = state_key

                    if has_content and first_content_at is None:
                        first_content_at = now
                    if text_changed:
                        last_text_change_at = now
                    if has_active_work:
                        has_seen_active_work = True
                        last_active_work_at = now

                    if has_content or is_generating or has_active_work or is_pending_section:
                        has_seen_generation_start = True

                    if (
                        current_text
                        and not is_intermediate_status(current_text)
                        and not is_pending_section
                        and not has_active_work
                    ):
                        safe_limit = len(current_text)
                        if tools:
                            masked_current = _mask_non_tool_code_blocks(current_text)
                            # Hold back only line-anchored `<tool_call>` blocks or partial line-start tags/fences
                            tc_match = STANDALONE_TOOL_CALL_OPEN_RE.search(masked_current)
                            if tc_match:
                                safe_limit = tc_match.start()
                            else:
                                tail_match = PARTIAL_TOOL_CALL_TAIL_RE.search(masked_current)
                                if tail_match:
                                    safe_limit = tail_match.start()

                        # While still generating, hold back the actively-streaming last row of a Markdown table
                        # so a premature closing '|' is never emitted mid-cell before the row finishes
                        if is_generating:
                            table_tail = INCOMPLETE_TABLE_ROW_TAIL_RE.search(current_text[:safe_limit])
                            if table_tail:
                                safe_limit = min(safe_limit, table_tail.start())

                        safe_prefix = current_text[:safe_limit]
                        if len(safe_prefix) > len(emitted_text):
                            delta_chunk = (
                                safe_prefix[len(emitted_text) :]
                                if safe_prefix.startswith(emitted_text)
                                else get_unemitted_suffix(emitted_text, safe_prefix)
                            )
                            if delta_chunk:
                                if not role_announced:
                                    yield event({"role": "assistant", "content": ""})
                                    role_announced = True
                                yield event({"content": delta_chunk})
                                emitted_text = safe_prefix

                    # Check completion criteria
                    if (
                        has_seen_generation_start
                        and has_content
                        and not is_generating
                        and not has_active_work
                        and first_content_at is not None
                        and last_text_change_at is not None
                    ):
                        grace_seconds = (
                            INTERMEDIATE_STATUS_GRACE_SECONDS
                            if (is_intermediate_status(current_text) or is_pending_section)
                            and not has_seen_active_work
                            and image_count == 0
                            else (
                                max(INITIAL_RESPONSE_GRACE_SECONDS, 6.0)
                                if IMAGE_CAPTION_ANNOUNCE_RE.search(current_text) and image_count == 0
                                else INITIAL_RESPONSE_GRACE_SECONDS
                            )
                        )
                        quiet_since = max(
                            last_text_change_at,
                            last_active_work_at or last_text_change_at,
                        )
                        if (
                            now - first_content_at >= grace_seconds
                            and now - quiet_since >= FINAL_RESPONSE_QUIET_SECONDS
                        ):
                            if not role_announced:
                                yield event({"role": "assistant", "content": ""})
                                role_announced = True

                            img_md = ""
                            caption_announces_image = bool(IMAGE_CAPTION_ANNOUNCE_RE.search(current_text))
                            if image_count > 0 or caption_announces_image:
                                img_md = await extract_and_save_turn_images(
                                    last_turn,
                                    page=page,
                                    request=request,
                                    emit_media_directive=emit_media,
                                    expect_image=caption_announces_image,
                                )

                            tool_calls = None
                            clean_text = current_text
                            if tools and current_text:
                                clean_text, tool_calls = parse_tool_calls(current_text, allowed_tools=tools)

                            if img_md:
                                clean_text = f"{clean_text}\n\n{img_md}" if clean_text else img_md

                            # Create the final assistant message and update history hashes
                            final_assistant_msg = Message(role="assistant", content=clean_text, tool_calls=tool_calls)
                            session_history_hashes[session_id] = initial_hashes + [hash_message(final_assistant_msg)]
                            session_tools_hashes[session_id] = new_tools_hash
                            dirty_sessions.discard(session_id)

                            usage_obj = None
                            if include_usage:
                                p_tok = estimate_tokens(text_to_send)
                                c_tok = estimate_tokens(current_text)
                                usage_obj = {
                                    "prompt_tokens": p_tok,
                                    "completion_tokens": c_tok,
                                    "total_tokens": p_tok + c_tok,
                                }

                            if tool_calls:
                                print(
                                    f"🔧 [Stream] Emitting tool calls: {[tc['function']['name'] for tc in tool_calls]}",
                                    flush=True,
                                )
                                rem = get_unemitted_suffix(emitted_text, clean_text).rstrip()
                                if rem:
                                    yield event({"content": rem})

                                formatted_stream_tools = []
                                for idx, tc in enumerate(tool_calls):
                                    formatted_stream_tools.append(
                                        {
                                            "index": idx,
                                            "id": tc.get("id", f"call_{int(time.time()*1000)}_{idx}"),
                                            "type": "function",
                                            "function": {
                                                "name": tc["function"]["name"],
                                                "arguments": tc["function"]["arguments"],
                                            },
                                        }
                                    )

                                yield event({"tool_calls": formatted_stream_tools})
                                yield event({}, finish_reason="tool_calls", usage=usage_obj)
                            else:
                                rem = get_unemitted_suffix(emitted_text, clean_text)
                                if rem:
                                    yield event({"content": rem})
                                yield event({}, finish_reason="stop", usage=usage_obj)

                            yield "data: [DONE]\n\n"
                            return

            except asyncio.CancelledError:
                dirty_sessions.add(session_id)
                return
            except Exception as e:
                consecutive_poll_errors += 1
                if is_browser_closed_error(e) or not is_ready or context is None or page.is_closed() or consecutive_poll_errors >= 5:
                    dirty_sessions.add(session_id)
                    print(f"🛑 [Stream Abort] Browser connection closed on {session_id[-8:]}: {e}", flush=True)
                    if not role_announced:
                        yield event({"role": "assistant", "content": ""})
                    yield event({"content": "\n\n[Gateway Error: Browser session closed or server shutting down.]"})
                    yield event({}, finish_reason="stop")
                    yield "data: [DONE]\n\n"
                    return
                print(f"  [Debug] Error polling stream: {e}", flush=True)

            now = time.monotonic()
            if now - last_keepalive_at >= 10:
                yield ": keep-alive\n\n"
                last_keepalive_at = now

            await asyncio.sleep(0.35)

        await stop_active_generation(page)
        if not role_announced:
            yield event({"role": "assistant", "content": ""})
        yield event({"content": "\n\n[Gateway timed out waiting for the web response.]"})
        yield event({}, finish_reason="stop")
        yield "data: [DONE]\n\n"


# ==============================================================================
# API ROUTING
# ==============================================================================
@app.get("/health")
async def health_check():
    return {
        "status": "ready" if is_ready else "initializing",
        "version": VERSION,
        "active_sessions": len(session_pages),
        "dirty_sessions": len(dirty_sessions),
        "available_models": AVAILABLE_MODELS,
    }


@app.post("/v1/sessions/reset", dependencies=[Depends(verify_api_key)])
async def reset_sessions(session_id: Optional[str] = None):
    """Manually reset a specific session tab or clear all active session tabs."""
    if session_id:
        dirty_sessions.add(session_id)
        return {"status": "marked_for_reset", "session_id": session_id}
    for sid, page in list(session_pages.items()):
        try:
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass
    session_pages.clear()
    session_history_hashes.clear()
    session_tools_hashes.clear()
    dirty_sessions.clear()
    return {"status": "all_sessions_cleared"}


@app.get("/v1/images/{filename}")
async def get_generated_image(filename: str):
    """Serve cached generated images (unauthenticated so Markdown <img> tags render directly)."""
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename or not safe_name.startswith("img_"):
        raise HTTPException(status_code=404, detail="Image not found")
    fpath = IMAGE_DIR / safe_name
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(fpath)


@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
            }
            for m in AVAILABLE_MODELS
        ],
    }


def is_dedicated_title_request(req: ChatRequest) -> bool:
    if len(req.messages) > 2:
        return False
    if req.tools:
        return False
    for m in req.messages:
        if m.role in ("system", "developer"):
            sys_text = get_content_text(m.content).strip()
            if len(sys_text) < 400 and (
                sys_text.startswith("You name chat sessions")
                or "you name chat sessions" in sys_text.lower()
            ):
                return True
    return False


@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatRequest, request: Request):
    is_title_request = is_dedicated_title_request(req)

    if is_title_request:
        print("🏷️ [Intercepted] Title generator detected. Returning instant title.", flush=True)
        title_json = '{"title": "Gemini Session"}'

        if req.stream:
            async def stream_title():
                completion_id = f"chatcmpl-title-{int(time.time() * 1000)}"
                created = int(time.time())
                chunk1 = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": req.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": title_json},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk1)}\n\n"
                chunk2 = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": req.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk2)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                stream_title(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            return {
                "id": f"chatcmpl-title-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": title_json},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }

    if not is_ready:
        raise HTTPException(
            status_code=503, detail="Gateway is initializing or awaiting authentication."
        )

    session_id = get_session_id(req, request)
    images = extract_images_from_messages(req.messages)
    include_usage = bool(req.stream_options and req.stream_options.get("include_usage"))

    if req.stream:
        return StreamingResponse(
            stream_from_gemini(
                session_id=session_id,
                req=req,
                request=request,
                initial_hashes=[],
                images=images,
                include_usage=include_usage,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    session_lock = page_locks.setdefault(session_id, asyncio.Lock())
    async with session_lock:
        session_last_used[session_id] = time.monotonic()
        prompt_to_send, needs_reset, initial_hashes, new_tools_hash = prepare_turn_prompt(session_id, req)
        page = await get_or_create_session_page(session_id, force_reset=needs_reset)

        sync_mode = "RESET+FULL" if needs_reset else ("DELTA" if session_id in session_history_hashes else "NEW")
        print(
            f"📥 [{time.strftime('%H:%M:%S')}] Chat {session_id[-8:]} ({sync_mode}): "
            f"{len(req.messages)} msgs | {len(prompt_to_send)} chars (Stream: False)",
            flush=True,
        )

        ai_reply = await send_to_gemini(
            session_id=session_id,
            page=page,
            text=prompt_to_send,
            model=req.model,
            images=images,
            request=request,
            emit_media_directive=is_hermes_client(req.messages),
        )

        tool_calls = None
        finish_reason = "stop"
        clean_text = ai_reply

        if req.tools and req.tool_choice != "none":
            clean_text, tool_calls = parse_tool_calls(ai_reply, allowed_tools=req.tools)
            if tool_calls:
                finish_reason = "tool_calls"
                print(
                    f"🔧 Tool calls detected: {[tc['function']['name'] for tc in tool_calls]}",
                    flush=True,
                )

        msg: Dict[str, Any] = {"role": "assistant"}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if clean_text:
            msg["content"] = clean_text

        # Create the final message and update session history hashes for the *next* turn
        final_assistant_msg = Message(**msg)
        session_history_hashes[session_id] = initial_hashes + [hash_message(final_assistant_msg)]
        session_tools_hashes[session_id] = new_tools_hash
        dirty_sessions.discard(session_id)

        prompt_toks = estimate_tokens(prompt_to_send)
        comp_toks = estimate_tokens(ai_reply)

        return {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": msg,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_toks,
                "completion_tokens": comp_toks,
                "total_tokens": prompt_toks + comp_toks,
            },
        }


if __name__ == "__main__":
    import uvicorn

    default_host = "0.0.0.0" if API_KEY else "127.0.0.1"
    host = os.getenv("HOST", default_host)
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=3)

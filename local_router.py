import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from fastapi.responses import StreamingResponse


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


LOCAL_MODEL_ENABLED: bool = _env_bool("LOCAL_MODEL_ENABLED", True)
LOCAL_MODEL_NAME: str = os.getenv("LOCAL_MODEL_NAME", "local-title").strip() or "local-title"
LOCAL_MODEL_REPO: str = os.getenv("LOCAL_MODEL_REPO", "Qwen/Qwen2.5-0.5B-Instruct-GGUF").strip()
LOCAL_MODEL_FILE: str = os.getenv("LOCAL_MODEL_FILE", "qwen2.5-0.5b-instruct-q4_k_m.gguf").strip()
LOCAL_IDLE_TTL_SECONDS: float = float(os.getenv("LOCAL_IDLE_TTL_SECONDS", "60"))
LOCAL_N_CTX: int = int(os.getenv("LOCAL_N_CTX", "4096"))
LOCAL_N_THREADS: int = int(os.getenv("LOCAL_N_THREADS", "0"))
LOCAL_START_TIMEOUT_SECONDS: float = float(os.getenv("LOCAL_START_TIMEOUT_SECONDS", "180"))

WORKER_SCRIPT = Path(__file__).resolve().parent / "local_worker.py"

TITLE_PATTERNS = (
    "you name chat sessions",
    'reply with json only: {"title":',
)


def _has_non_text_parts(messages: List[Any]) -> bool:
    for msg in messages:
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    ctype = part.get("type", "")
                    if ctype not in ("text", "input_text"):
                        return True
    return False


def classify_local_request(
    req: Any,
    get_content_text: Callable[[Any], str],
    estimate_tokens: Callable[[str], int],
) -> Optional[str]:
    """Return route reason if request should run on local GGUF worker, else None."""
    if not LOCAL_MODEL_ENABLED:
        return None

    req_model = (getattr(req, "model", None) or "").strip().lower()
    if req_model == LOCAL_MODEL_NAME.lower():
        return "explicit_model"

    if getattr(req, "tools", None):
        return None

    messages = getattr(req, "messages", None) or []
    if not messages or _has_non_text_parts(messages):
        return None

    if len(messages) <= 2:
        for m in messages:
            if getattr(m, "role", "") in ("system", "developer"):
                sys_lower = get_content_text(getattr(m, "content", "")).strip().lower()
                if len(sys_lower) < 2000 and any(p in sys_lower for p in TITLE_PATTERNS):
                    return "session_title"

    return None


class LocalWorkerManager:
    def __init__(self) -> None:
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._idle_task: Optional[asyncio.Task] = None
        self._last_used: float = 0.0

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _cancel_idle_timer(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _schedule_idle_unload(self) -> None:
        self._cancel_idle_timer()
        if LOCAL_IDLE_TTL_SECONDS <= 0:
            return

        async def _idle_watcher() -> None:
            try:
                await asyncio.sleep(LOCAL_IDLE_TTL_SECONDS)
                async with self._lock:
                    if self.is_running() and (time.monotonic() - self._last_used) >= LOCAL_IDLE_TTL_SECONDS:
                        print(
                            f"🧹 [LocalWorker] Idle for {int(LOCAL_IDLE_TTL_SECONDS)}s — unloading worker to free RAM.",
                            flush=True,
                        )
                        await self._terminate_process()
            except asyncio.CancelledError:
                pass

        self._idle_task = asyncio.create_task(_idle_watcher())

    async def _terminate_process(self) -> None:
        proc = self._process
        self._process = None
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write((json.dumps({"type": "shutdown"}) + "\n").encode("utf-8"))
                await proc.stdin.drain()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        if self.is_running():
            return self._process  # type: ignore[return-value]

        env = os.environ.copy()
        env["LOCAL_MODEL_REPO"] = LOCAL_MODEL_REPO
        env["LOCAL_MODEL_FILE"] = LOCAL_MODEL_FILE
        env["LOCAL_N_CTX"] = str(LOCAL_N_CTX)
        env["LOCAL_N_THREADS"] = str(LOCAL_N_THREADS)
        env["PYTHONUNBUFFERED"] = "1"

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

        print(f"⚡ [LocalWorker] Loading {LOCAL_MODEL_FILE} on demand...", flush=True)
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            str(WORKER_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            creationflags=creationflags,
        )
        self._process = proc

        try:
            assert proc.stdout is not None
            raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=LOCAL_START_TIMEOUT_SECONDS)
            if not raw_line:
                stderr_out = ""
                if proc.stderr:
                    stderr_out = (await proc.stderr.read()).decode("utf-8", errors="replace")
                raise RuntimeError(f"LocalWorker exited during startup: {stderr_out}")

            msg = json.loads(raw_line.decode("utf-8", errors="replace").strip())
            if msg.get("type") != "ready":
                raise RuntimeError(msg.get("error") or f"Unexpected LocalWorker init response: {msg}")

            elapsed = time.monotonic() - t0
            print(
                f"✅ [LocalWorker] Ready in {elapsed:.2f}s (threads={msg.get('n_threads')}, ctx={msg.get('n_ctx')})",
                flush=True,
            )
            return proc
        except Exception:
            await self._terminate_process()
            raise

    async def shutdown(self) -> None:
        self._cancel_idle_timer()
        async with self._lock:
            await self._terminate_process()

    def _build_worker_messages(self, req: Any, get_content_text: Callable[[Any], str]) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for m in req.messages:
            role = m.role if m.role in ("system", "user", "assistant") else "system"
            text = get_content_text(m.content)
            if text:
                out.append({"role": role, "content": text})
        return out

    async def _run_non_stream(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            self._cancel_idle_timer()
            proc = await self._ensure_started()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()

            while True:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=60.0)
                if not raw_line:
                    await self._terminate_process()
                    raise RuntimeError("LocalWorker terminated unexpectedly")
                msg = json.loads(raw_line.decode("utf-8", errors="replace").strip())
                if msg.get("id") != payload["id"]:
                    continue
                if msg.get("type") == "error":
                    raise RuntimeError(msg.get("error") or "LocalWorker inference error")
                if msg.get("type") == "result":
                    self._last_used = time.monotonic()
                    self._schedule_idle_unload()
                    return msg

    async def _stream_events(self, payload: Dict[str, Any], model_name: str, include_usage: bool, prompt_tokens_est: int) -> AsyncIterator[str]:
        completion_id = f"chatcmpl-local-{int(time.time() * 1000)}"
        created = int(time.time())
        full_text_parts: List[str] = []

        async with self._lock:
            self._cancel_idle_timer()
            proc = await self._ensure_started()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()

            sent_role = False
            finish_reason = "stop"

            while True:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=60.0)
                if not raw_line:
                    await self._terminate_process()
                    raise RuntimeError("LocalWorker terminated unexpectedly")
                msg = json.loads(raw_line.decode("utf-8", errors="replace").strip())
                if msg.get("id") != payload["id"]:
                    continue
                mtype = msg.get("type")
                if mtype == "error":
                    raise RuntimeError(msg.get("error") or "LocalWorker inference error")
                if mtype == "delta":
                    text = msg.get("content") or ""
                    if not text:
                        continue
                    full_text_parts.append(text)
                    delta: Dict[str, Any] = {"content": text}
                    if not sent_role:
                        delta["role"] = "assistant"
                        sent_role = True
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif mtype == "done":
                    finish_reason = msg.get("finish_reason") or "stop"
                    break

            self._last_used = time.monotonic()
            self._schedule_idle_unload()

        stop_chunk: Dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
        yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"

        if include_usage:
            comp_tokens = max(1, len("".join(full_text_parts)) // 4)
            usage_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt_tokens_est,
                    "completion_tokens": comp_tokens,
                    "total_tokens": prompt_tokens_est + comp_tokens,
                },
            }
            yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    async def handle_if_local(
        self,
        req: Any,
        get_content_text: Callable[[Any], str],
        estimate_tokens: Callable[[str], int],
    ) -> Optional[Any]:
        route = classify_local_request(req, get_content_text, estimate_tokens)
        if route is None:
            return None

        worker_messages = self._build_worker_messages(req, get_content_text)
        prompt_tokens_est = estimate_tokens("\n".join(m["content"] for m in worker_messages))
        max_tokens = req.max_completion_tokens or req.max_tokens or (48 if route == "session_title" else 512)
        temperature = 0.1 if route == "session_title" else (req.temperature if req.temperature is not None else 0.3)
        response_format = {"type": "json_object"} if route == "session_title" else req.response_format
        req_model = req.model or LOCAL_MODEL_NAME
        include_usage = bool(req.stream_options and req.stream_options.get("include_usage"))

        print(f"🧠 [LocalRouter] Route={route} | model={req_model} -> {LOCAL_MODEL_FILE}", flush=True)

        payload = {
            "id": uuid.uuid4().hex,
            "messages": worker_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": response_format,
            "stream": bool(req.stream),
        }

        try:
            if req.stream:
                return StreamingResponse(
                    self._stream_events(payload, req_model, include_usage, prompt_tokens_est),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            res = await self._run_non_stream(payload)
            content = res.get("content") or ""
            if route == "session_title":
                try:
                    parsed = json.loads(content)
                    if not isinstance(parsed, dict) or not isinstance(parsed.get("title"), str) or not parsed["title"].strip():
                        content = '{"title": "Gemini Session"}'
                    else:
                        content = json.dumps({"title": parsed["title"].strip()}, ensure_ascii=False)
                except Exception:
                    content = '{"title": "Gemini Session"}'
            usage = res.get("usage") or {}
            p_tok = int(usage.get("prompt_tokens") or prompt_tokens_est)
            c_tok = int(usage.get("completion_tokens") or estimate_tokens(content))
            return {
                "id": f"chatcmpl-local-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": res.get("finish_reason") or "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": p_tok,
                    "completion_tokens": c_tok,
                    "total_tokens": p_tok + c_tok,
                },
            }
        except Exception as exc:
            print(f"⚠️ [LocalRouter] Local inference failed ({exc})", flush=True)
            if route == "session_title":
                fallback_title = '{"title": "Gemini Session"}'
                return {
                    "id": f"chatcmpl-title-{int(time.time() * 1000)}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": req_model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": fallback_title},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            raise


local_router = LocalWorkerManager()

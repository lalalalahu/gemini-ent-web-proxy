import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _ensure_utf8_stdio() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _resolve_model_path(repo_id: str, filename: str, model_dir: Path) -> Path:
    model_dir.mkdir(parents=True, exist_ok=True)
    target = model_dir / filename
    if target.exists() and target.stat().st_size > 0:
        return target

    from huggingface_hub import hf_hub_download

    downloaded = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        local_dir=str(model_dir),
    )
    return Path(downloaded)


def _resolve_threads(raw_threads: int) -> int:
    if raw_threads > 0:
        return raw_threads
    cpu_count = os.cpu_count() or 4
    return max(1, min(8, cpu_count // 2 if cpu_count >= 8 else cpu_count))


def main() -> int:
    _ensure_utf8_stdio()

    repo_id = os.getenv("LOCAL_MODEL_REPO", "Qwen/Qwen2.5-0.5B-Instruct-GGUF")
    filename = os.getenv("LOCAL_MODEL_FILE", "qwen2.5-0.5b-instruct-q4_k_m.gguf")
    model_dir = Path(os.getenv("LOCAL_MODEL_DIR", str(Path.home() / ".gemini-service" / "models")))
    n_ctx = int(os.getenv("LOCAL_N_CTX", "4096"))
    n_threads = _resolve_threads(int(os.getenv("LOCAL_N_THREADS", "0")))

    try:
        model_path = _resolve_model_path(repo_id, filename, model_dir)
        from llama_cpp import Llama

        llm = Llama(
            model_path=str(model_path),
            n_ctx=n_ctx,
            n_threads=n_threads,
            verbose=False,
        )
        _emit({
            "type": "ready",
            "model_path": str(model_path),
            "n_ctx": n_ctx,
            "n_threads": n_threads,
        })
    except Exception as exc:
        _emit({"type": "init_error", "error": str(exc)})
        return 1

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            _emit({"id": None, "type": "error", "error": f"Invalid JSON: {exc}"})
            continue

        req_id = req.get("id")
        if req.get("type") == "shutdown":
            _emit({"id": req_id, "type": "shutdown_ack"})
            break

        messages: List[Dict[str, str]] = req.get("messages") or []
        max_tokens: Optional[int] = req.get("max_tokens") or 256
        temperature: float = float(req.get("temperature", 0.2))
        stream: bool = bool(req.get("stream", False))
        response_format: Optional[Dict[str, Any]] = req.get("response_format")

        kwargs: Dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if isinstance(response_format, dict) and response_format.get("type") == "json_object":
            kwargs["response_format"] = {"type": "json_object"}

        try:
            if stream:
                finish_reason = "stop"
                for chunk in llm.create_chat_completion(**kwargs):
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        _emit({"id": req_id, "type": "delta", "content": text})
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                _emit({"id": req_id, "type": "done", "finish_reason": finish_reason})
            else:
                res = llm.create_chat_completion(**kwargs)
                choice = (res.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content") or ""
                usage = res.get("usage") or {}
                _emit({
                    "id": req_id,
                    "type": "result",
                    "content": content,
                    "finish_reason": choice.get("finish_reason") or "stop",
                    "usage": usage,
                })
        except Exception as exc:
            _emit({"id": req_id, "type": "error", "error": str(exc)})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

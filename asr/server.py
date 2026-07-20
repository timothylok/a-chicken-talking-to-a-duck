"""Local ASR service (Win11): POST /inference with audio -> Cantonese transcription JSON.

Run:  python server.py   (listens on 127.0.0.1:9000, reachable only via the Cloudflare Tunnel)
Env:  ASR_MODEL (default "medium"), ASR_LANGUAGE (default "yue"), ASR_PORT (default 9000)
"""

import importlib.util
import io
import logging
import os
import sys
import threading
import time

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from faster_whisper import WhisperModel
from starlette.concurrency import run_in_threadpool

from router import COMMANDS, record_history, route, status_info, warm_ollama

MODEL_SIZE = os.environ.get("ASR_MODEL", "medium")
REQUESTED_LANGUAGE = os.environ.get("ASR_LANGUAGE", "yue")
PORT = int(os.environ.get("ASR_PORT", "9000"))
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_DURATION_SECONDS = 120

# The yue language token only exists in large-v3; older models handle Cantonese under zh.
LANGUAGE = REQUESTED_LANGUAGE
if REQUESTED_LANGUAGE == "yue" and "large-v3" not in MODEL_SIZE:
    LANGUAGE = "zh"

# Under NSSM stderr defaults to cp1252, which turns Cantonese log lines into
# \uXXXX escapes; force UTF-8 so transcriptions and replies stay readable.
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("asr")

def _register_cuda_dlls():
    # pip-installed NVIDIA wheels (nvidia-cublas-cu12, nvidia-cudnn-cu12) put their
    # DLLs inside site-packages, not on PATH; ctranslate2 needs them registered.
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        spec = importlib.util.find_spec(pkg)
        if spec and spec.submodule_search_locations:
            bin_dir = os.path.join(list(spec.submodule_search_locations)[0], "bin")
            if os.path.isdir(bin_dir):
                os.add_dll_directory(bin_dir)
                # ctranslate2 resolves cuBLAS via plain PATH search, which
                # add_dll_directory does not affect.
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def _load_model() -> tuple[WhisperModel, str]:
    _register_cuda_dlls()
    try:
        m = WhisperModel(MODEL_SIZE, device="cuda", compute_type="int8_float16")
        # cuBLAS/cuDNN load lazily on first inference, not at model load —
        # warm up now so a missing CUDA runtime falls back instead of 500ing.
        segments, _ = m.transcribe(np.zeros(16000, dtype=np.float32), language=LANGUAGE)
        list(segments)
        return m, "cuda"
    except Exception as exc:
        log.warning("CUDA unavailable (%s); using CPU int8", exc)
        return WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8"), "cpu"


model, DEVICE = _load_model()
status_info.update(model=MODEL_SIZE, device=DEVICE, started=time.time())
log.info("model=%s device=%s language=%s", MODEL_SIZE, DEVICE, LANGUAGE)

# Background so a slow/absent Ollama never delays ASR availability.
threading.Thread(target=warm_ollama, daemon=True).start()

# Bias decoding toward the command vocabulary so allowlisted phrases are
# transcribed exactly (e.g. 系統狀態, not the synonym 系統狀況).
# Whisper keeps only the LAST ~223 prompt tokens. Joining every variant
# phrase had grown the prompt to 880 tokens, silently dropping the first
# three-quarters of the command vocabulary out of the window (found
# 2026-07-18). Bias only needs the canonical form Whisper should emit — the
# router still matches every variant — so take the first CJK phrase per
# command. The freed budget adds chat vocabulary the persona invites users
# to speak: 尊嚴 was transcribed as its near-homophone 專業.
INITIAL_PROMPT = "以下係廣東話指令或者問題。" + "。".join(
    next(p for p in spec["phrases"] if not p.isascii())
    for spec in COMMANDS.values()
) + "。確認。取消。人最緊要係尊嚴。做人如果冇夢想，同條鹹魚有咩分別呀。老豆即係爸爸。"

# Opt-in benchmark capture (ops/asr_bench.py): when ASR_CAPTURE_DIR is set,
# every request's audio + transcript is saved there for offline model
# comparison. Audio never leaves this machine; unset the variable (and
# restart) to stop capturing, delete the directory to discard.
CAPTURE_DIR = os.environ.get("ASR_CAPTURE_DIR", "")
if CAPTURE_DIR:
    log.warning("capture mode ON: saving request audio to %s", CAPTURE_DIR)


def _capture(data: bytes, text: str) -> None:
    try:
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        stem = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000:03d}"
        with open(os.path.join(CAPTURE_DIR, stem + ".m4a"), "wb") as f:
            f.write(data)
        with open(os.path.join(CAPTURE_DIR, stem + ".txt"), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as exc:
        log.error("capture failed: %s", exc)


# One request at a time: inference saturates the machine, and overlapping
# transcriptions on a 4 GB GPU will OOM.
inference_lock = threading.Lock()

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_SIZE, "device": DEVICE, "language": LANGUAGE}


def transcribe(data: bytes) -> dict:
    with inference_lock:
        try:
            segments, info = model.transcribe(
                io.BytesIO(data),
                language=LANGUAGE,
                vad_filter=True,
                initial_prompt=INITIAL_PROMPT,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="could not decode audio") from exc
        if info.duration > MAX_DURATION_SECONDS:
            raise HTTPException(
                status_code=413, detail=f"audio longer than {MAX_DURATION_SECONDS}s"
            )
        out = [
            {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text}
            for s in segments
        ]
    result = {
        "text": "".join(s["text"] for s in out).strip(),
        "language": info.language,
        "segments": out,
    }
    if CAPTURE_DIR:
        _capture(data, result["text"])
    return result


async def _read_audio(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            fields = {k: type(v).__name__ for k, v in form.multi_items()}
            log.warning("rejected upload: no usable 'file' field, got %s", fields)
            raise HTTPException(status_code=400, detail="multipart body must include a 'file' field")
        data = await upload.read()
    elif content_type.startswith("audio/"):
        data = await request.body()
    else:
        log.warning("rejected upload: content-type %r", content_type)
        raise HTTPException(status_code=415, detail="expected multipart/form-data or audio/* body")

    if not data:
        log.warning("rejected upload: empty audio body")
        raise HTTPException(status_code=400, detail="empty audio")
    if len(data) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"audio exceeds {MAX_BODY_BYTES} bytes")
    return data


@app.post("/inference")
async def inference(request: Request):
    data = await _read_audio(request)
    return await run_in_threadpool(transcribe, data)


@app.post("/command")
async def command(request: Request):
    lang = "yue"
    if request.headers.get("content-type", "").startswith("application/json"):
        # Text path (scheduled Shortcut automations, e.g. spoken morning
        # briefing): skip ASR and route the text directly.
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json body")
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="json body must include a non-empty 'text'")
        result = {"text": text.strip()[:500]}
        # Channel tag: the Slack bridge, workflow engine, and public web
        # chat mark their requests; other JSON text callers (Shortcut
        # automations) are "text". Audio is always "voice".
        source = body.get("source") if body.get("source") in ("slack", "workflow", "web") else "text"
        # Only the web chat sends lang; every other channel is Cantonese.
        lang = body.get("lang") if body.get("lang") in ("yue", "en") else "yue"
    else:
        data = await _read_audio(request)
        result = await run_in_threadpool(transcribe, data)
        source = "voice"
    outcome = await run_in_threadpool(route, result["text"], source, lang)
    record_history(result["text"], outcome, source)
    # Phones just speak the reply; workflows need data for conditions, and
    # the Slack bridge needs it for image uploads.
    if source not in ("workflow", "slack"):
        outcome.pop("data", None)
    log.info("command [%s]: %r -> %s (%s)", source, result["text"], outcome["command"], outcome["status"])
    return {"text": result["text"], **outcome}


if __name__ == "__main__":
    # localhost only — the Cloudflare Tunnel is the sole way in.
    uvicorn.run(app, host="127.0.0.1", port=PORT)

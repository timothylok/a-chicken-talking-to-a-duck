"""Local ASR service (Win11): POST /inference with audio -> Cantonese transcription JSON.

Run:  python server.py   (listens on 127.0.0.1:9000, reachable only via the Cloudflare Tunnel)
Env:  ASR_MODEL (default "medium"), ASR_LANGUAGE (default "yue"), ASR_PORT (default 9000)
"""

import importlib.util
import io
import logging
import os
import threading

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from faster_whisper import WhisperModel
from starlette.concurrency import run_in_threadpool

from router import route

MODEL_SIZE = os.environ.get("ASR_MODEL", "medium")
REQUESTED_LANGUAGE = os.environ.get("ASR_LANGUAGE", "yue")
PORT = int(os.environ.get("ASR_PORT", "9000"))
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_DURATION_SECONDS = 120

# The yue language token only exists in large-v3; older models handle Cantonese under zh.
LANGUAGE = REQUESTED_LANGUAGE
if REQUESTED_LANGUAGE == "yue" and "large-v3" not in MODEL_SIZE:
    LANGUAGE = "zh"

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
log.info("model=%s device=%s language=%s", MODEL_SIZE, DEVICE, LANGUAGE)

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
                io.BytesIO(data), language=LANGUAGE, vad_filter=True
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
    return {
        "text": "".join(s["text"] for s in out).strip(),
        "language": info.language,
        "segments": out,
    }


async def _read_audio(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(status_code=400, detail="multipart body must include a 'file' field")
        data = await upload.read()
    elif content_type.startswith("audio/"):
        data = await request.body()
    else:
        raise HTTPException(status_code=415, detail="expected multipart/form-data or audio/* body")

    if not data:
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
    data = await _read_audio(request)
    result = await run_in_threadpool(transcribe, data)
    outcome = route(result["text"])
    log.info("command: %r -> %s (%s)", result["text"], outcome["command"], outcome["status"])
    return {"text": result["text"], **outcome}


if __name__ == "__main__":
    # localhost only — the Cloudflare Tunnel is the sole way in.
    uvicorn.run(app, host="127.0.0.1", port=PORT)

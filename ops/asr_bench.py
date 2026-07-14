"""Benchmark ASR models against captured real utterances (hardening-checklist
"validate Cantonese accuracy" item).

Workflow:
  1. Set ASR_CAPTURE_DIR (service env) to e.g. asr\\logs\\capture and restart
     VoiceASR — every request's audio + transcript is then saved there.
  2. Speak the test phrases once each through the normal iPhone Shortcut.
  3. asr\\.venv\\Scripts\\python.exe ops/asr_bench.py asr/logs/capture
     (the service venv — the system Python lacks faster-whisper)
     First run writes labels.tsv pre-filled with the live model's transcripts —
     correct any line where the transcript is not what you actually said.
  4. Re-run. Each model transcribes every clip (same prompt/VAD settings as the
     service); reports command-routing accuracy (the metric that matters),
     exact-match rate, and character error rate.
  5. Unset ASR_CAPTURE_DIR and restart; delete the capture dir when done.

Runs on CPU by default so it never fights the live service for the 4 GB GPU.
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "asr"))

from router import COMMANDS, REMINDER_PREFIXES, _normalize  # noqa: E402

DEFAULT_MODELS = [
    "medium",  # current production baseline
    "JackyHoCL/whisper-small-cantonese-yue-english-ct2",  # Cantonese+English fine-tune
]

# Same biasing prompt the service builds (server.py).
INITIAL_PROMPT = "以下係廣東話指令或者問題。" + "。".join(
    p for spec in COMMANDS.values() for p in spec["phrases"] if not p.isascii()
) + "。確認。取消。"

AUDIO_EXTS = (".m4a", ".wav", ".mp3", ".mp4", ".ogg", ".flac")


def match_command(text: str) -> str | None:
    # route()'s matching logic without execution or confirmation state.
    phrase = _normalize(text)
    if not phrase:
        return None
    if phrase.startswith(REMINDER_PREFIXES):
        return "CREATE_REMINDER"
    for command_id, spec in COMMANDS.items():
        if phrase in (_normalize(p) for p in spec["phrases"]):
            return command_id
    return None


def cer(ref: str, hyp: str) -> float:
    # Character error rate = edit distance / len(ref), on normalized text.
    ref, hyp = _normalize(ref), _normalize(hyp)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, rc in enumerate(ref, 1):
        cur = [i]
        for j, hc in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(ref)


def load_labels(capture_dir: str) -> dict[str, str]:
    labels_path = os.path.join(capture_dir, "labels.tsv")
    clips = sorted(
        f for f in os.listdir(capture_dir) if f.lower().endswith(AUDIO_EXTS)
    )
    if not clips:
        sys.exit(f"no audio clips in {capture_dir} — capture some first (see docstring)")

    if not os.path.exists(labels_path):
        with open(labels_path, "w", encoding="utf-8", newline="\n") as f:
            for clip in clips:
                sidecar = os.path.join(capture_dir, os.path.splitext(clip)[0] + ".txt")
                text = ""
                if os.path.exists(sidecar):
                    with open(sidecar, encoding="utf-8") as s:
                        text = s.read().strip()
                f.write(f"{clip}\t{text}\n")
        sys.exit(
            f"wrote {labels_path} pre-filled with the live model's transcripts.\n"
            "Fix any line where that is not what you actually said, then re-run."
        )

    labels = {}
    with open(labels_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line and "\t" in line:
                name, text = line.split("\t", 1)
                labels[name] = text.strip()
    missing = [c for c in clips if c not in labels]
    if missing:
        sys.exit(f"labels.tsv has no line for: {', '.join(missing)}")
    return labels


def bench_model(name: str, capture_dir: str, labels: dict[str, str],
                device: str, language: str) -> dict:
    from faster_whisper import WhisperModel

    print(f"\n=== {name} ({device}, language={language}) ===")
    t0 = time.time()
    model = WhisperModel(name, device=device,
                         compute_type="int8" if device == "cpu" else "int8_float16")
    print(f"loaded in {time.time() - t0:.1f}s")

    rows, routed_ok, exact_ok, cer_total, infer_total = [], 0, 0, 0.0, 0.0
    for clip, expected in labels.items():
        t1 = time.time()
        segments, _ = model.transcribe(
            os.path.join(capture_dir, clip),
            language=language,
            vad_filter=True,
            initial_prompt=INITIAL_PROMPT,
        )
        got = "".join(s.text for s in segments).strip()
        infer_total += time.time() - t1
        want_cmd, got_cmd = match_command(expected), match_command(got)
        routed = want_cmd == got_cmd
        exact = _normalize(expected) == _normalize(got)
        routed_ok += routed
        exact_ok += exact
        cer_total += cer(expected, got)
        rows.append((clip, expected, got, want_cmd, got_cmd, routed))
        mark = "ok " if routed else "MISS"
        print(f"  [{mark}] {clip}: {expected!r} -> {got!r} ({want_cmd} -> {got_cmd})")

    n = len(labels)
    summary = {
        "model": name,
        "routing": routed_ok / n,
        "exact": exact_ok / n,
        "cer": cer_total / n,
        "sec_per_clip": infer_total / n,
    }
    print(f"routing {routed_ok}/{n} ({summary['routing']:.0%})  "
          f"exact {exact_ok}/{n} ({summary['exact']:.0%})  "
          f"mean CER {summary['cer']:.2f}  "
          f"{summary['sec_per_clip']:.1f}s/clip")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", help="directory of captured clips (+ labels.tsv)")
    parser.add_argument("--model", action="append", dest="models",
                        help=f"repeatable; default: {DEFAULT_MODELS}")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    parser.add_argument("--language", default="zh",
                        help="decode language (default zh; yue needs large-v3-based models)")
    args = parser.parse_args()

    labels = load_labels(args.capture_dir)
    print(f"{len(labels)} labelled clips in {args.capture_dir}")
    summaries = [
        bench_model(m, args.capture_dir, labels, args.device, args.language)
        for m in (args.models or DEFAULT_MODELS)
    ]

    print("\n=== summary ===")
    print(f"{'model':55} {'routing':>8} {'exact':>7} {'CER':>6} {'s/clip':>7}")
    for s in summaries:
        print(f"{s['model']:55} {s['routing']:>8.0%} {s['exact']:>7.0%} "
              f"{s['cer']:>6.2f} {s['sec_per_clip']:>7.1f}")


if __name__ == "__main__":
    main()

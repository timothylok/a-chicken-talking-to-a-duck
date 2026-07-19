"""Image generation CLI: prompt -> PNG via LCM Dreamshaper v7 on CPU.

Run as a subprocess by the GENERATE_IMAGE command (asr/router.py). CPU-only
by design: the 4 GB GPU is fully committed to Whisper plus the pinned
gemma3:4b, and loading a diffusion model there evicts them (chat and the
morning briefing pay a cold reload). LCM needs only a handful of steps,
which keeps CPU inference inside the Slack reply window.
"""

import argparse
import os
import sys

MODEL = "SimianLuo/LCM_Dreamshaper_v7"


def _cached_snapshot() -> str | None:
    # Resolve the warmup-downloaded snapshot folder ourselves: the hub
    # library's offline path demands the *complete* repo (README, unused
    # single-file checkpoints...) and rejects the pipeline-only snapshot
    # that from_pretrained(repo_id) actually downloads.
    hub_dir = os.path.join(
        os.environ.get("HF_HOME", os.path.join(os.path.expanduser("~"), ".cache", "huggingface")),
        "hub", "models--" + MODEL.replace("/", "--"),
    )
    try:
        with open(os.path.join(hub_dir, "refs", "main"), encoding="ascii") as f:
            commit = f.read().strip()
    except OSError:
        return None
    path = os.path.join(hub_dir, "snapshots", commit)
    return path if os.path.isdir(path) else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt")
    parser.add_argument("--out")
    parser.add_argument(
        "--warmup", action="store_true",
        help="download/load the model only; run as the user so the service "
             "account never needs HF cache write access",
    )
    args = parser.parse_args()
    if not args.warmup and not (args.prompt and args.out):
        parser.error("--prompt and --out are required unless --warmup")

    source = MODEL
    if not args.warmup:
        # After the one-time warmup download the service runs fully offline;
        # this also fails fast if the cache is missing instead of hanging on
        # a network fetch under the service account.
        os.environ["HF_HUB_OFFLINE"] = "1"
        source = _cached_snapshot()
        if source is None:
            print("model not cached; run image_gen.py --warmup as the user first", file=sys.stderr)
            return 1

    import torch  # noqa: E402 (heavy imports after the offline env is set)
    from diffusers import DiffusionPipeline

    pipe = DiffusionPipeline.from_pretrained(source, torch_dtype=torch.float32)
    pipe.to("cpu")
    pipe.set_progress_bar_config(disable=True)
    if args.warmup:
        print("warmup ok")
        return 0

    image = pipe(
        prompt=args.prompt, num_inference_steps=6, guidance_scale=8.0,
        height=512, width=512,
    ).images[0]
    image.save(args.out, format="PNG")
    return 0


if __name__ == "__main__":
    sys.exit(main())

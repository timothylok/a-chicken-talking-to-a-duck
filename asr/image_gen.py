"""Image generation CLI: prompt -> PNG via DreamShaper 8 LCM on CPU.

Run as a subprocess by the GENERATE_IMAGE command (asr/router.py). CPU-only
by design: the 4 GB GPU is fully committed to Whisper plus the pinned
gemma3:4b, and loading a diffusion model there evicts them (chat and the
morning briefing pay a cold reload). Distilled few-step models are what make
CPU inference fit the Slack reply window at all.

Model history, all benchmarked on this box 2026-09-09. LCM Dreamshaper v7 at
6 steps -> sd-turbo at 4 -> here. sd-turbo was faster but its SD 2.1 base has
weak style defaults and, being distilled without classifier-free guidance
(guidance_scale must be 0.0), nothing pulls it back on-prompt: "a cat on the
moon eating Mooncake" came out as a needle-felted textile cat. DreamShaper 8
LCM keeps Dreamshaper's subject quality at ~21 s/image, near sd-turbo's ~20 s.

**LCMScheduler is mandatory** — `from_pretrained` gives this repo a plain
StableDiffusionPipeline on PNDMScheduler, and LCM weights under PNDM at 4
steps produce etched noise. guidance_scale stays at 1.0: above 1.0 diffusers
switches on real classifier-free guidance, which doubles UNet passes per step
(36 s -> 58 s at 8 steps) for no gain on an LCM.
"""

import argparse
import os
import sys

MODEL = "Lykon/dreamshaper-8-lcm"


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
    from diffusers import AutoPipelineForText2Image, LCMScheduler

    # safety_checker off: it blanks anything it rejects to pure black, which
    # this CLI then saved and reported as success — a black square with a
    # "畫好喇" reply. Private single-user Slack bot, and dropping it also frees
    # ~1.2 GB of RAM per run (it is not even downloaded).
    pipe = AutoPipelineForText2Image.from_pretrained(
        source, torch_dtype=torch.float32,
        safety_checker=None, requires_safety_checker=False,
    )
    # Not optional — see the module docstring. The repo ships PNDM.
    pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
    pipe.to("cpu")
    pipe.set_progress_bar_config(disable=True)
    if args.warmup:
        print("warmup ok")
        return 0

    image = pipe(
        prompt=args.prompt, num_inference_steps=4, guidance_scale=1.0,
        height=512, width=512,
    ).images[0]
    # An all-zero frame is a failed generation, not a picture — belt and braces
    # now the safety checker is off, since CPU fp32 can still collapse to black.
    # Exiting non-zero routes it to the caller's existing failure reply instead
    # of posting the black square as a finished drawing.
    if image.getbbox() is None:
        print("generation produced an all-black image", file=sys.stderr)
        return 1
    image.save(args.out, format="PNG")
    return 0


if __name__ == "__main__":
    sys.exit(main())

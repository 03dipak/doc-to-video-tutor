"""doc-to-video-tutor — turn design docs / PPT / transcripts into lessons.

Usage:
    uv run doc-to-video-tutor doc/design/03_lld_tests.md -a         # audio mp3
    uv run doc-to-video-tutor doc/design/03_lld_tests.md -v         # video mp4
    uv run doc-to-video-tutor slides.pptx --lang hi -v              # PPT -> video
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
TTS_VOICE = os.getenv("TTS_VOICE", "hi-IN-SwaraNeural")

TUTOR_PROMPT = """You are a friendly AI tutor teaching a student who is NEW to this field.
Explain the content below in simple {lang}-English mix, like a classroom lesson.

Aim for a spoken duration of roughly {target_minutes} minutes in total
(about 130 words per minute when read aloud).

Structure your answer into 5-8 sections with clear headings, covering:
1) What is this?
2) Why do we need it?
3) How does it work? (break into 2-4 short parts)
4) Real-world example / analogy
5) Key takeaways

Rules:
- Use simple words; avoid jargon without explaining it.
- Repeat the most important idea at least twice (learning reinforcement).
- Use at least one real-life analogy.
- Be patient and encouraging. NEVER say "as described above" or "as mentioned".
- Write self-contained spoken text — no markdown tables, no code blocks.

Content to teach:
{content}"""

LANG_HINT = {
    "hi": "Hindi",
    "mr": "Marathi",
    "en": "English",
}


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def load_content(paths: list[str], max_chars: int = 12000) -> str:
    """Read .md/.txt transcripts or .pptx decks; combine multiple inputs.

    Returns labelled sections so the tutor knows which doc each part came from.
    """
    parts: list[str] = []
    for path in paths:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Input not found: {path}")

        if p.suffix.lower() in (".pptx", ".ppt"):
            body = _load_pptx(p, max_chars)
        else:
            body = p.read_text(encoding="utf-8")[:max_chars]

        parts.append(f"<doc name='{p.stem}'>\n{body}\n</doc>")
    return "\n\n".join(parts)[:max_chars * 4]


def _load_pptx(p: Path, max_chars: int) -> str:
    from pptx import Presentation

    prs = Presentation(str(p))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
        if texts:
            parts.append(f"--- Slide {i} ---\n" + "\n".join(texts))
    joined = "\n\n".join(parts)
    if not joined.strip():
        raise ValueError(f"No slide text found in {p}")
    return joined[:max_chars]


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def ask_llm(prompt: str) -> str:
    """OpenAI-compatible chat completion against LLM_BASE_URL."""
    if not (LLM_BASE_URL and LLM_API_KEY and LLM_MODEL):
        raise RuntimeError(
            "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL not set. "
            "Copy .env.example to .env and fill in values."
        )
    # Some providers ship the /v1 segment inside LLM_BASE_URL already, so we
    # call the OpenAI-style endpoint WITHOUT a hardcoded /v1 prefix.
    url = f"{LLM_BASE_URL}/chat/completions"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        },
        timeout=300,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise RuntimeError(f"LLM returned non-string content: {type(content)!r}")
    return content.strip()


def generate_lesson(content: str, lang: str, target_minutes: float) -> str:
    hint = LANG_HINT.get(lang, "Hindi")
    print(f"Calling {LLM_MODEL} to generate a {hint} lesson (~{target_minutes:.0f} min)...")
    return ask_llm(
        TUTOR_PROMPT.format(lang=hint, target_minutes=target_minutes, content=content)
    )


# ---------------------------------------------------------------------------
# Audio + video
# ---------------------------------------------------------------------------

async def _text_to_audio(text: str, out_path: Path, voice: str) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))


def text_to_audio(text: str, out_path: Path, voice: str | None = None) -> None:
    asyncio.run(_text_to_audio(text, out_path, voice or TTS_VOICE))


def _split_sections(text: str, n: int) -> list[str]:
    """Split lesson into n sections on blank lines / headings, left-to-right."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        return [text]
    if len(blocks) <= n:
        return blocks
    # merge remainder into the last n chunks proportionally
    base = len(blocks) // n
    out: list[str] = []
    i = 0
    for k in range(n):
        take = base + (1 if k < len(blocks) % n else 0)
        out.append("\n\n".join(blocks[i : i + take]))
        i += take
    return [b for b in out if b.strip()]


def _make_slide_image(text: str, index: int, slides_dir: Path) -> Path:
    """Render a presentable slide: header bar, title, wrapped body, footer."""
    from PIL import Image, ImageDraw

    W, H = 1280, 720
    BG, ACCENT, FG, MUTED = (18, 24, 38), (66, 133, 244), (235, 238, 245), (150, 158, 175)

    img = Image.new("RGB", (W, H), color=BG)
    draw = ImageDraw.Draw(img)

    # header accent bar
    draw.rectangle([0, 0, W, 8], fill=ACCENT)
    draw.text((40, 28), f"Lesson {index + 1}", fill=MUTED)

    # first non-empty line is the section heading
    body = text.strip().splitlines()
    title = body[0].strip() if body else ""
    rest = "\n".join(body[1:]).strip()

    draw.text((40, 70), title[:60] or "Lesson", fill=(255, 255, 255))
    draw.line([40, 130, W - 40, 130], fill=ACCENT, width=2)

    # word-wrap body
    words = rest.split()
    lines: list[str] = []
    current = ""
    for w in words:
        probe = f"{current} {w}".strip()
        if len(probe) > 62:
            lines.append(current)
            current = w
        else:
            current = probe
    lines.append(current)

    y = 160
    for line in lines[:16]:
        draw.text((40, y), line, fill=FG)
        y += 40

    draw.text((40, H - 50), "doc-to-video-tutor — learn by listening", fill=MUTED)

    out = slides_dir / f"slide_{index:02d}.png"
    img.save(out)
    return out


def create_video(text: str, audio_path: Path, out_path: Path) -> None:
    """Combine several text slides + one narration audio into an mp4."""
    from moviepy import AudioFileClip, CompositeVideoClip, ImageClip

    audio = AudioFileClip(str(audio_path))
    sections = _split_sections(text, max(3, int(audio.duration / 20)))

    slides_dir = Path("slides_tmp")
    slides_dir.mkdir(exist_ok=True)
    images = [_make_slide_image(s, i, slides_dir) for i, s in enumerate(sections)]

    per_slide = audio.duration / len(images)
    clips = []
    for i, img in enumerate(images):
        clip = ImageClip(str(img)).with_duration(per_slide)
        clip = clip.with_start(i * per_slide)
        clips.append(clip)

    video = CompositeVideoClip(clips)
    video = video.with_audio(audio)
    video.write_videofile(str(out_path), fps=24, logger=None)
    print(f"Video written: {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# `doc-to-video-tutor` is DEPRECATED. It is kept for one release cycle as a
# translating shim, then removed. It is not repointed silently, and it is not
# deleted outright: someone's shell alias or CI script calls this name today.
#
# Why it had to change: this path contains NO quality machinery - no grounding
# gate, no narration contract, no repeat enforcement, no layout audit. The LLD
# invariant is that damaged narration can never reach TTS or video, and a
# documented live command that bypasses every gate contradicts it. The README
# documented this command, so the gap was reachable by following the docs.
#
# `--lang` never existed on `studio`; it maps exactly onto `--narr-voice`
# (`hi`/`mr` -> `mhe-mix`, `en` -> `english`), so the shim translates it and
# names the replacement. Anything it cannot translate raises rather than being
# ignored.
_LANG_TO_VOICE = {"hi": "mhe-mix", "mr": "mhe-mix", "en": "english"}


def main() -> None:
    """Deprecated entry point. Translates to `doc-to-studio` and delegates."""
    parser = argparse.ArgumentParser(
        prog="doc-to-video-tutor",
        description="DEPRECATED - use `doc-to-studio build`. This command is a "
                    "translating shim and will be removed in one release.",
        epilog="Replacement: doc-to-studio build <inputs> [--minutes N] "
               "[--out BASE] [--voice V] [--narr-voice mhe-mix|english] "
               "[--skip-video]",
    )
    parser.add_argument("inputs", nargs="+", help="path(s) to .md, .txt, or .pptx")
    parser.add_argument(
        "-a", "--audio", action="store_true", help="output audio only (default)")
    parser.add_argument(
        "-v", "--video", action="store_true",
        help="DEPRECATED and ignored: studio always renders video unless "
             "--skip-video is passed")
    parser.add_argument("--lang", default=None, choices=("hi", "mr", "en"),
                        help="mapped to --narr-voice")
    parser.add_argument("--voice", default=None, help="override TTS voice")
    parser.add_argument("--minutes", type=float, default=None,
                        help="target spoken length (studio default 3.0 min)")
    parser.add_argument("--out", default=None, help="output base path")
    args = parser.parse_args()

    if args.video:
        print("  NOTE: -v/--video is now the default; the flag is accepted and "
              "ignored rather than rejected, so existing invocations still run.",
              file=sys.stderr)

    argv: list[str] = ["build", *args.inputs]
    if args.minutes is not None:
        argv += ["--minutes", str(args.minutes)]
    if args.out is not None:
        argv += ["--out", args.out]
    if args.voice:
        argv += ["--voice", args.voice]
    if args.lang:
        argv += ["--narr-voice", _LANG_TO_VOICE[args.lang]]
    if args.audio:
        argv += ["--skip-video"]

    print("=" * 72, file=sys.stderr)
    print("  DEPRECATED: `doc-to-video-tutor` is the legacy, UNGATED pipeline.",
          file=sys.stderr)
    print("  It has no grounding gate, narration contract or layout audit. It is",
          file=sys.stderr)
    print("  being retired; this invocation is being handed to `doc-to-studio`.",
          file=sys.stderr)
    if args.lang:
        print(f"  --lang {args.lang} -> --narr-voice "
              f"{_LANG_TO_VOICE[args.lang]}", file=sys.stderr)
    print("  Update the call site: doc-to-studio "
          + " ".join(("-a/--audio was dropped" if False else "") or argv[1:]),
          file=sys.stderr)
    print("=" * 72, file=sys.stderr)

    from .studio.cli import main as studio_main
    studio_main(argv)

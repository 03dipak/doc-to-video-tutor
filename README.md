# doc-to-video-tutor

Convert design docs, PPT slides, or transcripts into beginner-friendly **audio or
video lessons** in Hindi / Marathi / English — using your existing LLM.

## Setup

```bash
# fill in your LLM credentials (any OpenAI-compatible endpoint)
cp .env.example .env

# install dependencies
uv sync
```

## Usage

`doc-to-studio` is the pipeline. `doc-to-video-tutor` is **deprecated** - see
[Deprecation](#deprecation) below.

```bash
# video lesson (slides + narration) - the default output
uv run doc-to-studio build doc/design/03_lld_tests.md

# audio only
uv run doc-to-studio build doc/design/03_lld_tests.md --skip-video

# English narration rather than the MHE mix
uv run doc-to-studio build slides.pptx --narr-voice english

# Marathi/Hindi narration is the default profile
uv run doc-to-studio build slides.pptx --narr-voice mhe-mix

# STUDIO: high-quality synced video + PPTX deck from several docs.
# (`uv run doc-to-studio ...` is an alias for the same command.)
uv run python -m doc_to_video_tutor.studio build \
  modules/08_concepts_mod03_gates.md \
  --minutes 4 --out studio_lesson

# STUDIO fast loop: plan + PPTX deck + scene audios only, no mp4. Inspect the
# saved .plan.json, then `verify` / `render` when the narration is clean.
uv run python -m doc_to_video_tutor.studio build \
  modules/08_concepts_mod03_gates.md \
  --minutes 4.0 --skip-video --out mod03_gates

# deterministic QA on a saved plan (no LLM / no render): writes a *_fixed.plan.json
uv run python -m doc_to_video_tutor.studio verify output/mod03_gates.plan.json

# render media (mp4 + pptx + audios) from a verified plan
uv run python -m doc_to_video_tutor.studio render \
  output/mod03_gates_fixed.plan.json --out mod03_gates
```

Or use the `Makefile` wrappers, which default to `modules/08_concepts_mod03_gates.md`:

```bash
make plan      # build --skip-video from the source doc
make verify    # deterministic QA on the saved plan
make build     # full render from the verified plan
make test lint typecheck
```

All generated files land in `output/` (created automatically). A full studio
`build` writes `output/<name>.mp4` + `output/<name>.pptx`; a `--skip-video`
build writes the `.pptx`, the scene audios, and `output/<name>.plan.json`
instead.

## Studio (`python -m doc_to_video_tutor.studio`)

Mirrors the production lesson pipeline:

| Stage | Component |
|---|---|
| **Director** | LLM reads the docs and plans a **structured lesson plan** (JSON) validated against a strict Pydantic schema: title, opening, one scene per source concept, takeaways |
| **Scriptwriter** | a **dedicated narration pass** (`NARRATION_PROMPT`) writes the spoken track in a **Marathi-Hindi-English mix** from the finished plan — the planner no longer writes narration — with a deterministic narration builder as fallback; slide content (bullets, steps, flow, analogy) stays **English-only** for clean slides |
| **Visual designer** | Pillow renders styled scene cards: numbered steps, key-point bullets, arrow flow pipelines, analogy cards |
| **Voice actor** | `edge-tts` narrates each scene independently (`--voice` to change) |
| **Editing** | `moviepy` times each slide to exactly its own narration duration — audio and slide stay in sync |
| **Delivery** | `.mp4` (synced video) + `.pptx` (title slide / scene slides / takeaways) |

Subcommands:

| Command | Purpose |
|---|---|
| `build DOC... [--skip-video]` | plan → narration polish → hard gates → deck + audio (+ video unless skipped) |
| `verify PLAN.json` | deterministic pre-build QA (no LLM/render): applies narration + design-decision fixers, prints before/after repeat counts, writes `*_fixed.plan.json` |
| `render PLAN.json --out PREFIX` | render media from a saved plan (deterministic, no LLM); refuses (`exit 1`) when the pre-render hard gates fail |
| `review PLAN.json` | audit a saved plan (narration/topic/quality) against its saved topics and source terms |

Options (studio):

| Flag | Meaning |
|---|---|
| `--minutes N` | target spoken length (default 4.0) |
| `--voice VOICE` | TTS voice override |
| `--narr-voice mhe-mix\|english` | narration language voice (default `mhe-mix`) |
| `--skip-video` | deck + scene audios only, no mp4 |
| `--out PREFIX` | output prefix (`PREFIX.mp4`, `PREFIX.pptx`) |
| `--out-dir DIR` | output folder (default `output/`, created if missing) |
| `--pause SEC` | silent pause between slides (default 3.0) |

## Doc-to-video-tutor (simple tool)

Options:

| Flag | Meaning |
|---|---|
| `-a` / `--audio` | output `lesson.mp3` (default) |
| `-v` / `--video` | output `lesson.mp4` (text slides + narration) |
| `--lang hi\|mr\|en` | explanation language mix (default `hi`) |
| `--minutes N` | target spoken length (default 3.0) |
| `--voice VOICE` | TTS voice override (`hi-IN-SwaraNeural` default) |
| `--out PATH` | output file path |

Note: multiple input files are accepted (they are merged into one lesson).

## How it works

1. **Load** — `.md` / `.txt` read directly; `.pptx` slide text extracted via `python-pptx`
2. **Explain** — the LLM (from `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`) rewrites
   the content as a 3-part classroom lesson (What / Why / How) with an analogy
3. **Speak** — `edge-tts` narrates in Hindi / Marathi / English (free)
4. **Render (optional)** — `moviepy` puts the narration over text slides to make an mp4

## Requirements

- An OpenAI-compatible LLM endpoint (`LLM_BASE_URL`) — e.g. Qwen 7B local, Ollama
- 8GB+ VRAM for Qwen 7B; use `qwen2.5:3b` for CPU-only
- Network access to Microsoft Edge TTS servers for `edge-tts`
## Deprecation

`doc-to-video-tutor` is the legacy path and is being retired. It still works, and
it now **hands off to `doc-to-studio`**, so existing scripts and shell aliases
keep running - but it prints a deprecation banner and will be removed in one
release.

It is being retired because it contains **no quality gates**: no grounding
check, no narration contract, no repeat enforcement, no layout audit. The design
contract states that damaged narration can never reach TTS or video, and this
command bypassed every check that enforces it.

The old flags are translated, so nothing is silently dropped:

| deprecated | replacement |
|---|---|
| `doc.md` (positional) | unchanged |
| `-a` / `--audio` | `build --skip-video` |
| `-v` / `--video` | accepted and ignored - video is the default now |
| `--lang hi` / `mr` | `--narr-voice mhe-mix` |
| `--lang en` | `--narr-voice english` |
| `--voice`, `--minutes`, `--out` | unchanged |

Anything the shim cannot translate raises rather than being ignored.

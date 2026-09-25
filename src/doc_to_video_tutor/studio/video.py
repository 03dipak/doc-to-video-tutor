"""TTS synthesis and moviepy assembly of frames+audio into the final .mp4."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import LOUDNESS_LRA, LOUDNESS_TARGET, LOUDNESS_TP, TITLE_HOLD, TTS_LOUDNORM, TTS_VOICE
from .pptx import build_pptx
from .slides import _render_title_card, render_scenes
from .text import _nar_tokens
from .util import _Progress

# edge-tts reports boundary offsets in 100-nanosecond ticks.
_TICKS_PER_SECOND = 10_000_000
# Shortest a reveal variant may be held: a sub-second slide reads as a flash,
# which the no-sub-1.5s-flash rule in the media contract forbids.
_MIN_VARIANT_SECONDS = 0.8


async def _scene_audio(text: str, out_path: Path, voice: str,
                       rate: str = "-8%", pitch: str = "+0Hz",
                       volume: str = "+0%") -> list[dict]:
    """Synthesize one clip and return its per-word timings.

    `edge-tts` already emits a `WordBoundary` event per word (offset, duration,
    text) when the stream is consumed with `boundary="WordBoundary"`, and it
    applies offset compensation across chunked input so the timings stay
    continuous for the whole narration. Writing the audio with `.save()` throws
    that data away, which is why reveal timing had to guess. Consuming the stream
    ourselves costs nothing extra and yields the timing for free.
    """
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch,
                                       volume=volume, boundary="WordBoundary")
    timings: list[dict] = []
    with open(out_path, "wb") as handle:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                timings.append({
                    "text": str(chunk.get("text", "")),
                    "start": round(float(chunk.get("offset", 0))
                                   / _TICKS_PER_SECOND, 4),
                    "duration": round(float(chunk.get("duration", 0))
                                      / _TICKS_PER_SECOND, 4),
                })
    return timings


def _normalize_loudness(path: Path) -> bool:
    """One-pass EBU R128 loudness normalization (rule C3) via ffmpeg loudnorm.

    Targets integrated -{LOUDNESS_TARGET} LUFS / {LOUDNESS_TP} dBTP so back-to-back
    scenes don't audibly jump. Returns False (keeps the original file) when
    ffmpeg is unavailable or the filter fails, so TTS output is never destroyed.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("  WARN: ffmpeg not on PATH; skipping loudness normalization")
        return False
    probe_dir = Path(tempfile.mkdtemp(prefix="loudnorm_"))
    tmp = probe_dir / path.name
    limiter_tp = LOUDNESS_TP - 0.5
    peak_limit = max(0.0001, min(1.0, 10 ** (limiter_tp / 20.0)))
    try:
        run = subprocess.run(
            [ffmpeg, "-y",
             "-i", str(path),
             "-af", (f"loudnorm=I={LOUDNESS_TARGET}:TP={LOUDNESS_TP}:"
                     f"LRA={LOUDNESS_LRA},alimiter=limit={peak_limit:.6f}:"
                     "attack=5:release=50:level=disabled"),
             "-ac", "1", "-ar", "44100",
             "-codec:a", "libmp3lame", "-q:a", "2",
             str(tmp)],
            capture_output=True, text=True, timeout=120)
        if run.returncode != 0:
            print(f"  WARN: loudnorm failed for {path.name}; keeping original "
                  f"({run.stderr.strip().splitlines()[-1:]})")
            return False
        os.replace(tmp, path)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  WARN: loudnorm skipped for {path.name}: {exc}")
        return False
    finally:
        with contextlib.suppress(OSError):
            probe_dir.rmdir()


def _clip_voice(script: dict, voice: str | None) -> tuple[str, str, str, str]:
    v = voice or script.get("tts_voice") or TTS_VOICE
    return (v, str(script.get("rate", "-8%")),
            str(script.get("pitch", "+0Hz")),
            str(script.get("volume", "+0%")))


def _bullet_start_times(bullets: list[str], timings: list[dict]) -> list[float]:
    """First-spoken time for each bullet, or -1 when it cannot be located.

    Matching is token-level against the same canonical tokenizer the repeat
    machinery uses, anchored on each bullet's most distinctive token rather than
    a verbatim phrase. That matters: the narration contract deliberately stops
    the model reading bullets verbatim (§7.5), so a multi-word phrase match would
    almost always miss and silently degrade every scene to an even split. A
    single long token survives rephrasing. Bullets that are never spoken, or
    that appear out of order, return -1 and the caller falls back.
    """
    if not bullets or not timings:
        return []
    words = [(float(t.get("start", 0.0)), _nar_tokens(str(t.get("text", ""))))
             for t in timings]
    flat = [tok for _, toks in words for tok in toks]
    starts: list[float] = []
    cursor = 0
    for bullet in bullets:
        tokens = [t for t in _nar_tokens(str(bullet)) if len(t) >= 4]
        if not tokens:
            starts.append(-1.0)
            continue
        anchor = max(tokens, key=len)
        found = -1.0
        for index in range(cursor, len(flat)):
            if flat[index] == anchor:
                found = words[index][0] if index < len(words) else -1.0
                cursor = index + 1
                break
        starts.append(found)
    return starts


def _variant_durations(scene_dur: float, variant_count: int,
                       bullets: list[str] | None = None,
                       timings: list[dict] | None = None) -> list[float]:
    """Split a scene's audio across its reveal variants.

    With word timings the split follows the narration: each variant starts when
    its bullet is actually spoken. Without usable timings - no provider data, a
    bullet that was never spoken, or a degenerate match order - it falls back to
    the previous even split, so this can never shorten or lose audio.
    """
    if variant_count <= 1:
        return [scene_dur]
    per_variant = (scene_dur - min(scene_dur * 0.15, 3.0)) / (variant_count - 1)
    fallback = [min(scene_dur * 0.15, 3.0)] + [per_variant] * (variant_count - 1)
    starts = _bullet_start_times((bullets or [])[:variant_count - 1],
                                 timings or [])
    if len(starts) != variant_count - 1 or any(s < 0 for s in starts):
        return fallback
    # Variant boundaries are the moments each bullet is spoken: the intro holds
    # until bullet one starts, and the last variant runs to the end of the clip.
    bounds: list[float] = []
    previous = 0.0
    for start in starts:
        value = max(start, previous + _MIN_VARIANT_SECONDS)
        if value >= scene_dur:
            return fallback
        bounds.append(value)
        previous = value
    durations = ([bounds[0]]
                 + [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]
                 + [scene_dur - bounds[-1]])
    if len(durations) != variant_count or any(d <= 0 for d in durations):
        return fallback
    return durations


def synth_scenes(script: dict, work_dir: Path, voice: str | None
                 ) -> tuple[list[Path], list[list[dict]]]:
    clips = script.get("clips", [])
    p = _Progress("TTS", len(clips))
    texts = [str(c["spoken"]) for c in clips]
    paths = [work_dir / (f"scene_{c.get('index', i):02d}.mp3")
             for i, c in enumerate(clips)]
    v, rate, pitch, volume = _clip_voice(script, voice)

    async def _all():
        return await asyncio.gather(*(
            _scene_audio(text, path, v, rate, pitch, volume)
            for text, path in zip(texts, paths, strict=True)
        ))

    all_timings = list(asyncio.run(_all()))
    dead = _audit_clip_health(paths)
    normalized = 0
    if TTS_LOUDNORM != "off":
        for clip_path in paths:
            normalized += int(_normalize_loudness(clip_path))
    for i in range(len(paths), 0, -1):
        p._tick(i)
    p.done()
    if TTS_LOUDNORM != "off":
        print(f"  loudness : {LOUDNESS_TARGET} LUFS / {LOUDNESS_TP} dBTP "
              f"({normalized}/{len(paths)} clips via ffmpeg loudnorm)")
    if dead:
        print(f"  WARN: {len(dead)} clip(s) look truncated or silent: "
              f"{', '.join(dead[:4])}")
    return paths, all_timings


def _audit_clip_health(paths: list[Path]) -> list[str]:
    """Flag clips that are effectively silent or carry long dead air.

    `edge-tts` is an unofficial wrapper around a consumer endpoint, so a silent
    breakage (an empty or near-empty MP3) is a real failure mode rather than a
    theoretical one. ffmpeg is already a dependency, so `silencedetect` costs
    nothing extra. This is a warning, not a hard gate: a short but valid clip
    must not fail a build.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    flagged: list[str] = []
    for path in paths:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
                 "-af", "silencedetect=n=-45dB:d=2.0", "-f", "null", "-"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        silence = re.findall(r"silence_duration: ([\d.]+)", result.stderr)
        total = sum(float(value) for value in silence)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size < 1024 or total > 6.0:
            flagged.append(path.name)
    return flagged
def assemble_video(slide_groups: list[list[Path]], audios: list[Path],
                   out_path: Path, pause: float = 3.0, end_hold: float = 6.0,
                   timings: list[list[dict] | None] | None = None,
                   bullets_by_scene: list[list[str] | None] | None = None) -> None:
    from moviepy import (
        AudioClip,
        AudioFileClip,
        CompositeVideoClip,
        ImageClip,
        concatenate_audioclips,
    )
    from moviepy.video.fx import CrossFadeIn

    W, H = 1280, 720

    img_clips, segs = [], []
    offset = 0.0
    last_slide: Path | None = None
    for i, (variants, audio) in enumerate(zip(slide_groups, audios, strict=True)):
        if variants:
            last_slide = variants[-1]
        a = AudioFileClip(str(audio))
        scene_dur = max(a.duration, 0.1)
        if len(variants) == 1:
            parts = [scene_dur]
        else:
            scene_timings = timings[i] if timings and i < len(timings) else None
            scene_bullets = (bullets_by_scene[i]
                             if bullets_by_scene and i < len(bullets_by_scene)
                             else None)
            parts = _variant_durations(scene_dur, len(variants),
                                      scene_bullets, scene_timings)
        for j, (vp, d) in enumerate(zip(variants, parts, strict=True)):
            if j == len(variants) - 1 and i < len(slide_groups) - 1:
                d += pause  # hold the last variant through the silence gap
            clip = ImageClip(str(vp)).with_duration(d).with_start(offset)
            clip = clip.with_effects([CrossFadeIn(0.6)])
            img_clips.append(clip)
            offset += d

        segs.append(a)
        if i < len(slide_groups) - 1:
            segs.append(AudioClip(lambda _t: 0.0, duration=pause, fps=44100))

    if end_hold > 0 and last_slide is not None:
        hold = ImageClip(str(last_slide)).with_duration(end_hold).with_start(offset)
        hold = hold.with_effects([CrossFadeIn(0.6)])
        img_clips.append(hold)
        offset += end_hold
        segs.append(AudioClip(lambda _t: 0.0, duration=end_hold, fps=44100))

    full_audio = concatenate_audioclips(segs)
    video = CompositeVideoClip(img_clips, size=(W, H)).with_audio(full_audio)
    print(f"  Rendering {offset:.1f}s of video @ 720p/24fps ...")
    # Static dark slides are trivially compressible, so ABR ('-b:v') and CRF both
    # collapse to ~10-60 kbps and trip the studio QA bitrate floor. Enforce a
    # CBR-ish stream with 'nal-hrd=cbr' (equal min/max/buf) so the file stays
    # portable and above the gate threshold.
    video.write_videofile(str(out_path), fps=24,
                          ffmpeg_params=["-g", "48", "-b:v", "700k",
                                         "-minrate", "700k", "-maxrate", "700k",
                                         "-bufsize", "700k",
                                         "-x264-params", "nal-hrd=cbr"],
                          logger="bar")
    print(f"  Video written: {out_path} ({offset:.1f}s)")
def _render_media(plan: dict, out_base: Path, script: dict, voice: str | None,
                  skip_video: bool, pause: float, end_hold: float = 6.0) -> None:
    """Deterministic media render from an existing lesson plan (no LLM).

    ``voice`` is the edge-tts engine voice override (None -> profile/env).
    """
    work = Path(tempfile.mkdtemp(prefix="tutor_studio_"))
    print("  [2/5] Rendering slides ...")
    slides = render_scenes(plan, work)
    print("  [3/5] Generating voiceover (TTS) ...")
    audios, timings = synth_scenes(script, work, voice)
    bullets_by_scene = [[str(b) for b in (sc.get("bullets") or [])]
                        for sc in plan.get("scenes", [])]
    word_timings = {"schema_version": 1,
                    "ticks_per_second": _TICKS_PER_SECOND,
                    "provider": script.get("provider", "edge-tts"),
                    "clips": [{"index": int(clip.get("index", i)),
                               "words": words}
                              for i, (clip, words) in enumerate(
                                  zip(script.get("clips", []), timings,
                                      strict=False))]}
    Path(f"{out_base}.word_timings.json").write_text(
        json.dumps(word_timings, indent=2, ensure_ascii=False), encoding="utf-8")
    spoken_words = sum(len(entry["words"]) for entry in word_timings["clips"])
    print(f"  word sync  : {spoken_words} word timings captured "
          f"-> {out_base}.word_timings.json")
    audio_dir = Path(f"{out_base}_audio")
    audio_dir.mkdir(exist_ok=True)
    for a in audios:
        shutil.copy(a, audio_dir / a.name)
    print(f"  [3/5] audio clips kept in {audio_dir}")
    if not skip_video:
        # Cover card prepended so the video mirrors the deck's title slide
        # (same header chrome); held silently, then crossfades into scene 1.
        title_png = work / "title.png"
        _render_title_card(plan, title_png)
        # Real PCM silence (NOT a moviepy AudioClip): the all-zeros clip moviepy
        # writes probes as 0.0s duration, so the subsequent AudioFileClip reader
        # collapses its buffer to 1 frame and Popen(bufsize=1) trips the CPython
        # "line buffering in binary mode" RuntimeWarning. A proper WAV carries
        # samples, keeps duration == TITLE_HOLD, and the warning never fires.
        import wave
        title_wav = work / "title.wav"
        sr = 44100
        with wave.open(str(title_wav), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(sr)
            f.writeframes(b"\x00\x00" * int(sr * TITLE_HOLD))
        slides = [[title_png], *slides]
        audios = [title_wav, *audios]
    print("  [4/5] Building PPTX deck ...")
    tp = time.monotonic()
    build_pptx(plan, Path(f"{out_base}.pptx"))
    print(f"  [4/5] done in {time.monotonic() - tp:.0f}s")
    if not skip_video:
        print("  [5/5] Rendering video (this can take a while) ...")
        # The cover card is not a narrated scene, so it gets no word timings.
        assemble_video(slides, audios, Path(f"{out_base}.mp4"),
                       pause=pause, end_hold=end_hold,
                       timings=[None, *timings],
                       bullets_by_scene=[None, *bullets_by_scene])

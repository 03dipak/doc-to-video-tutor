"""TTS synthesis and moviepy assembly of frames+audio into the final .mp4."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import LOUDNESS_LRA, LOUDNESS_TARGET, LOUDNESS_TP, TITLE_HOLD, TTS_LOUDNORM, TTS_VOICE
from .pptx import build_pptx
from .slides import _render_title_card, render_scenes
from .util import _Progress


async def _scene_audio(text: str, out_path: Path, voice: str,
                       rate: str = "-8%", pitch: str = "+0Hz",
                       volume: str = "+0%") -> None:
    import edge_tts

    await edge_tts.Communicate(text, voice, rate=rate,
                               pitch=pitch, volume=volume).save(str(out_path))


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


def synth_scenes(script: dict, work_dir: Path, voice: str | None) -> list[Path]:
    clips = script.get("clips", [])
    p = _Progress("TTS", len(clips))
    texts = [str(c["spoken"]) for c in clips]
    paths = [work_dir / (f"scene_{c.get('index', i):02d}.mp3")
             for i, c in enumerate(clips)]
    v, rate, pitch, volume = _clip_voice(script, voice)

    async def _all():
        await asyncio.gather(*(
            _scene_audio(text, path, v, rate, pitch, volume)
            for text, path in zip(texts, paths, strict=True)
        ))

    asyncio.run(_all())
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
    return paths
def assemble_video(slide_groups: list[list[Path]], audios: list[Path],
                   out_path: Path, pause: float = 3.0, end_hold: float = 6.0) -> None:
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
            intro = min(scene_dur * 0.15, 3.0)
            per_bullet = (scene_dur - intro) / (len(variants) - 1)
            parts = [intro] + [per_bullet] * (len(variants) - 1)
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
    audios = synth_scenes(script, work, voice)
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
        assemble_video(slides, audios, Path(f"{out_base}.mp4"),
                       pause=pause, end_hold=end_hold)

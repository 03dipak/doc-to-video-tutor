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
from collections.abc import Sequence
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


def _spoken_tokens(text: str, rules: tuple | None) -> list[str]:
    """Tokens of ``text`` in the same form the provider actually spoke.

    The word stream we match against comes from `WordBoundary` events, which
    segment the *post-expansion* spoken text. A bullet is stored raw, so digits,
    snake_case identifiers and the M1-M12 rules all differ between the two sides.
    Measured on a real render: 0 of 383 boundary words were digits while 11 were
    number-words, so a bullet containing `3` could never match a stream holding
    `three` - at any weighting. Canonicalising the bullet through the same
    `speech_expand` the TTS text went through removes that asymmetry.
    """
    if not rules:
        return _nar_tokens(str(text))
    from .speech import speech_expand

    return _nar_tokens(speech_expand(str(text), rules))


_MIN_MATCH_WINDOW = 4


def _stream_weights(flat: list[str]) -> dict[str, float]:
    """Inverse-frequency weight for every token in one scene's word stream.

    Scoped to the scene rather than a corpus, so it needs no training data and
    stays a pure function of what the provider actually said. It is what stops a
    word the narrator repeats in surrounding prose from dominating the match: in
    the real lesson `structure` occurs 4 times in one clip, so it counts for far
    less than `engine` or `comparing`, which occur once.
    """
    import math

    counts: dict[str, int] = {}
    for tok in flat:
        counts[tok] = counts.get(tok, 0) + 1
    total = max(len(flat), 1)
    return {tok: math.log(1.0 + total / count) for tok, count in counts.items()}


def _best_window(flat: list[str], cursor: int, tokens: list[str],
                 weights: dict[str, float]) -> tuple[int, float]:
    """Index of the best-matching position at or after ``cursor``, and its score.

    Every occurrence of any bullet token is a candidate; each is scored by the
    weighted fraction of the bullet's *whole* token set that appears in a short
    window starting there. Scoring the neighbourhood rather than a single anchor
    is what fixes both measured failures: a token repeated in prose no longer
    wins just by being first, and two bullets that share an anchor are separated
    by the rest of their text.
    """
    wanted = {tok for tok in tokens}
    if not wanted:
        return -1, 0.0
    total = sum(weights.get(tok, 1.0) for tok in wanted)
    # The window is the bullet's own spoken span. Widening it does not buy
    # recall - a bullet is still located whenever *any* of its tokens is found -
    # it only blurs position, because a wider window lets an early mention in
    # surrounding prose scoop up the tokens of the real bullet further along.
    window = max(_MIN_MATCH_WINDOW, len(wanted))
    best_at, best_score = -1, 0.0
    for index in range(cursor, len(flat)):
        if flat[index] not in wanted:
            continue
        seen = {tok for tok in flat[index:index + window] if tok in wanted}
        score = sum(weights.get(tok, 1.0) for tok in seen) / total
        if score > best_score:
            best_at, best_score = index, score
    return best_at, best_score


def _bullet_start_times(bullets: list[str], timings: list[dict],
                        rules: tuple | None = None) -> list[float]:
    """First-spoken time for each bullet, or -1 when it cannot be located.

    Matching is token-level against the same canonical tokenizer the repeat
    machinery uses. The narration contract deliberately stops the model reading
    bullets verbatim (§7.5), so a verbatim phrase match would almost always miss
    and silently degrade every scene to an even split; a single anchor token
    survives rephrasing but is too weak to place a reveal, because the narrator
    usually mentions the same words in the surrounding prose first. So a
    candidate position is scored by how much of the bullet appears *around* it,
    weighted by how rare each token is in this scene (see `_stream_weights`).
    The search stays monotonic — bullets are spoken in listed order, so the cursor
    only moves forward — and a bullet that cannot be located returns -1, which
    makes the caller fall back to an even split for that scene alone.

    ``rules`` are the profile's pronunciation rules; see `_spoken_tokens` for why
    the bullet side has to be expanded before it can be compared.
    """
    if not bullets or not timings:
        return []
    words = [(float(t.get("start", 0.0)), _nar_tokens(str(t.get("text", ""))))
             for t in timings]
    flat = [tok for _, toks in words for tok in toks]
    starts: list[float] = []
    for start, toks in words:
        starts.extend([start] * len(toks))
    weights = _stream_weights(flat)
    out: list[float] = []
    cursor = 0
    for bullet in bullets:
        tokens = {t for t in _spoken_tokens(bullet, rules) if len(t) >= 4}
        if not tokens:
            out.append(-1.0)
            continue
        at, _score = _best_window(flat, cursor, sorted(tokens), weights)
        if at < 0:
            out.append(-1.0)
            continue
        out.append(starts[at])
        cursor = at + 1
    return out


def _variant_durations(scene_dur: float, variant_count: int,
                       bullets: list[str] | None = None,
                       timings: list[dict] | None = None,
                       rules: tuple | None = None) -> list[float]:
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
                                 timings or [], rules)
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
    _report_measured_duration(paths, script)
    return paths, all_timings


def _clip_seconds(path: Path) -> float:
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


# Measured-duration bands, as a fraction of the declared target. Symmetric:
# undershoot and overshoot are different defects with different remedies.
_DURATION_BAND_LOW = 0.80      # below -> FAIL
_DURATION_BAND_OK = 0.92       # below -> WARN short
_DURATION_BAND_OK_HIGH = 1.15  # above -> WARN long
_DURATION_BAND_HIGH = 1.50     # above -> FAIL


def _duration_verdict(ratio: float) -> tuple[str, str]:
    """Band a measured/target duration ratio.

    Symmetric on purpose. The first version of this check only ever looked at the
    short side, so a fresh `build` that rendered 4.38 min against a 4.0 min
    target (109%) reported PASS - and so would 300%, since anything at or above
    0.92 cleared it. A lesson twice its declared length is as much a product
    defect as one at half, and it needs a different remedy: overshoot is trimmed
    selectively, undershoot is filled with source-grounded teaching.

    Bands are advisory rather than blocking, matching the rest of the media
    layer: the remedy for either side is a content decision, not a mechanical
    fix, and auto-trimming narration would risk the grounding the gates exist to
    protect.
    """
    if ratio < _DURATION_BAND_LOW:
        return "FAIL", ("lesson_duration_critical_short: the render reaches only "
                        f"{ratio:.0%} of the declared target, so the lesson does "
                        "not meet its stated format")
    if ratio < _DURATION_BAND_OK:
        return "WARN", ("lesson_duration_short: the render is materially under "
                        f"target at {ratio:.0%}. Add source-grounded teaching - "
                        "never padding")
    if ratio > _DURATION_BAND_HIGH:
        return "FAIL", ("lesson_duration_critical_long: the render reaches "
                        f"{ratio:.0%} of the declared target, which is a pacing "
                        "defect rather than extra teaching")
    if ratio > _DURATION_BAND_OK_HIGH:
        return "WARN", ("lesson_duration_long: the render runs long at "
                        f"{ratio:.0%} of target; trim selectively rather than "
                        "across the board")
    return "PASS", ""


def _report_measured_duration(paths: list[Path], script: dict) -> None:
    """Report the rendered duration and band it against the target.

    The pre-audio gate can only estimate, because it runs before the provider is
    called. Once the clips exist the estimate is no longer needed and is in fact
    misleading: on mod03_gates_v023 the estimate said 2.84 min against a 4.0 min
    target (71%, warning) while ffprobe measured 3.72 min (93%, no warning). Any
    duration policy built on the estimate would have chased a defect that was not
    there. So the authoritative number is printed from the audio, and the finding
    is derived from that rather than from the estimate.
    """
    target = float(script.get("target_minutes") or 0.0)
    total = sum(_clip_seconds(p) for p in paths)
    if total <= 0:
        return
    measured_min = total / 60.0
    if target <= 0:
        print(f"  duration  : {measured_min:.2f} min measured "
              f"({total:.1f}s across {len(paths)} clips)")
        return
    ratio = measured_min / target
    verdict, finding = _duration_verdict(ratio)
    print(f"  duration  : {measured_min:.2f} min measured vs {target:.2f} min "
          f"target = {ratio:.0%} [{verdict}]")
    if finding:
        print(f"  [{verdict}] {finding}")


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
_VTT_CUE_WORDS = 8          # words per caption cue before a forced break
_VTT_BREAKS = ".!?।"   # end a cue at sentence punctuation


def _write_webvtt(out_base: Path, script: dict,
                  timings: Sequence[Sequence[dict] | None],
                  audios: Sequence[Path] | None = None,
                  pause: float = 3.0) -> int:
    """Write a WebVTT caption track from the provider's own word stream.

    `edge_tts.SubMaker` builds cues from the very same `WordBoundary` events that
    drive reveal sync, so this costs nothing beyond a file write and needs no
    alignment model. WebVTT is the interoperable form - any player or editor
    reads it - whereas the JSON sidecar is a private format only this pipeline
    consumes. Both are emitted because they answer different questions: the VTT
    is for a human watching the lesson, the JSON for the renderer.

    Clip-local offsets are rebased onto the assembled timeline, including the
    inter-scene pause, so cue times line up with the rendered MP4 rather than
    with the individual clip files. Failure is non-fatal by design: a missing
    caption track must never cost a rendered lesson.
    """
    try:
        import edge_tts
    except ImportError:
        return 0
    maker = edge_tts.SubMaker()
    fed = 0
    base_s = 0.0
    try:
        for position, words in enumerate(timings):
            if not words:
                base_s += pause
                continue
            group: list[str] = []
            group_start = 0.0
            group_end = 0.0
            for word in words:
                start = base_s + float(word.get("start", 0.0))
                end = start + float(word.get("duration", 0.0))
                text = str(word.get("text", "")).strip()
                if not text:
                    continue
                if not group:
                    group_start = start
                group.append(text)
                group_end = end
                # Break on sentence punctuation or on length, so a cue reads as
                # a phrase. SubMaker emits exactly one cue per feed, so feeding
                # raw word boundaries would caption the lesson one word at a
                # time - technically synced, practically unreadable.
                if len(group) >= _VTT_CUE_WORDS or text[-1] in _VTT_BREAKS:
                    maker.feed({"type": "WordBoundary",
                                "offset": int(group_start * 1e7),
                                "duration": int(max(group_end - group_start, 0.0)
                                                * 1e7),
                                "text": " ".join(group)})
                    fed += 1
                    group = []
            if group:
                maker.feed({"type": "WordBoundary",
                            "offset": int(group_start * 1e7),
                            "duration": int(max(group_end - group_start, 0.0)
                                            * 1e7),
                            "text": " ".join(group)})
                fed += 1
            # Advance by the clip's MEASURED duration, not by its last word
            # offset: every clip carries trailing silence after the final word,
            # so advancing on word offsets drifts the captions progressively
            # earlier and would leave the last scene's cues on top of the first.
            spoken = (float(words[-1].get("start", 0.0))
                      + float(words[-1].get("duration", 0.0)))
            measured = 0.0
            if audios is not None and position < len(audios):
                measured = _clip_seconds(audios[position])
            base_s += (measured or spoken) + pause
        srt = maker.get_srt()
    except Exception as exc:  # provider shape drift must not lose the render
        print(f"  WARN: caption export skipped ({type(exc).__name__}: {exc})")
        return 0
    if not fed or not srt.strip():
        return 0
    # SRT and WebVTT share cue timing; only the header and the millisecond
    # separator differ, so convert rather than re-deriving timings a second way.
    Path(f"{out_base}.vtt").write_text(
        "WEBVTT\n\n" + srt.replace(",", ".").strip() + "\n", encoding="utf-8")
    return fed


def assemble_video(slide_groups: list[list[Path]], audios: list[Path],
                   out_path: Path, pause: float = 3.0, end_hold: float = 6.0,
                   timings: list[list[dict] | None] | None = None,
                   bullets_by_scene: list[list[str] | None] | None = None,
                   rules: tuple | None = None) -> None:
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
                                      scene_bullets, scene_timings, rules)
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
def _profile_rules(script: dict) -> tuple | None:
    """Pronunciation rules of the profile the script was built with.

    Reveal matching has to expand bullets the same way the TTS text was expanded,
    so the rules are resolved from the script's own recorded profile. Recording
    the profile name in the artifact (rather than re-deriving it from ambient
    config) is what makes the two sides provably the same expansion.
    """
    from .voice import _make_voice

    name = script.get("voice_profile")
    if not name:
        return None
    try:
        return tuple(_make_voice(str(name)).pronunciation_rules)
    except SystemExit:
        print(f"  [sync] voice profile {name!r} is not registered; reveal "
              f"timings fall back to an even split")
        return None


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
    vtt_cues = _write_webvtt(out_base, script, timings, audios)
    if vtt_cues:
        print(f"  captions   : {vtt_cues} cues -> {out_base}.vtt")
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
                       bullets_by_scene=[None, *bullets_by_scene],
                       rules=_profile_rules(script))

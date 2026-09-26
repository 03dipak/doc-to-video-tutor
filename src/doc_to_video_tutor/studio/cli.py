"""Command-line entry point (doc-to-studio / python -m)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

from .config import (
    _REPEAT_POLICY_VERSION,
    _SCHEMA_VERSION,
    _SOFT_PREFIXES,
    LOUDNESS_WPM,
    TITLE_HOLD,
)
from .llm import _extract_topics
from .narration import (
    _dedupe_narration_templates,
    _deepen_narrations,
    _enforce_unique_narration_trigrams,
    _narration_repeat_report,
    _protected_terms,
    _repair_thin_narrations,
    _repair_unsafe_narrations,
    _trim_narration_word_count,
)
from .plan import (
    _concept_headers,
    _drop_ungrounded_slide_text,
    _paginate_plan_slides,
    _prune_bullet_takeaway_echo,
    _sanitize_design_decisions,
    _sanitize_plan_source_leaks,
    _unclaimed_source_sections,
    plan_lesson,
)
from .slides import LAYOUT_NOTES
from .speech import build_tts_script, render_script_txt
from .text import (
    _content_ngrams,
    _content_tokens,
    _ngram_set,
    _strip_source_metadata_blocks,
    _top_source_terms,
)
from .util import _wait_before_retry, atomic_json_write, file_digest, load_documents
from .validate import _render_blocking_problems, check_audit_binding, guard_plan, review_plan
from .video import _render_media, measure_delivery
from .voice import _make_voice, _voice_fingerprint

# Sample-boundary gates that plan_lesson's deterministic chain already repaired
# in-place are advisory here: a leftover is written into the persisted plan for
# `verify`'s hard audit, not burned on a fresh whole-plan resample. See the
# land-and-repair contract in plan_lesson.
_SAMPLE_SOFT_EXTRA = ("ungrounded slide text",)


def narration_under_run_warning(words: int, target_minutes: float) -> str | None:
    """Warn when the spoken track is materially shorter than the target.

    A lesson that ships at 69% of its requested length reads as truncated, so the
    warning is a quality signal rather than a structural failure. Returns None
    when no target is set or the target is met.
    """
    if target_minutes <= 0:
        return None
    spoken_min = words / LOUDNESS_WPM
    if spoken_min >= 0.7 * target_minutes:
        return None
    return (f"narration under-run: only {spoken_min / target_minutes:.0%} of "
            f"target reached - MAJOR (A2.6/E1.2); lengthen plan or deepen "
            f"narration")


def _tts_health_check(args) -> int:
    """Probe the TTS provider and assert it still behaves as the pipeline assumes.

    `edge-tts` wraps a consumer reading service rather than a published API, so
    it can change shape without a deprecation notice. This probe fails loudly
    and early instead of letting a silent breakage surface as a production
    outage: it checks that a fixed phrase yields audio of a plausible duration
    and that per-word boundary events are still returned, which the reveal
    alignment in §19.5 depends on.
    """
    import tempfile
    import time as _time
    from pathlib import Path as _Path

    from .video import _scene_audio
    from .voice import _make_voice as _mk

    profile = _mk(getattr(args, "narr_voice", None))
    voice, rate, pitch, volume = _clip_probe_voice(profile, args.voice)
    phrase = "Regression gate baseline compare tolerance check."
    out_dir = _Path(args.out_dir) if args.out_dir else _Path(tempfile.mkdtemp())
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "tts_check.mp3"
    started = _time.monotonic()
    try:
        words = _asyncio_run(_scene_audio(phrase, target, voice, rate, pitch,
                                          volume))
    except Exception as exc:  # provider transport error
        print(f"  TTS probe : FAIL — provider call raised "
              f"{type(exc).__name__}: {exc}")
        return 1
    elapsed = _time.monotonic() - started
    size = target.stat().st_size if target.exists() else 0
    print(f"  provider  : edge-tts voice={voice} rate={rate}")
    print(f"  audio     : {size} bytes in {elapsed:.1f}s -> {target}")
    print(f"  word sync : {len(words)} boundary events")
    problems = []
    if size < 1024:
        problems.append("audio is effectively empty")
    if not words:
        problems.append("no WordBoundary events — reveal sync would degrade "
                        "to an even split")
    if words and words[-1]["start"] > 60:
        problems.append("last word offset beyond 60s — timing looks wrong")
    for problem in problems:
        print(f"  [FAIL] {problem}")
    if not problems:
        print("  TTS probe : PASS")
    return 1 if problems else 0


def _asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def _clip_probe_voice(profile, override):
    voice = override or profile.tts_voice
    if not voice:
        raise SystemExit("tts-check: the voice profile has no concrete tts_voice")
    return voice, profile.rate, profile.pitch, profile.volume


def _write_verify_artifact(out_base: Path, plan_doc: dict, plan: dict,
                          script: dict, args, *, verdict: str,
                          soft: list[str] | None = None,
                          blockers: list[dict] | None = None,
                          blocking: list[str] | None = None) -> Path:
    """Write the per-run evidence file. The artifact the build was throwing away.

    The build computes the TTS audit, the layout audit, loudness and duration,
    then prints them as stdout no later process can read. The findings survived
    only inside `tts_script.json`; the measurements did not survive at all. So
    every reviewer re-derived them - one spent 100 toolcalls reproducing gates
    that had already run. This file is the durable form: measured values, gate
    verdicts, and the plan digest, so cross-artifact review becomes checking
    rather than re-deriving.

    It is also what makes the L4 gap impossible to hit again: `soft` findings
    used to be computed and then filtered out a few lines above where they were
    produced, so `source section not covered` and `narration still speaks
    about` never appeared anywhere a human would see them.
    """
    scenes = plan.get("scenes") or []
    clips = script.get("clips") or []
    words = sum(int(c.get("word_count") or 0) for c in clips)
    audio_dir = Path(f"{out_base}_audio")
    doc: dict = {
        "schema_version": 1,
        "verdict": verdict,
        "plan_path": str(Path(f"{out_base}.plan.json")),
        # The digest `check_audit_binding` compares, so a green build finally
        # has something to bind against.
        "plan_sha256": file_digest(Path(f"{out_base}.plan.json"))
        if Path(f"{out_base}.plan.json").exists() else None,
        "target_minutes": float(getattr(args, "minutes", 0) or 0),
        "gates": {
            "review_before_build": {
                "soft": list(soft or []),
            },
            "pre_audio": {"findings": list(blockers or [])},
            "pre_render": {"findings": list(blocking or [])},
        },
        "counts": {
            "scenes": len(scenes),
            "clips": len(clips),
            "audio_files": len(sorted(audio_dir.glob("*.mp3"))) if audio_dir.exists() else 0,
            "narration_words": words,
            "bullets_per_scene": [len(s.get("bullets") or []) for s in scenes],
            "dropped_slide_text": len(plan.get("dropped_slide_text") or []),
            # The count that was missing, not the total: naming a field
            # "unclaimed" while filling it with every section in the document
            # is the same class of bug as printing a config constant under a
            # "loudness :" label.
            "source_sections": len(plan.get("source_sections") or []),
            "unclaimed_source_sections": len(_unclaimed_source_sections(plan)),
        },
        "layout": {
            "pptx": list(plan.get("_layout_findings") or []),
            "slides": list(LAYOUT_NOTES),
        },
        "media": measure_delivery(out_base),
    }
    # Duration as a ratio, per surface. The mp4 is what a viewer experiences and
    # it is materially longer than the audio, because of the title hold and the
    # inter-scene pauses; reporting only the audio understates it by ~17 points.
    media = doc["media"]
    secs = media.get("seconds")
    target = doc["target_minutes"] * 60.0
    if secs and target > 0:
        pct = round(secs / target * 100.0, 1)
        # Decomposed, because "137% of target" reads as a content problem when
        # most of it is not content. Measured on v012_015: the MP4 is 326s
        # against a 240s target, and 37s of that is structural silence -
        # TITLE_HOLD 4.0 plus a 3.0s pause after each of 9 scenes plus a 6.0s
        # end hold. The teaching audio is 289s (120%). One number for both made
        # a configurable inter-scene pause look like a lesson that taught too
        # much, and sent the fix to the wrong layer.
        n_scene = len(scenes)
        gap = float(getattr(args, "pause", 3.0) or 0.0)
        silence = TITLE_HOLD + gap * n_scene + float(
            getattr(args, "end_hold", 6.0) or 0.0)
        audio_s = max(0.0, secs - silence)
        doc["duration"] = {
            "mp4_seconds": secs,
            "mp4_pct_of_target": pct,
            "band": "LONG" if pct > 115 else ("SHORT" if pct < 70 else "OK"),
            "narration_seconds": round(audio_s, 1),
            "narration_pct_of_target": round(audio_s / target * 100.0, 1),
            "structural_silence_seconds": round(silence, 1),
            "structural_silence_pct_of_target": round(
                silence / target * 100.0, 1),
            "inter_scene_gap_seconds": gap,
            "scenes": n_scene,
        }
        # Per-scene share, so an outlier is visible as a number. Scene 2 on
        # v012_015 was 14.8% of the audio against a 9.4% mean, and it is the
        # only scene over the 70-word WARN band.
        shares = []
        for c in clips:
            w = int(c.get("word_count") or 0)
            if w and audio_s > 0:
                shares.append({"index": c.get("index"), "role": c.get("role"),
                               "words": w,
                               "pct_of_audio": round(w / max(1, words) * 100.0, 1)})
        if shares:
            mean = sum(s["pct_of_audio"] for s in shares) / len(shares)
            doc["duration"]["scene_share_pct"] = shares
            doc["duration"]["scene_share_mean_pct"] = round(mean, 1)
            doc["duration"]["scene_share_outliers"] = [
                s for s in shares if s["pct_of_audio"] > mean * 1.4]
    out = Path(f"{out_base}.verify.json")
    atomic_json_write(out, doc)
    return out


def _write_tts_artifacts(out_base: Path, script: dict, args) -> None:
    """Apply CLI overrides, persist tts_script.json + script.txt, print the audit."""
    if args.rate:
        script["rate"] = args.rate
    if args.pitch:
        script["pitch"] = args.pitch
    if args.volume:
        script["volume"] = args.volume
    atomic_json_write(Path(f"{out_base}.tts_script.json"), script)
    Path(f"{out_base}.script.txt").write_text(
        render_script_txt(script), encoding="utf-8")
    audit = script.get("audit") or []
    fails = [f for f in audit if f["severity"] == "FAIL"]
    warns = [f for f in audit if f["severity"] == "WARN"]
    print("  TTS QA      : " + ("PASS" if not fails else f"ISSUES ({len(fails)})"))
    print(f"  voice       : {script.get('tts_voice')} | "
          f"{script.get('rate')} / {script.get('pitch')} / {script.get('volume')}")
    print(f"  clips       : {len(script.get('clips', []))} "
          f"(scene + final takeaways)")
    print(f"  slide-meta leaks : {len(fails)} | warnings: {len(warns)}")
    words = sum(int(c.get("word_count", 0)) for c in script.get("clips", []))
    target_min = float(script.get("target_minutes") or 0.0)
    spoken_min = words / LOUDNESS_WPM
    if target_min:
        # Labelled as an estimate on purpose. No audio exists yet at this point,
        # so this is a projection from a calibrated words-per-minute constant,
        # and printing it unqualified next to the measured figure reported after
        # TTS invites reading a ~6% projection as the real duration. The
        # authoritative number is the ffprobe measurement in _render_media.
        print(f"  narration   : {words} words (~{spoken_min:.1f} min estimated "
              f"pre-audio) vs target {target_min:.1f} min")
        under_run = narration_under_run_warning(words, target_min)
        if under_run:
            print(f"    [WARN] {under_run}")
    for f in audit:
        print(f"    [{f['severity']}] {f['code']}: {f['message']}")
    print(f"  artifacts   : {out_base}.tts_script.json, "
          f"{out_base}.script.txt")


def _tts_blocking_findings(script: dict) -> list[dict]:
    """TTS script is NOT speakable: any FAIL means we refuse to call edge-tts
    (no MP3, no MP4) and drop a rejected.script artifact instead (rule
    TTS_BLOCKER / tts_voice_unset / tts_text_corrupted / etc.)."""
    return [f for f in (script.get("audit") or []) if f.get("severity") == "FAIL"]


def _write_rejected_tts_script(out_base: Path, script: dict,
                                plan_path: str, findings: list[dict]) -> None:
    payload = {
        "verdict": "BLOCKED",
        "reason": "TTS script integrity gate (_tts_blocking_findings): "
                  "audio would be unscannable/corrupt - no TTS was invoked",
        "findings": findings,
        "plan_path": plan_path,
        "script": script,
    }
    atomic_json_write(out_base.with_suffix(".rejected.tts_script.json"), payload)
def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if argv and argv[0] not in ("build", "review", "render", "verify",
                               "tts-check") \
            and argv[0] not in ("-h", "--help"):
        argv = ["build", *argv]

    parser = argparse.ArgumentParser(
        description="Studio: docs -> scene plan -> synced video + PPTX lesson."
    )
    sub = parser.add_subparsers(dest="cmd")

    build_p = sub.add_parser("build", help="generate lesson video + deck")
    build_p.add_argument("inputs", nargs="+", help="path(s) to .md, .txt, or .pptx")
    build_p.add_argument("--minutes", type=float, default=4.0, help="target spoken length")
    build_p.add_argument("--voice", default=None, help="TTS voice (Marathi-Hindi mix)")
    build_p.add_argument("--rate", default=None, help="TTS speaking rate, e.g. -8%%")
    build_p.add_argument("--pitch", default=None, help="TTS pitch, e.g. +0Hz")
    build_p.add_argument("--volume", default=None, help="TTS volume, e.g. +0%%")
    build_p.add_argument("--narr-voice", default=None,
                          help="narration voice (mhe-mix | english); default mhe-mix")
    build_p.add_argument("--skip-video", action="store_true", help="PPTX + audio only")
    build_p.add_argument("--out", default=None, help="output prefix (default: first input's stem)")
    build_p.add_argument(
        "--out-dir", default="output", help="folder for generated files (created if missing)"
    )
    build_p.add_argument("--pause", type=float, default=3.0,
                         help="silent pause seconds between slides (default 3.0)")

    review_p = sub.add_parser("review", help="audit a saved .plan.json (narration/topic/quality)")
    review_p.add_argument("plan_json", nargs="+", help="path(s) to *.plan.json")
    review_p.add_argument("--topics", nargs="+", default=None,
                          help="override topic reference list")

    tts_p = sub.add_parser(
        "tts-check",
        help="TTS provider health probe: synthesise a fixed phrase, assert a "
             "plausible duration and that word boundaries are returned")
    tts_p.add_argument("--voice", default=None,
                       help="engine voice override (default: profile/env)")
    tts_p.add_argument("--out-dir", default=None,
                       help="keep the probe clip(s) in this directory")

    verify_p = sub.add_parser(
        "verify",
        help="deterministic pre-build QA on a saved .plan.json (no LLM/no "
             "render): apply narration+design_decision fixers, show before/"
             "after repeat counts, write a *_fixed.plan.json")
    verify_p.add_argument("plan_json", help="path to *.plan.json")
    verify_p.add_argument("--out", default=None,
                          help="output path for the fixed plan (default: "
                               "<input stem>_fixed.plan.json)")
    verify_p.add_argument("--narr-voice", default=None,
                          help="narration voice to assume (default: saved voice / mhe-mix)")

    render_p = sub.add_parser(
        "render",
        help="render media from a saved .plan.json (deterministic, no LLM)")
    render_p.add_argument("plan_json", help="path to *.plan.json")
    render_p.add_argument("--out", required=True, help="output prefix")
    render_p.add_argument("--voice", default=None, help="TTS voice (Marathi-Hindi mix)")
    render_p.add_argument("--rate", default=None, help="TTS speaking rate, e.g. -8%%")
    render_p.add_argument("--pitch", default=None, help="TTS pitch, e.g. +0Hz")
    render_p.add_argument("--volume", default=None, help="TTS volume, e.g. +0%%")
    render_p.add_argument("--narr-voice", default=None,
                          help="narration voice to assume (default: saved voice / mhe-mix)")
    render_p.add_argument("--skip-video", action="store_true", help="PPTX + audio only")
    render_p.add_argument(
        "--out-dir", default="output", help="folder for generated files (created if missing)"
    )
    render_p.add_argument("--pause", type=float, default=3.0,
                          help="silent pause seconds between slides (default 3.0)")
    render_p.add_argument("--end-hold", type=float, default=6.0,
                          help="extra hold seconds on the last slide (default 6.0)")

    args = parser.parse_args(argv)
    if args.cmd == "tts-check":
        raise SystemExit(_tts_health_check(args))
    if args.cmd == "review":
        topics = args.topics or None
        code = 0
        for p in args.plan_json:
            code = max(code, review_plan(Path(p), topics))
        raise SystemExit(code)
    if args.cmd == "verify":
        src = Path(args.plan_json)
        out_p = Path(args.out) if args.out else src.with_name(
            f"{src.stem}_fixed.plan.json")
        data = json.loads(src.read_text(encoding="utf-8"))
        plan = data.get("plan", data)
        ref = data.get("topics", [])
        voice = _make_voice(args.narr_voice
                            or data.get("voice")
                            or (data.get("narration_voice") or {}).get("name"))
        data["voice"] = voice.name
        data.setdefault("schema_version", _SCHEMA_VERSION)
        data.setdefault("repeat_policy_version", _REPEAT_POLICY_VERSION)
        plan.setdefault("protected_trigrams",
                        [str(p) for p in (data.get("protected_trigrams") or [])])
        source_leaks = _sanitize_plan_source_leaks(
            plan, voice=voice,
            default_refs=[str(value) for value in (data.get("source_files") or [])])
        protected = _protected_terms(plan)
        before_banned, _before_prot = _narration_repeat_report(plan, protected)
        dd_removed = _sanitize_design_decisions(plan)
        _prune_bullet_takeaway_echo(plan)
        _dedupe_narration_templates(plan, quiet=True, voice=voice,
                                    protected=protected)
        _deepen_narrations(plan, quiet=True, voice=voice)
        _enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
        _repair_unsafe_narrations(plan, voice=voice, protected=protected)
        _repair_thin_narrations(plan, voice=voice, protected=protected)
        _trim_narration_word_count(plan, voice=voice, quiet=True)
        if data.get("source_bigrams") and data.get("source_tokens"):
            _drop_ungrounded_slide_text(
                plan, _ngram_set(data["source_bigrams"]),
                set(data["source_tokens"]))
        _paginate_plan_slides(plan)
        verify_problems = guard_plan(
            plan, ref, source_bigrams=data.get("source_bigrams"),
            source_tokens=data.get("source_tokens"),
            source_top=data.get("source_top"), voice=voice)
        after_banned, after_prot = _narration_repeat_report(plan, protected)
        # Idempotency self-check: a second pass must not change the plan.
        again = deepcopy(plan)
        _sanitize_plan_source_leaks(
            again, voice=voice,
            default_refs=[str(value) for value in (data.get("source_files") or [])])
        _prune_bullet_takeaway_echo(again)
        _sanitize_design_decisions(again)
        _dedupe_narration_templates(again, quiet=True, voice=voice,
                                    protected=protected)
        _deepen_narrations(again, quiet=True, voice=voice)
        _enforce_unique_narration_trigrams(again, quiet=True, protected=protected)
        _repair_unsafe_narrations(again, voice=voice, protected=protected)
        _repair_thin_narrations(again, voice=voice, protected=protected)
        _trim_narration_word_count(again, voice=voice, quiet=True)
        if data.get("source_bigrams") and data.get("source_tokens"):
            _drop_ungrounded_slide_text(
                again, _ngram_set(data["source_bigrams"]),
                set(data["source_tokens"]))
        _paginate_plan_slides(again)
        _still_changes = _narration_repeat_report(again, protected) != (
            after_banned, after_prot) or (
                [str(s.get("narration", "")) for s in again.get("scenes", [])]
                != [str(s.get("narration", "")) for s in plan.get("scenes", [])])
        audit = {
            "artifact_type": "plan_audit",
            "audit_schema_version": 1,
            # Identity is the digest of the file this audit actually describes -
            # the repaired plan that ships beside it. `plan` is kept as display
            # metadata only: it is the input path, and a path can be renamed or
            # point at a different file, so it can never stand in for identity.
            "plan_sha256": file_digest(out_p),
            "plan_filename": out_p.name,
            "source_plan": str(src),
            "plan": str(src),
            "voice": voice.name,
            "protected_trigrams": sorted(protected),
            "before": {"banned_repeats": len(before_banned),
                       "banned_examples": before_banned[:4]},
            "after": {"banned_repeats": len(after_banned),
                      "protected_repeats": len(after_prot),
                      "protected_examples": after_prot[:4],
                      "source_leaks_removed": source_leaks,
                      "design_decision_degenerates_dropped": dd_removed},
            "idempotent": not _still_changes,
        }
        audit_p = out_p.with_name(f"{out_p.stem}.audit.json")
        # An audit is only evidence if it is bound to the plan beside it, so a
        # stale or swapped plan cannot inherit a clean bill of health.
        binding = check_audit_binding(out_p, audit_p) if audit_p.exists() else []
        print(f"Verify {src}")
        print(f"  title     : {plan.get('title')!r}")
        print(f"  voice     : {voice.name}")
        print(f"  scenes    : {len(plan.get('scenes', []))}")
        print(f"  banned repeated phrases (before) : {len(before_banned)}")
        print(f"  banned repeated phrases (after)  : {len(after_banned)}")
        print(f"  protected terminology repeats (before) : {len(_before_prot)}")
        print(f"  protected terminology repeats (after)  : {len(after_prot)}")
        if before_banned:
            print(f"    removed : {sorted(set(before_banned) - set(after_banned))[:4]} ...")
        if source_leaks:
            print(f"  source metadata leaks removed: {source_leaks}")
        if dd_removed:
            print(f"  design_decision degenerates dropped: {dd_removed}")
        if _still_changes:
            print("  WARNING: verification is not idempotent (second pass changed "
                  "the plan again)")
        hard = [p for p in verify_problems
                if not any(p.startswith(s) for s in _SOFT_PREFIXES)]
        hard += _render_blocking_problems(plan, voice=voice)
        hard += list(binding)
        soft = [p for p in verify_problems
                if any(p.startswith(s) for s in _SOFT_PREFIXES)]
        if hard:
            print("  VERDICT   : ISSUES")
            for p in hard:
                print(f"    - {p}")
            for p in soft:
                print(f"    ~ {p}")
        elif soft:
            print("  VERDICT   : PASS (soft warnings)")
            for p in soft:
                print(f"    ~ {p}")
        else:
            print("  VERDICT   : PASS")
        tts_script = build_tts_script(plan, voice)
        audio_fails = [f for f in tts_script.get("audit", [])
                       if f["severity"] == "FAIL"]
        if audio_fails:
            print(f"  pre-audio gate : {len(audio_fails)} FAIL(s) remain "
                  f"(deterministic repair is done; these need a rebuild)")
            for f in audio_fails:
                print(f"    [FAIL] {f['code']}: {f['message'][:110]}")
        elif soft:
            print("  pre-audio gate : PASS (no FAIL)")
        else:
            print("  pre-audio gate : PASS")
        if hard or audio_fails:
            sys.exit(1)
        atomic_json_write(out_p, data)
        # Digest the plan *after* it lands, so the audit is bound to the exact
        # bytes on disk rather than to an in-memory object that write() may
        # still reshape.
        audit["plan_sha256"] = file_digest(out_p)
        atomic_json_write(audit_p, audit)
        if not hard:
            print(f"  fixed plan written to {out_p}")
            print(f"  audit written to {audit_p}")
        else:
            print(f"  fixedish plan written to {out_p} (gates still failing)")
            print(f"  audit written to {audit_p}")
        raise SystemExit(1 if hard else 0)
    if args.cmd == "render":
        data = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        plan = data.get("plan", data)
        narrvoice = _make_voice(args.narr_voice
                                or data.get("voice")
                                or (data.get("narration_voice") or {}).get("name"))
        data["voice"] = narrvoice.name
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_base = out_dir / Path(args.out)
        _sanitize_plan_source_leaks(
            plan, voice=narrvoice,
            default_refs=[str(value) for value in (data.get("source_files") or [])])
        _trim_narration_word_count(plan, voice=narrvoice, quiet=True)
        blocking = _render_blocking_problems(plan, voice=narrvoice)
        if blocking:
            print(f"Render {Path(args.plan_json).name} -> {out_base.name}",
                  flush=True)
            print("  VERDICT   : BLOCKED (pre-render hard gates)")
            for p in blocking:
                print(f"    - {p}")
            atomic_json_write(out_base.with_suffix(".rejected.audit.json"), {
                "verdict": "BLOCKED",
                "reason": "pre-render hard gate (_render_blocking_problems)",
                "findings": blocking,
                "plan_path": str(args.plan_json),
            })
            print("  audit written to "
                  f"{out_base.with_suffix('.rejected.audit.json')}")
            print("  fix: run `verify` on the plan (deterministic repair), or "
                  "rebuild the lesson, then retry render.")
            raise SystemExit(1)
        script = build_tts_script(plan, narrvoice)
        script["target_minutes"] = float(
            (data.get("tts") or {}).get("target_minutes") or 0.0)
        tts_blockers = _tts_blocking_findings(script)
        if tts_blockers:
            print(f"Render {Path(args.plan_json).name} -> {out_base.name}",
                  flush=True)
            print("  VERDICT   : BLOCKED (TTS script integrity gate)")
            for f in tts_blockers:
                print(f"    [{f['severity']}] {f['code']}: {f['message']}")
            _write_rejected_tts_script(out_base, script, args.plan_json,
                                       tts_blockers)
            print("  rejected tts_script written to "
                  f"{out_base.with_suffix('.rejected.tts_script.json')}")
            print("  fix: run `verify` on the plan (deterministic repair), or "
                  "rebuild the lesson, then retry render.")
            raise SystemExit(1)
        data["tts"] = {"provider": "edge-tts",
                       "voice": script.get("tts_voice"),
                       "rate": script.get("rate"),
                       "pitch": script.get("pitch"),
                       "volume": script.get("volume"),
                       "source": "env/CLI defaults",
                       "target_minutes": script["target_minutes"]}
        atomic_json_write(out_base.with_suffix(".plan.json"), data)
        t0 = time.monotonic()
        _write_tts_artifacts(out_base, script, args)
        _render_media(plan, out_base, script, args.voice, args.skip_video,
                      args.pause, args.end_hold)
        if not args.skip_video:
            print(f"  Total pipeline time: {time.monotonic() - t0:.0f}s")
        raise SystemExit(0)
    if args.cmd == "build" or args.cmd is None:
        # build requires positional inputs
        if args.cmd is None and not getattr(args, "inputs", None):
            parser.print_help()
            raise SystemExit(2)
    else:
        parser.print_help()
        raise SystemExit(2)

    content = _strip_source_metadata_blocks(load_documents(args.inputs))
    ngrams = _content_ngrams(content)
    tokens = _content_tokens(content)
    top_terms = _top_source_terms(content)
    concepts = _concept_headers(content)
    if concepts and len(concepts) < 5:
        print(f"\n  docs-as-code: {len(concepts)} source concept(s) found; "
              f"below the 5-scene floor - falling back to the classic 6-scene "
              f"drill.", flush=True)
        concepts = []
    narrvoice = _make_voice(getattr(args, "narr_voice", None))
    max_samples = 5

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_prefix = Path(args.out) if args.out else Path(args.inputs[0]).stem
    out_base = out_dir / out_prefix

    # Extract the lesson topics ONCE per build and reuse it across samples:
    # plan_lesson obtains it via a fresh LLM call, so caching across the
    # read-verify-resample loop saves one call per sample.
    topics_pre: list[str] = []
    try:
        topics_pre = _extract_topics(content)
    except RuntimeError:
        print("\n  topics extraction failed; continuing without topic "
              "constraints ...", flush=True)

    def _doc_for(plan: dict, topics: list[str], script: dict) -> dict:
        return {
            "schema_version": _SCHEMA_VERSION,
            "repeat_policy_version": _REPEAT_POLICY_VERSION,
            "narration_voice": {
                "name": narrvoice.name,
                "version": 1,
                "fingerprint": _voice_fingerprint(narrvoice),
                "language_tag": narrvoice.policy.expected_language,
            },
            "voice": narrvoice.name,
            "tts": {"provider": "edge-tts",
                    "voice": script.get("tts_voice"),
                    "rate": script.get("rate"),
                    "pitch": script.get("pitch"),
                    "volume": script.get("volume"),
                    "target_minutes": float(args.minutes),
                    "source": "env/CLI defaults"},
            "protected_trigrams": plan.get("protected_trigrams") or [],
            "topics": topics,
            "source_bigrams": sorted(" ".join(g) for g in ngrams),
            "source_tokens": sorted(tokens),
            "source_top": top_terms,
            "source_files": [str(value) for value in args.inputs],
            "plan": plan,
        }

    candidate: dict | None = None
    lesson_topics: list[str] = topics_pre
    problems: list[str] = []
    soft_prefixes = _SOFT_PREFIXES + _SAMPLE_SOFT_EXTRA
    for sample in range(1, max_samples + 1):
        try:
            sampled_plan, lesson_topics = plan_lesson(
                content, args.minutes, voice=narrvoice,
                concepts=concepts or None, topics=topics_pre,
                source_files=[str(value) for value in args.inputs])
        except RuntimeError:
            # LLM flake (degenerated loop / unparseable JSON): back off, then
            # resample - don't crash the build.
            print(f"\n  review-before-build: plan sample {sample} failed to "
                  f"parse (LLM flake); resampling the whole plan ... ",
                  flush=True)
            _wait_before_retry(sample)
            continue
        candidate = sampled_plan
        # Land EVERY parsed sample so a busy output window never wastes the
        # LLM burn: each persisted sample is directly verifiable/repairable.
        sampled_script = build_tts_script(sampled_plan, narrvoice)
        sampled_script["target_minutes"] = float(args.minutes)
        atomic_json_write(
            Path(f"{out_base}.sample{sample}.plan.json"),
            _doc_for(sampled_plan, lesson_topics, sampled_script))
        all_problems = guard_plan(
            sampled_plan, lesson_topics, source_bigrams=sorted(" ".join(g) for g in ngrams),
            source_tokens=sorted(tokens), source_top=top_terms, voice=narrvoice)
        # Split instead of filtering. `soft_problems` used to be computed and
        # then thrown away two lines down, which is the whole of the L4 gap:
        # `source section not covered` and `narration still speaks about` were
        # produced on every build and surfaced nowhere. They now reach both the
        # build log and the verify artifact.
        soft_problems = [p for p in all_problems
                         if any(p.startswith(soft) for soft in soft_prefixes)]
        problems = [p for p in all_problems
                    if not any(p.startswith(soft) for soft in soft_prefixes)]
        if not problems:
            break
        if sample < max_samples:
            print(f"\n  review-before-build: plan sample {sample} FAILED "
                  f"({'; '.join(problems)}); resampling the whole plan ... ",
                  flush=True)
            _wait_before_retry(sample)
    if candidate is None:
        print(f"\n  review-before-build: all {max_samples} samples failed to "
              f"parse (LLM flaky on this document); no plan produced. Re-run "
              f"the build to retry.", flush=True)
        raise SystemExit(1)
    plan = candidate
    series = " ".join(sorted(set(problems)))

    if series:
        print(f"\n  review-before-build: hard gates still failing after "
              f"{max_samples} samples: {series!r}", flush=True)
        raise SystemExit(1)

    plan.pop("_narration_unsafe_scenes", None)
    script = build_tts_script(plan, narrvoice)
    script["target_minutes"] = float(args.minutes)
    plan_doc = _doc_for(plan, lesson_topics, script)
    # Persist AFTER the LLM survives every hard gate so a blocked build still
    # leaves the expensive plan behind for `verify`'s deterministic repair -
    # the user never pays for a rebuild they were told could be avoided.
    atomic_json_write(Path(f"{out_base}.plan.json"), plan_doc)
    tts_blockers = _tts_blocking_findings(script)
    if tts_blockers:
        print("\n  pre-audio gate: script integrity FAIL; refusing to call "
              "edge-tts (no MP3/MP4).", flush=True)
        for f in tts_blockers:
            print(f"    [{f['severity']}] {f['code']}: {f['message']}")
        _write_rejected_tts_script(out_base, script,
                                   str(out_base.with_suffix(".plan.json")),
                                   tts_blockers)
        # A blocked build is exactly when the evidence matters most, so the
        # artifact is written on this path too - not only when everything
        # passes. R4 ruled the audit rejection-only by design; this is the same
        # reasoning applied consistently.
        _write_verify_artifact(out_base, plan_doc, plan, script, args,
                              verdict="BLOCKED", soft=soft_problems,
                              blockers=tts_blockers)
        print(f"  rejected tts_script written to "
              f"{out_base}.rejected.tts_script.json")
        print(f"  evidence    : {out_base}.verify.json")
        print("  fix: `verify "
              f"{out_base.with_suffix('.plan.json')}` (deterministic, no LLM) "
              f"or rebuild the lesson.")
        raise SystemExit(1)

    t0 = time.monotonic()
    blocking = _render_blocking_problems(plan, voice=narrvoice)
    if blocking:
        print(f"\n  pre-render hard gate: {len(blocking)} blocking finding(s); "
              f"refusing to render damaged narration.", flush=True)
        for p in blocking:
            print(f"    - {p}")
        atomic_json_write(Path(f"{out_base}.rejected.audit.json"), {
            "verdict": "BLOCKED",
            "reason": "pre-render hard gate (_render_blocking_problems)",
            "findings": blocking,
            "plan_path": str(out_base.with_suffix(".plan.json")),
        })
        _write_verify_artifact(out_base, plan_doc, plan, script, args,
                              verdict="BLOCKED", soft=soft_problems,
                              blocking=blocking)
        print(f"  evidence    : {out_base}.verify.json")
        print(f"  rejected plan + audit written; fix with `verify "
              f"{out_base.with_suffix('.plan.json')}` or rebuild the lesson.")
        raise SystemExit(1)
    _write_tts_artifacts(out_base, script, args)
    _render_media(plan, out_base, script, args.voice, args.skip_video, args.pause)
    for note in dict.fromkeys(LAYOUT_NOTES):
        soft_problems.append(note)
    verify_path = _write_verify_artifact(
        out_base, plan_doc, plan, script, args, verdict="PASS",
        soft=soft_problems)
    if soft_problems:
        uniq = list(dict.fromkeys(soft_problems))
        print(f"  soft findings: {len(uniq)} (advisory, non-blocking)")
        for note in uniq[:6]:
            print(f"    - {note}")
        if len(uniq) > 6:
            print(f"    ... +{len(uniq) - 6} more")
    print(f"  evidence    : {verify_path.name}")
    if not args.skip_video:
        print(f"  Total pipeline time: {time.monotonic() - t0:.0f}s")

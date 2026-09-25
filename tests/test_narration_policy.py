"""Narration-policy tests: gate contract, protection, safety and idempotency.

Complements tests/test_repeat_safety.py. Encodes the LLD §2/§3/§5 contract:

* Protected terminology (takeaways / saved source entities) is reported, never
  banned, and survives the enforcer verbatim.
* Unrepairable repeats (full duplicates, too-short scenes) are classified
  'unsafe' -> the hard resample gate, never silently shipped.
* The language check is advisory and voice-policy-driven.
* Post-repair integrity, design-decision sanitize, template collapse and the
  verify fixer chain are all deterministic and idempotent.
"""
import copy

import pytest

from doc_to_video_tutor import studio as S


def _plan(narrations: list[str], takeaways=None, dds: list[str] | None = None) -> dict:
    dds = dds or [""] * len(narrations)
    return {
        "opening": "",
        "takeaways": takeaways or [],
        "scenes": [{"title": f"Scene {i} topic", "narration": n,
                    "design_decision": dds[i], "bullets": []}
                   for i, n in enumerate(narrations)],
    }


def _banned(plan: dict) -> list[str]:
    return S._narration_repeat_report(plan, S._protected_terms(plan))[0]


def test_protected_terminology_is_reported_never_banned() -> None:
    plan = _plan(
        [
            "We should study the rag evaluation metrics pipeline first and then "
            "trust the honest numbers in this whole lesson.",
            "The rag evaluation metrics pipeline measures the final answer so "
            "every output score here is fair and fully reliable.",
            "A third scene explains the routing and caching layers in detail.",
            "A fourth scene describes how the retriever builds the index.",
            "A fifth scene closes the lesson with the takeaways recap.",
        ],
        takeaways=["the rag evaluation metrics pipeline"],
    )
    protected = S._protected_terms(plan)
    banned, prot = S._narration_repeat_report(plan, protected)
    assert banned == []
    assert "the rag evaluation" in prot
    assert "rag evaluation metrics" in prot
    r = S._enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    after_banned, after_prot = S._narration_repeat_report(
        plan, S._protected_terms(plan))
    assert r == 0
    assert after_banned == []
    assert "the rag evaluation" in after_prot
    assert "the rag evaluation metrics pipeline" in plan["scenes"][0]["narration"]


def test_verbatim_duplicate_scene_is_flagged_unsafe() -> None:
    plan = _plan([
        "run the evaluation once against the baseline cache now.",
        "run the evaluation once against the baseline cache now.",
        "Then we inspect the outputs step by step to confirm the result fully.",
        "A fourth scene talks about the golden label set storage layout.",
        "A fifth scene sums up the whole evaluation story for the learner.",
    ])
    protected = S._protected_terms(plan)
    S._enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    assert S._unsafe_repeat_scenes(plan, protected) == [2]


def test_too_short_repeat_scene_is_flagged_unsafe() -> None:
    plan = _plan([
        "Run the eval gate.",
        "Run the eval gate.",
        "Then inspect the outputs step by step and verify the grading fully now.",
        "And a fourth scene that is completely unique content of its own.",
        "And a fifth scene about the golden labels being protected forever.",
    ])
    protected = S._protected_terms(plan)
    S._enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    assert S._unsafe_repeat_scenes(plan, protected) == [2]


def test_language_policy_is_voice_driven_and_advisory() -> None:
    pure_en = _plan(["The baseline runs on cpu for this simple lesson today."])
    assert S._narration_is_pure_english(pure_en, voice=S._ENGLISH_VOICE) is False
    assert S._narration_is_pure_english(pure_en, voice=S._MHE_VOICE) is True
    assert S._narration_is_pure_english(
        _plan(["aaj hum seekhenge ki kya hai ye module aur kaise kaam karta hai."]),
        voice=S._MHE_VOICE) is False


def test_narration_integrity_problems_detect_repair_damage() -> None:
    plan = _plan([
        "",
        "hi",
        "ok this narration has enough length to speak out",
        ".leads with punctuation to flag",
        "there is an artifact . . right inside the text",
    ])
    problems = S._narration_integrity_problems(plan)
    joined = "\n".join(problems)
    assert "scene 1: empty narration" in joined
    assert "too short to speak" in joined
    assert "starts with punctuation" in joined
    assert "fragment artifact" in joined


def test_cross_scene_shared_sentence_collapsed() -> None:
    plan = _plan([
        "aaj aapne dekha ki kya hua module me sab kuch theek raha. "
        "Unique tail one here for this scene.",
        "aaj aapne dekha ki kya hua module me sab kuch theek raha. "
        "Unique tail two here for that scene.",
        "A third scene about the vector index store that is fully unique here.",
        "A fourth scene about the query routing layer that is fully unique here.",
        "A fifth scene about the answer ranking head that is fully unique here.",
    ])
    changed = S._drop_shared_narration_sentences(plan, quiet=True)
    assert changed == 1
    assert "aaj aapne" in plan["scenes"][0]["narration"]
    assert "aaj aapne" not in plan["scenes"][1]["narration"]
    assert "Unique tail two" in plan["scenes"][1]["narration"]


def test_deepen_speaks_dd_once_and_trims_takeaway_echo() -> None:
    plan = _plan(
        [
            "Load the data into memory for this run.",
            "Score the results against the golden set in this run.",
            "Rank the answers that the retriever produces in this run.",
            "A fourth narration that has nothing in common at all here.",
            "A fifth narration that closes the lesson very nicely now.",
        ],
        takeaways=["the golden set wins"],
        dds=[
            "the golden set wins because caching is much cheaper in the long run",
            "accuracy beats speed for the p95 metric here",
            "accuracy beats speed for the p95 metric here",
            "",
            "",
        ],
    )
    S._deepen_narrations(plan, quiet=True)
    narrations = [str(s["narration"]) for s in plan["scenes"]]
    assert "the golden set wins" in narrations[0]
    assert "because caching is much cheaper in the long run" not in narrations[0]
    assert "accuracy beats speed for the p95 metric here" in narrations[1]
    assert "accuracy beats speed" not in narrations[2]


def test_degenerate_design_decisions_dropped_without_counting_empties() -> None:
    plan = _plan(
        ["n1", "n2", "n3", "n4", "n5"],
        dds=[
            "must be consistent across all modules here",
            "caching the corpus is cheaper in the long run",
            "the retriever scores answer tokens against a golden set",
            "",
            "",
        ],
    )
    removed = S._sanitize_design_decisions(plan)
    assert removed == 1
    assert plan["scenes"][0]["design_decision"] == ""
    assert plan["scenes"][1]["design_decision"] != ""
    assert plan["scenes"][3]["design_decision"] == ""


def test_nar_tokens_canonicalize_case_punctuation_quotes_and_nfkc() -> None:
    tokens = S._nar_tokens("We must Compare, \u2018quality\u2019 \u0964 f\u00e9e?")
    assert tokens == ["we", "must", "compare", "quality", "f\u00e9e"]


def test_mhe_skeleton_templates_collapse_to_unique_heads() -> None:
    plan = _plan([
        "ek ahem module hai, hum yeh samajhenge ki kya baseline data structure "
        "hain, kaise yeh kaam karta hai.",
        "ek ahem module hai, hum yeh samajhenge ki kya scoring gate hain, "
        "kaise yeh kaam karta hai.",
        "chaliye seekhte hai kya retriever hain, kaise yeh bahut useful hai.",
        "totally unique content that shares nothing at all with anyone else.",
        "another totally unique line for the fifth and final scene.",
    ])
    S._dedupe_narration_templates(plan, quiet=True, voice=S._MHE_VOICE)
    heads = S._MHE_VOICE.narr_heads
    assert "ek ahem module hai" not in " ".join(str(s["narration"]) for s in plan["scenes"])
    assert str(plan["scenes"][0]["narration"]).startswith(heads)
    assert "baseline data structure" in plan["scenes"][0]["narration"]
    assert "scoring gate" in plan["scenes"][1]["narration"]
    assert "retriever" in plan["scenes"][2]["narration"]


def test_unknown_narr_voice_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        S._make_voice("klingon")
    assert exc.value.code == 2


def _verify_chain(plan: dict) -> None:
    voice = S._ENGLISH_VOICE
    protected = S._protected_terms(plan)
    S._sanitize_design_decisions(plan)
    S._prune_bullet_takeaway_echo(plan)
    S._dedupe_narration_templates(plan, quiet=True, voice=voice,
                                  protected=protected)
    S._deepen_narrations(plan, quiet=True, voice=voice)
    S._enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)


def test_verify_path_is_idempotent_on_clean_plan() -> None:
    plan = _plan([
        "We compare the baseline numbers against the golden set in this lesson.",
        "The retriever scores each candidate answer against the frozen index.",
        "Then the grading gate verifies precision and recall on the held out set.",
        "The final summary ranks the outputs by the honest p95 metric.",
        "That closes the lesson and tells you what to run next on your data.",
    ])
    _verify_chain(plan)
    snapshot = [str(s["narration"]) for s in plan["scenes"]]
    assert _banned(plan) == []
    again = copy.deepcopy(plan)
    _verify_chain(again)
    assert [str(s["narration"]) for s in again["scenes"]] == snapshot
    assert _banned(again) == []
    assert again.get("_narration_unsafe_scenes") == []


def test_saved_source_protected_trigrams_reused_from_plan() -> None:
    content = "the rag_v2 pipeline runs cpu-only and the LLaMA3 tokenizer for RAG_eval scoring"
    plan = _plan([
        "llama3 tokenizer for rag_eval scoring must test the entire corpus once "
        "before we trust any output here",
        "llama3 tokenizer for rag_eval scoring must test the prompt cache too "
        "so the final labels are stable",
        "a third scene about the vector index store that is entirely unique",
        "a fourth scene about the query routing layer that is entirely unique",
        "a fifth scene about the answer ranking head that is entirely unique",
    ])
    S._persist_protected_trigrams(plan, content)
    protected = S._protected_terms(plan)
    assert "llama3 tokenizer for" in protected
    banned, prot = S._narration_repeat_report(plan, protected)
    assert "llama3 tokenizer for" in prot
    assert "llama3 tokenizer for" not in banned
    assert "must test the" in banned


def test_narration_under_run_warning_flags_short_lessons() -> None:
    """The pre-audio estimate must track the measured speaking pace.

    Regression guard for the calibration: `mod03_gates_v023` rendered 383 spoken
    words into 223.0 s of measured clip time (103.1 wpm). At the old 135 wpm the
    estimate read 2.84 min against a 4.0 min target - a false 71% under-run on a
    lesson that actually measured 93%. These cases pin both ends.
    """
    from doc_to_video_tutor.studio.cli import narration_under_run_warning

    # The real lesson: 93% of target measured, so the estimate must agree.
    assert narration_under_run_warning(383, 4.0) is None
    # A genuinely thin lesson still warns.
    warning = narration_under_run_warning(150, 4.0)
    assert warning is not None
    assert "under-run" in warning
    # A lesson that exceeds the target must not warn either.
    assert narration_under_run_warning(578, 4.0) is None
    # No target means nothing to compare against.
    assert narration_under_run_warning(373, 0.0) is None


def test_report_measured_duration_prefers_measurement(monkeypatch, capsys) -> None:
    """Once audio exists the ffprobe measurement is the authoritative number."""
    from pathlib import Path

    from doc_to_video_tutor.studio import video

    # Five clips totalling 223.0 s == 3.72 min against a 4.0 min target (93%).
    monkeypatch.setattr(video, "_clip_seconds",
                        lambda _p: 223.0 / 5)
    video._report_measured_duration([Path(f"c{i}.mp3") for i in range(5)],
                                    {"target_minutes": 4.0})
    out = capsys.readouterr().out
    assert "3.72 min measured" in out
    assert "93%" in out
    assert "[PASS]" in out
    assert "lesson_duration_short" not in out


def test_audit_binding_rejects_a_swapped_plan(tmp_path) -> None:
    """A renamed or replaced plan must not inherit the old audit's clean bill."""

    from doc_to_video_tutor.studio.util import atomic_json_write, file_digest
    from doc_to_video_tutor.studio.validate import check_audit_binding

    plan_p = tmp_path / "m.plan.json"
    audit_p = tmp_path / "m.audit.json"
    atomic_json_write(plan_p, {"plan": {"title": "original"}})
    atomic_json_write(audit_p, {
        "artifact_type": "plan_audit",
        "plan_sha256": file_digest(plan_p),
        "plan_filename": plan_p.name,
    })
    # The audit genuinely describes this plan.
    assert check_audit_binding(plan_p, audit_p) == []

    # Now swap the plan for different content, leaving the filename identical.
    atomic_json_write(plan_p, {"plan": {"title": "tampered"}})
    findings = check_audit_binding(plan_p, audit_p)
    assert len(findings) == 1
    assert "audit_plan_digest_mismatch" in findings[0]


def test_audit_binding_flags_an_unbound_audit(tmp_path) -> None:
    """An audit with no digest is reported, not assumed good."""
    import json

    from doc_to_video_tutor.studio.util import atomic_json_write
    from doc_to_video_tutor.studio.validate import check_audit_binding

    plan_p = tmp_path / "m.plan.json"
    audit_p = tmp_path / "m.audit.json"
    atomic_json_write(plan_p, {"plan": {}})
    # The legacy shape: a path and nothing to tie it to content.
    atomic_json_write(audit_p, {"plan": "somewhere/else.plan.json"})
    findings = check_audit_binding(plan_p, audit_p)
    assert findings and "audit_plan_digest_missing" in findings[0]
    assert json.loads(audit_p.read_text())["plan"] == "somewhere/else.plan.json"


def test_duration_verdict_is_symmetric(monkeypatch, capsys) -> None:
    """Overshoot must be banded too, not just undershoot.

    The first version cleared anything at or above 92%, so a build that rendered
    109% of target - and one that rendered 300% - both reported PASS.
    """
    from pathlib import Path

    from doc_to_video_tutor.studio import video

    monkeypatch.setattr(video, "_clip_seconds", lambda _p: 4.38 * 60 / 10)
    video._report_measured_duration([Path(f"c{i}.mp3") for i in range(10)],
                                    {"target_minutes": 4.0})
    assert "110%" in capsys.readouterr().out  # 4.38/4.0 rounds up

    # A lesson twice its declared length is a pacing defect, not a pass.
    monkeypatch.setattr(video, "_clip_seconds", lambda _p: 8.0 * 60 / 10)
    video._report_measured_duration([Path(f"c{i}.mp3") for i in range(10)],
                                    {"target_minutes": 4.0})
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "lesson_duration_critical_long" in out
    # And the remedy differs by side: never pad a short one.
    assert "trim" in video._duration_verdict(1.20)[1]
    assert "padding" in video._duration_verdict(0.85)[1]

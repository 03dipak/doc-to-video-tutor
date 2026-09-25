"""Regression tests for the narration-repeat policy (gate + enforcer).

Encodes the contract shared by `verify` and `review` (see LLD §2b / §5):

* A >=3-word phrase repeated across scenes is BANNED only when it is NOT on
  the protected allowlist (takeaways / scene titles / design decisions /
  source trigrams / entity tokens).
* The gate is canonical-token based (NFKC, lowercased, punctuation-stripped),
  so case and punctuation variants of a phrase are the SAME repeat.
* Repairable repeats are removed sentence-aware: whole dangling filler
  sentences are dropped, real-content sentences are only trimmed above the
  minimal spoken length, otherwise the scene is marked 'unsafe' (hard gate).
* `_narration_repeat_report` is the SINGLE source of truth used by both
  `verify`'s fixer chain and `review`'s `guard_plan`, so the two commands can
  never disagree about a repeat.
"""
from doc_to_video_tutor import studio as S


def _plan_with(narrations: list[str]) -> dict:
    return {
        "opening": "",
        "takeaways": [],
        "scenes": [{"section": "Example", "title": f"Scene {i} topic",
                    "topic": f"Scene {i} topic",
                    "source_refs": ["source document"],
                    "narration": n, "design_decision": "", "bullets": []}
                    for i, n in enumerate(narrations, 1)],
    }


def _banned(plan: dict) -> list[str]:
    return S._narration_repeat_report(plan, S._protected_terms(plan))[0]


def test_rebuild_skips_bullet_repeating_an_earlier_bullet() -> None:
    scene = {
        "title": "Verdict semantics",
        "section": "What Is This",
        "design_decision": "",
        "bullets": [
            "Info is recorded for provenance, CI gate ney pass nahi karne ka",
            "Info never breaks a build, kyunki CI gate ney pass nahi karne ka",
        ],
    }
    scenes = [scene]
    rebuilt = S._rebuild_scene_narration(scene, 0, scenes, S._MHE_VOICE)
    assert "recorded for provenance" in rebuilt
    assert rebuilt.count("pass nahi karne ka") == 1


def test_unsafe_repair_converges_when_scene_fields_hold_the_repeat() -> None:
    tail = "CI gate ney pass nahi karne ka"
    plan = {
        "opening": "",
        "takeaways": [],
        "scenes": [
            {"section": "What Is This", "title": "Kind equals verdict semantics",
             "topic": "Kind equals verdict semantics",
             "source_refs": ["source document"], "design_decision": "",
             "bullets": [
                 "Gate is a hard contract that blocks the merge on regression",
                 f"Info is recorded for provenance, {tail}",
                 f"Info never breaks a build, kyunki {tail}",
             ],
             "source_chunk": (
                 "These three kinds control what the evidence means: the gate "
                 "is a hard contract, the guardrail is a soft target, and info "
                 "is recorded only for provenance and never a verdict."),
             "narration": (
                 "Yahan hum ek important piece samjhte hain. "
                 f"Info is recorded for provenance, {tail}. "
                 f"Info never breaks a build, kyunki {tail}.")},
            {"section": "Example", "title": "Tolerance and the two units",
             "topic": "Tolerance and the two units",
             "source_refs": ["source document"], "design_decision": "",
             "bullets": ["Absolute units give a fixed delta for a metric"],
             "narration": (
                 "Ab chaliye tolerance dekhte hain. Absolute units ek fixed "
                 "delta dete hain aur relative units percentage dete hain.")},
        ],
    }
    protected = S._protected_terms(plan)
    assert _banned(plan), "fixture must reproduce the repeated-clause failure"
    S._enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    repaired = S._repair_unsafe_narrations(plan, voice=S._MHE_VOICE,
                                           protected=protected, quiet=True)
    assert repaired == 1
    assert _banned(plan) == []
    assert not [p for p in S._render_blocking_problems(plan, voice=S._MHE_VOICE)
                if "banned" in p or "repeat" in p]


def test_drop_repeated_bullet_clauses_keeps_distinct_bullets() -> None:
    plan = {"scenes": [{
        "title": "Kinds", "narration": "",
        "bullets": [
            "Gate is a hard contract that blocks the merge when a metric fails",
            "Info is a record only signal that never blocks a merge by existing",
            "Guardrail is a soft review target for quality and latency drift",
        ],
    }]}
    assert S._drop_repeated_bullet_clauses(plan) == 0
    assert len(plan["scenes"][0]["bullets"]) == 3

    plan["scenes"][0]["bullets"].append(
        "The nightly lane is informational only because a live judge never "
        "decides whether the merge should be blocked")
    assert S._drop_repeated_bullet_clauses(plan) == 0

    plan["scenes"][0]["bullets"] = [
        "Info is recorded for provenance, CI gate ney pass nahi karne ka",
        "Info never breaks a build, kyunki CI gate ney pass nahi karne ka",
        "Gate blocks the merge when a metric fails its hard tolerance",
    ]
    assert S._drop_repeated_bullet_clauses(plan) == 1
    assert plan["scenes"][0]["bullets"] == [
        "Info is recorded for provenance, CI gate ney pass nahi karne ka",
        "Gate blocks the merge when a metric fails its hard tolerance",
    ]


def test_repeated_filler_is_removed_across_scenes() -> None:
    plan = _plan_with([
        "We must compare the baseline carefully in this lesson before we trust "
        "any number that comes out of the pipeline run.",
        "We must compare the baseline. The golden labels give the honest score "
        "so every result here stays fully reproducible.",
        "Then we inspect the outputs step by step and verify the grading on the "
        "full held out validation set before we write the summary.",
        "A fourth scene explains how the retriever picks the relevant context "
        "chunks from the index in a deterministic order.",
        "The last scene recaps the evaluation gates and tells you what to run "
        "next on your own data for a clean result.",
    ])
    before = _banned(plan)
    removed = S._enforce_unique_narration_trigrams(plan, quiet=True)
    after = _banned(plan)
    assert before
    assert removed > 0
    assert after == []
    assert not plan.get("_narration_unsafe_scenes")
    assert "We must compare the baseline." not in plan["scenes"][1]["narration"]
    assert "We must compare the baseline carefully" in plan["scenes"][0]["narration"]


def test_case_variant_repeat_is_detected() -> None:
    plan = _plan_with([
        "We must Compare the Baseline method carefully before trusting any of "
        "the final numbers in this lesson.",
        "we must compare the baseline method against a golden set of labels so "
        "every score stays honest and fully reproducible.",
        "Then we inspect the outputs step by step and verify the grading on the "
        "full held out validation set before we write the summary.",
        "A fourth scene explains how the retriever picks the relevant context "
        "chunks from the index in a deterministic order.",
        "The last scene recaps the evaluation gates and tells you what to run "
        "next on your own data for a clean result.",
    ])
    banned = _banned(plan)
    assert "we must compare" in banned
    assert "compare the baseline" in banned
    assert "the baseline method" in banned


def test_punctuation_variant_repeat_is_detected() -> None:
    plan = _plan_with([
        "We must compare, the baseline tool, carefully in this lesson before "
        "trusting any of the final numbers.",
        "We must compare the baseline tool against a golden set of labels so "
        "every score stays honest and fully reproducible.",
        "Then we inspect the outputs step by step and verify the grading on the "
        "full held out validation set before we write the summary.",
        "A fourth scene explains how the retriever picks the relevant context "
        "chunks from the index in a deterministic order.",
        "The last scene recaps the evaluation gates and tells you what to run "
        "next on your own data for a clean result.",
    ])
    banned = _banned(plan)
    assert "we must compare" in banned
    assert "compare the baseline" in banned
    assert "the baseline tool" in banned


def test_within_scene_trigram_repeat_is_detected() -> None:
    plan = _plan_with([
        "We must compare the baseline today before we finalize the results of "
        "this run. We must compare the baseline again tomorrow to double check "
        "every single number in the final report.",
        "Then we inspect the outputs step by step and verify the grading on the "
        "full held out validation set before we write the summary.",
        "A third scene explains how the retriever picks the relevant context "
        "chunks from the index in a deterministic order.",
        "A fourth scene recaps the evaluation gates of the pipeline.",
        "The last scene tells you what to run next on your own data.",
    ])
    changed = S._drop_repeated_filler(plan, quiet=True)
    narration = plan["scenes"][0]["narration"]
    assert changed == 1
    assert "again tomorrow" not in narration
    assert "We must compare the baseline today" in narration


def test_within_scene_exact_repeats_are_removed_even_when_protected() -> None:
    repeated = (
        "Check structure before comparing values. "
        "Prioritize FAIL over REVIEW over PASS. "
        "Check structure before comparing values. "
        "Prioritize FAIL over REVIEW over PASS. "
        "Check structure before comparing values. "
        "Prioritize FAIL over REVIEW over PASS."
    )
    plan = _plan_with([
        repeated,
        "A second scene explains the golden label storage layout clearly.",
        "A third scene describes the retriever and its deterministic index order.",
        "A fourth scene records the verdict output for the complete evaluation run.",
        "A final scene gives the learner a clean next step for testing this gate.",
    ])
    plan["takeaways"] = [
        "check structure before comparing values",
        "prioritize fail over review over pass",
    ]
    protected = S._protected_terms(plan)
    removed = S._enforce_unique_narration_trigrams(plan, quiet=True,
                                                    protected=protected)
    assert removed >= 1
    assert plan["scenes"][0]["narration"].count("Check structure") == 1
    assert plan["scenes"][0]["narration"].count("Prioritize FAIL") == 1
    assert not S._narration_repeat_report(plan, protected)[0]


def test_cross_scene_trigram_repeat_is_detected() -> None:
    plan = _plan_with([
        "Introduce the frozen baseline cache first so all of the comparisons "
        "stay stable across every run in this lesson.",
        "The frozen baseline cache must be protected so we never overwrite the "
        "golden labels by accident during evaluation.",
        "Then we inspect the outputs step by step and verify the grading on the "
        "full held out validation set before we write the summary.",
        "A fourth scene explains how the retriever picks the relevant context "
        "chunks from the index in a deterministic order.",
        "The last scene recaps the evaluation gates and tells you what to run "
        "next on your own data for a clean result.",
    ])
    banned = _banned(plan)
    assert "the frozen baseline" in banned
    assert "frozen baseline cache" in banned


def test_verify_and_review_use_identical_repeat_contract() -> None:
    repairable = _plan_with([
        "We must compare the baseline carefully in this lesson before we trust "
        "any number that comes out of the pipeline run.",
        "We must compare the baseline. The golden labels give the honest score "
        "so every result here stays fully reproducible.",
        "Then we inspect the outputs step by step and verify the grading on the "
        "full held out validation set before we write the summary.",
        "A fourth scene explains how the retriever picks the relevant context "
        "chunks from the index in a deterministic order.",
        "The last scene recaps the evaluation gates and tells you what to run "
        "next on your own data for a clean result.",
    ])
    voice = S._ENGLISH_VOICE
    protected = S._protected_terms(repairable)
    assert _banned(repairable)
    problems = S.guard_plan(repairable, [], voice=voice)
    assert any(p.startswith("repeating narration phrase") for p in problems)

    S._dedupe_narration_templates(repairable, quiet=True, voice=voice,
                                  protected=protected)
    S._deepen_narrations(repairable, quiet=True, voice=voice)
    S._enforce_unique_narration_trigrams(repairable, quiet=True,
                                         protected=protected)
    after = _banned(repairable)
    assert after == []
    assert not any(p.startswith(("repeating narration phrase",
                                 "unrepairable narration repeat"))
                   for p in S.guard_plan(repairable, [], voice=voice))

    unrepairable = _plan_with([
        "We must compare the baseline before trusting any final number.",
        "We must compare the baseline before trusting any final number.",
        "Then we inspect the outputs step by step and verify the grading on the "
        "full held out validation set before we write the summary.",
        "A fourth scene explains how the retriever picks the relevant context "
        "chunks from the index in a deterministic order.",
        "The last scene recaps the evaluation gates and tells you what to run "
        "next on your own data for a clean result.",
    ])
    uprotected = S._protected_terms(unrepairable)
    S._enforce_unique_narration_trigrams(unrepairable, quiet=True,
                                         protected=uprotected)
    assert S._unsafe_repeat_scenes(unrepairable, uprotected) == [2]
    assert any(p.startswith("unrepairable narration repeat")
               for p in S.guard_plan(unrepairable, [], voice=voice))


def _voice() -> object:
    return S._MHE_VOICE


def _fix_chain(plan: dict) -> None:
    """Mirror verify's repair pipeline."""
    S._sanitize_design_decisions(plan)
    S._prune_bullet_takeaway_echo(plan)
    protected = S._protected_terms(plan)
    S._dedupe_narration_templates(plan, quiet=True, voice=_voice(),
                                  protected=protected)
    S._deepen_narrations(plan, quiet=True, voice=_voice())
    S._enforce_unique_narration_trigrams(plan, quiet=True,
                                         protected=protected)


def _dd_plan(shared_preamble: str) -> dict:
    """Scenes with matching 'why step9 chose it' design decisions; one already
    carries that phrase in its narration (the flap repro from mod03_gates_v003):
    repeated passes used to re-assign the shared phrase to a DIFFERENT scene
    each time, so verify never converged."""
    return {
        "opening": "",
        "takeaways": [],
        "scenes": [
            {"title": "S1",
             "narration": "first scene walks through the comparison flow once "
                          "before any number gets written down.",
             "design_decision": "", "bullets": []},
            {"title": "S2",
             "narration": "second scene already explains core decision: "
                          "why step9 chose it right here for the audience.",
             "design_decision": shared_preamble + " one specific reason for "
                             "this slot of the pipeline.", "bullets": []},
            {"title": "S3",
             "narration": "third scene talks about the registry being pure "
                          "data that drives the compare step mechanically.",
             "design_decision": shared_preamble + " a different reason for "
                             "that other slot of the pipeline.", "bullets": []},
            {"title": "S4",
             "narration": "fourth scene introduces the sample size floor and "
                          "how a single flip must always trip the gate.",
             "design_decision": shared_preamble + " yet another reason for "
                             "the remaining slot of the pipeline.", "bullets": []},
            {"title": "S5",
             "narration": "fifth scene recaps the precedence rules and what "
                          "to run next on your own data for a clean result.",
             "design_decision": "", "bullets": []},
        ],
    }


def test_deepen_is_idempotent_with_shared_dd_preamble() -> None:
    plan = _dd_plan("why step9 chose it:")
    _fix_chain(plan)
    first = [s["narration"] for s in plan["scenes"]]
    S._deepen_narrations(plan, quiet=True, voice=_voice())
    second = [s["narration"] for s in plan["scenes"]]
    print(first)
    assert first == second


def test_verify_chain_converges_after_repeated_run() -> None:
    plan = _dd_plan("why step9 chose it:")
    _fix_chain(plan)
    one = [s["narration"] for s in plan["scenes"]]
    _fix_chain(plan)
    two = [s["narration"] for s in plan["scenes"]]
    assert one == two
    assert not S._narration_repeat_report(plan, S._protected_terms(plan))[0]


def _scene_plan(n_scenes: int) -> dict:
    return {
        "opening": "",
        "takeaways": [],
        "scenes": [
            {"title": f"Scene {i} topic", "narration":
             f"Scene {i} talks about one distinct idea in the lesson before "
             "we move to the next clean topic.", "design_decision": "",
             "bullets": []}
            for i in range(1, n_scenes + 1)
        ],
    }


def test_scene_count_over_target_is_a_hard_problem_not_clamped() -> None:
    plan = _scene_plan(10)
    assert len(plan["scenes"]) == 10
    scene_problem = S._scene_count_problem(plan)
    assert "scene count 10" in scene_problem
    problems = S.guard_plan(plan, [], voice=S._MHE_VOICE)
    hard = [p for p in problems if not p.startswith(S._SOFT_PREFIXES)]
    assert any("scene count 10" in p for p in hard)


def test_trim_scene_overflow_keeps_first_8_records_audit() -> None:
    plan = _scene_plan(11)
    for i, sc in enumerate(plan["scenes"], 1):
        sc["section"] = f"Section {i}"
        sc["bullets"] = [f"scene {i} unique anchor fact.", f"bullet text {i}."]
    plan["takeaways"] = [
        "xylophone metric",      # invented; anchored to neither retained nor dropped
        "bullet text 1",         # anchored to a RETAINED scene
    ]
    dropped = S._trim_scene_overflow(plan)
    assert dropped == 3
    assert len(plan["scenes"]) == 8
    assert [sc["title"] for sc in plan["scenes"]] == \
        [f"Scene {i} topic" for i in range(1, 9)]
    report = plan["_overflow_report"]
    assert report["scene_count_original"] == 11
    assert report["scene_count_final"] == 8
    assert [d["original_index"] for d in report["dropped_scenes"]] == [9, 10, 11]
    assert [d["section"] for d in report["dropped_scenes"]] == \
        ["Section 9", "Section 10", "Section 11"]
    assert report["scene_overflow_policy"]["action"] == "trim_prefix"
    assert all("xylophone" not in t for t in plan["takeaways"])
    assert any("bullet text 1" in t for t in plan["takeaways"])


def test_trim_scene_overflow_noop_within_range_and_rejects_over_12() -> None:
    eight = _scene_plan(8)
    assert S._trim_scene_overflow(eight) == 0
    assert "_overflow_report" not in eight
    plan13 = _scene_plan(13)
    assert S._trim_scene_overflow(plan13) == 0
    assert len(plan13["scenes"]) == 13
    assert "_overflow_report" not in plan13
    assert "scene count 13" in S._scene_count_problem(plan13)


def test_trim_dropped_topic_flags_only_after_overflow_trim() -> None:
    a = _scene_plan(8)
    outline = ["Scope", "Error budget", "Golden set"]
    assert S._trim_dropped_topic(a, outline) == []
    b = _scene_plan(11)
    for i, sc in enumerate(b["scenes"]):
        sc["bullets"] = [f"scene {i} talks about a distinct doc topic."]
    S._trim_scene_overflow(b)
    assert S._trim_dropped_topic(b, outline) == outline


def test_filter_takeaways_to_retained_spares_grounded_takeaways() -> None:
    plan = _scene_plan(12)
    for i, sc in enumerate(plan["scenes"]):
        sc["bullets"] = [f"fact item {i}: a retained detail."]
    plan["takeaways"] = [
        "quasar zone brief",   # invented; anchored to neither retained nor dropped
        "fact item 1",         # anchored to a RETAINED scene
    ]
    S._trim_scene_overflow(plan)
    taken = plan["takeaways"]
    assert all("quasar zone" not in t for t in taken)
    assert any(t == "fact item 1" for t in taken)


def _grounded_plan() -> tuple[dict, set, set]:
    plan = _scene_plan(3)
    for i, sc in enumerate(plan["scenes"], 1):
        sc["title"] = f"Scene {i} distinct idea"
        sc["bullets"] = [f"scene {i} talks about one distinct idea in the lesson."]
    del plan["takeaways"]
    bg = {("scene", "one"), ("one", "distinct"), ("distinct", "idea"),
          ("idea", "lesson")}
    tk = {"scene", "one", "distinct", "idea", "lesson", "metric", "registry"}
    return plan, bg, tk


def test_drop_ungrounded_slide_text_drops_drift_keeps_anchored() -> None:
    plan, bg, tk = _grounded_plan()
    tk |= {"verdict", "compare", "code", "exit"}
    plan["scenes"][0]["bullets"].append("latency vibes are cool under the hood.")
    plan["scenes"][0]["bullets"].append("the exit code of compare IS the verdict.")
    issues = S._grounding_issues(plan, bg, tk)
    assert any("latency vibes" in i for i in issues)
    dropped = S._drop_ungrounded_slide_text(plan, bg, tk)
    assert dropped == 1
    remaining = [b for sc in plan["scenes"] for b in sc["bullets"]]
    assert "latency vibes" not in remaining
    assert any("exit code of compare" in b for b in remaining)
    assert all(not i.startswith("scene 1 ungrounded: 'latency")
               for i in S._grounding_issues(plan, bg, tk))


def test_drop_ungrounded_slide_text_retitles_only_when_needed() -> None:
    plan, bg, tk = _grounded_plan()
    assert S._grounding_issues(plan, bg, tk) == []
    assert S._drop_ungrounded_slide_text(plan, bg, tk) == 0
    assert plan["scenes"][0]["title"] == "Scene 1 distinct idea"

    drifted, bg2, tk2 = _grounded_plan()
    drifted["scenes"][0]["title"] = "Totally invented heading nobody wrote"
    drifted["scenes"][0]["topic"] = "one distinct idea in the lesson"
    assert S._drop_ungrounded_slide_text(drifted, bg2, tk2) == 1
    assert drifted["scenes"][0]["title"] == "one distinct idea in the lesson"
    assert S._grounding_issues(drifted, bg2, tk2) == []


def _unsafe_plan() -> (tuple)[dict, dict]:
    scenes = [
        {"title": "Compare gates module", "design_decision": "why compare first: "
         "the baseline cache gives an honest score", "bullets": [
             "baseline cache holds frozen results.", "run the delta comparison."],
         "narration": "How does the compare module work?"},
        {"title": "Eval registry module",
         "design_decision": "registry drives the tool mechanically",
         "bullets": ["registry is pure data driving the compare step."],
         "narration": "How does the compare module work?"},
    ]
    plan = {"opening": "", "takeaways": [], "scenes": scenes}
    return plan, {"compare", "module", "work", "how", "does", "the"}


def test_repair_unsafe_narrations_rebuilds_too_short_repeating_scene() -> None:
    plan, _toks = _unsafe_plan()
    protected = S._protected_terms(plan)
    S._enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    assert S._unsafe_repeat_scenes(plan, protected) == [2]
    repaired = S._repair_unsafe_narrations(plan, voice=S._MHE_VOICE,
                                           protected=protected)
    assert repaired >= 1
    assert S._unsafe_repeat_scenes(plan, protected) == []
    assert not _banned(plan)


def test_repair_unsafe_narrations_reuses_own_validated_fields() -> None:
    plan, _toks = _unsafe_plan()
    protected = S._protected_terms(plan)
    S._enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    S._repair_unsafe_narrations(plan, voice=S._MHE_VOICE, protected=protected)
    rebuilt = plan["scenes"][1]["narration"]
    assert any(w in rebuilt.lower()
               for w in ("registry", "compare module", "baseline cache"))
    assert len(S._nar_tokens(rebuilt)) >= 12
    assert not _banned(plan)


def test_repair_unsafe_narrations_noop_when_no_banned_repeats() -> None:
    plan = _plan_with([
        "first scene walks through the index in a deterministic order only.",
        "A second distinct scene talks about the golden labels honestly here.",
        "The third scene covers how the output verification reruns cleanly.",
        "A fourth scene explains how the retriever ranks every candidate first.",
        "The fifth scene recaps the precedence rules and gates for a clean run.",
    ])
    protected = S._protected_terms(plan)
    S._enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    assert not _banned(plan)
    repaired = S._repair_unsafe_narrations(plan, voice=S._MHE_VOICE,
                                           protected=protected)
    assert repaired == 0


def test_render_blocking_problems_blocks_banned_repeats() -> None:
    plan, _toks = _unsafe_plan()
    blocking = S._render_blocking_problems(plan, voice=S._MHE_VOICE)
    assert any("RENDER-BLOCKING" in b and "banned narration phrase" in b
               for b in blocking)


def test_render_blocking_problems_pass_on_clean_plan() -> None:
    plan = _plan_with([
        "first scene walks through the index in a deterministic order only.",
        "A second distinct scene talks about the golden labels honestly here.",
        "The third scene covers how the output verification reruns cleanly.",
        "A fourth scene explains how the retriever ranks every candidate first.",
        "The fifth scene recaps the precedence rules and gates for a clean run.",
    ])
    assert S._render_blocking_problems(plan, voice=S._MHE_VOICE) == []


def test_render_blocking_problems_flags_out_of_range_scene_count() -> None:
    plan = _scene_plan(3)
    blocking = S._render_blocking_problems(plan, voice=S._MHE_VOICE)
    assert any("RENDER-BLOCKING" in b and "scene count 3" in b for b in blocking)


def test_concept_headers_extract_from_markdown() -> None:
    md = (
        "# Mod 3\n\nintro text\n\n"
        "## Baseline Snapshot & Compare\n\nbody one\n\n"
        "### Kind = Verdict Semantics\n\nbody two\n\n"
        "## Tolerance & Two Units\n\nbody three\n\n"
        "## Out of Scope\n\nskip me\n\n"
        "## Baseline Snapshot & Compare\n\ndup should be dropped\n\n"
        "## Table of Contents\n\nskip me too\n")
    heads = S._concept_headers(md)
    assert heads == ["Baseline Snapshot & Compare",
                     "Kind = Verdict Semantics",
                     "Tolerance & Two Units"]
    assert len(heads) == 3


def test_paginate_plan_slides_repairs_stale_pages() -> None:
    plan = {"scenes": [{
        "title": "Registry", "narration": "", "bullets": ["kept one", "kept two"],
        "bullet_pages": [["dropped", "kept one", "kept two"]],
    }]}
    assert S._paginate_plan_slides(plan) == 1
    assert plan["scenes"][0]["bullet_pages"] == [["kept one", "kept two"]]


def test_render_gate_blocks_stale_bullet_pages() -> None:
    plan = _plan_with(["The registry types every metric so verdicts stay mechanical."])
    plan["scenes"][0]["bullets"] = ["kept one", "kept two"]
    plan["scenes"][0]["bullet_pages"] = [["dropped", "kept one", "kept two"]]
    problems = S._render_blocking_problems(plan, voice=S._MHE_VOICE)
    assert any("bullet_pages does not match bullets" in problem
               for problem in problems)


def test_technical_visual_fallback_covers_registry_and_kinds() -> None:
    from doc_to_video_tutor.studio.plan import _ensure_technical_visuals
    plan = {"scenes": [
        {"title": "Metric registry", "design_decision": "",
         "bullets": [], "source_chunk": "kind direction tolerance unit",
         "visual_diagram": ""},
        {"title": "Verdict semantics", "design_decision": "",
         "bullets": [], "source_chunk": "gate guardrail info verdict",
         "visual_diagram": ""},
    ]}
    assert _ensure_technical_visuals(plan) == 2
    assert "Tolerance" in plan["scenes"][0]["visual_diagram"]
    assert "Guardrail" in plan["scenes"][1]["visual_diagram"]


def test_dd_leads_are_colon_free_for_speech() -> None:
    assert all(not lead.strip().endswith(":") for lead in S._DD_LEADS)
    assert "Iska ahem reason yeh hai ki " in S._DD_LEADS


def test_concept_headers_skip_container_and_overview_headings() -> None:
    md = "\n".join([
        "## The one sentence to memorize",
        "## The 5 big ideas",
        "## Concept by concept",
        "### 1. First real concept",
        "### 2. Second real concept",
        "## Checkpoints",
        "## Now read the LLD",
    ])
    assert S._concept_headers(md) == [
        "1. First real concept",
        "2. Second real concept",
    ]


def test_concept_headers_bounded_to_schema_range() -> None:
    md = "\n".join(f"## Concept number {i}" for i in range(30))
    heads = S._concept_headers(md)
    assert len(heads) == S.AUTO_TRIM_MAX_SCENES


def test_scene_count_contract_honors_source_concepts() -> None:
    good = _plan_with(["narration one"] * 12)
    good["scene_target"] = 12
    good["source_concepts"] = [f"concept {i}" for i in range(12)]
    assert S._scene_count_problem(good) == ""
    bad = _plan_with(["narration one"] * 6)
    bad["scene_target"] = 12
    bad["source_concepts"] = [f"concept {i}" for i in range(12)]
    msg = S._scene_count_problem(bad)
    assert "1:1 concept contract" in msg and "6" in msg and "12" in msg
    assert S._render_blocking_problems(bad, voice=S._MHE_VOICE)


def test_plan_scene_target_bounds() -> None:
    assert S._plan_scene_target(None) == 6
    assert S._plan_scene_target(["c"] * 6) == 6
    assert S._plan_scene_target(["c"] * 12) == 12  # <=12 -> 1:1
    assert S._plan_scene_target(["c"] * 30) == 12  # >12 -> merged to 12 slots


def test_concept_groups_cover_every_source_concept() -> None:
    concepts = [f"Concept {i:02d} name" for i in range(1, 13)]
    groups = S._concept_groups(concepts)
    assert len(groups) == 12
    flat = " | ".join(groups)
    for c in concepts:
        assert c in flat  # no concept silently dropped
    assert groups[0] == "Concept 01 name"  # 1:1 at 12-concept ceiling
    assert len(S._concept_groups(concepts[:6])) == 6  # 1:1 when within range


def test_concrete_values_population_is_source_grounded() -> None:
    from doc_to_video_tutor.studio.plan import _populate_concrete_values

    pointer = {
        "title": "Canonical pointer", "source_chunk": (
            "active.json is a tiny pointer {schema_version, baseline_id, path} "
            "that names which committed baseline is current."),
    }
    assert _populate_concrete_values(pointer, str(pointer["source_chunk"])) == 1
    assert "schema_version" in pointer["json_snippet"]
    assert "baseline_id" in pointer["json_snippet"]
    # Re-running must not rewrite what is already there.
    before = pointer["json_snippet"]
    assert _populate_concrete_values(pointer, str(pointer["source_chunk"])) == 0
    assert pointer["json_snippet"] == before


def test_value_table_never_renders_exit_codes_as_thresholds() -> None:
    from doc_to_video_tutor.studio.plan import _populate_concrete_values

    exit_codes = {
        "title": "Structural error classes",
        "source_chunk": ("Plain words: exit code 0 PASS, 1 FAIL, 2 REVIEW are "
                         "verdicts, while 3 and 4 are configuration errors."),
    }
    _populate_concrete_values(exit_codes, str(exit_codes["source_chunk"]))
    assert not exit_codes.get("value_table")


def test_value_table_requires_two_boundary_rows() -> None:
    from doc_to_video_tutor.studio.plan import _populate_concrete_values

    single = {"title": "Tolerance", "source_chunk":
              "the inclusive band is 0.97 PASS / 0.96 FAIL for this metric."}
    _populate_concrete_values(single, str(single["source_chunk"]))
    assert not single.get("value_table")

    pair = {"title": "Tolerance", "source_chunk": (
        "boundary PASS 0.97 with FAIL 0.96 verified, and relative 120ms PASS "
        "with 121ms REVIEW recorded.")}
    assert _populate_concrete_values(pair, str(pair["source_chunk"])) == 1
    assert len(pair["value_table"]) == 2
    assert all("|" in row for row in pair["value_table"])


def test_status_badges_populated_from_real_exit_code_map() -> None:
    from doc_to_video_tutor.studio.plan import _populate_status_badges

    scene = {"title": "Structural error classes", "source_chunk": (
        "exit code 0 PASS, 1 FAIL, 2 REVIEW are verdicts. 3 = evaluation/input "
        "error and 4 = config/baseline error for a broken pointer.")}
    assert _populate_status_badges(scene, str(scene["source_chunk"])) == 1
    badges = scene["status_badges"]
    assert [b["code"] for b in badges] == ["0", "1", "2", "3", "4"]
    assert [b["state"] for b in badges] == ["PASS", "FAIL", "REVIEW",
                                            "ERROR", "ERROR"]
    assert badges[3]["label"] == "EVAL ERR"
    assert badges[4]["label"] == "CONFIG ERR"
    # Idempotent: an existing row is never rewritten.
    assert _populate_status_badges(scene, str(scene["source_chunk"])) == 0


def test_status_badges_absent_without_the_verdict_triple() -> None:
    from doc_to_video_tutor.studio.plan import _populate_status_badges

    partial = {"title": "Tolerance", "source_chunk":
               "The gate reports 0 PASS or 1 FAIL on a metric boundary."}
    assert _populate_status_badges(partial, str(partial["source_chunk"])) == 0
    assert not partial.get("status_badges")

    unrelated = {"title": "Baseline", "source_chunk":
                 "A baseline stores a known-good snapshot of every metric."}
    assert _populate_status_badges(unrelated, str(unrelated["source_chunk"])) == 0
    assert not unrelated.get("status_badges")


def test_status_badges_error_label_falls_back_without_keywords() -> None:
    from doc_to_video_tutor.studio.plan import _populate_status_badges

    scene = {"title": "Codes", "source_chunk": (
        "0 PASS, 1 FAIL, 2 REVIEW are verdicts. 3 = runtime breakdown of the "
        "harness itself.")}
    assert _populate_status_badges(scene, str(scene["source_chunk"])) == 1
    labels = {b["code"]: b["label"] for b in scene["status_badges"]}
    assert labels["3"] == "ERROR"


def test_scene_title_repair_targets_only_truncated_titles() -> None:
    from doc_to_video_tutor.studio.plan import _normalize_scene_titles

    truncated = "The 11-step precedence — structure before va"
    plan = {"scenes": [
        {"title": truncated,
         "topic": "The 11-step precedence — structure before values, FAIL over REVIEW"},
        {"title": "M4 Tolerance units", "topic": "M4 Tolerance units"},
    ]}
    assert _normalize_scene_titles(plan) == 1
    repaired = plan["scenes"][0]["title"]
    assert repaired == "The 11-step precedence — structure before"
    assert not repaired.endswith("va")
    # A title that was never truncated keeps the model's wording and label.
    assert plan["scenes"][1]["title"] == "M4 Tolerance units"


def test_source_chunk_assignment_matches_headings_and_stays_exclusive() -> None:
    """Chunk assignment must see the heading, and no section may serve two scenes.

    Both properties were missing: `_markdown_sections` returns (heading, body)
    and the matcher compared against the body only, so the document's most
    discriminative text was never in the comparison set. And a per-scene argmax
    let several scenes collapse onto the same overview section.
    """
    from doc_to_video_tutor.studio.plan import _annotate_source_chunks

    doc = (
        "# Guide\n\n"
        "## Overview\n\n"
        "Baselines tolerance units precedence verdict are all mentioned here so "
        "this summary section overlaps every scene in the document.\n\n"
        "### 1. Baseline snapshot\n\n"
        "Freeze a known good baseline snapshot and diff every new run against "
        "it to see what changed.\n\n"
        "### 2. Tolerance units\n\n"
        "Tolerance is expressed in two units, and the two units must agree "
        "before a verdict is allowed to pass.\n"
    )
    plan = {"scenes": [
        {"title": "Baseline snapshot", "bullets": ["diff every new run"],
         "design_decision": "freeze a known good baseline"},
        {"title": "Tolerance units", "bullets": ["the two units must agree"],
         "design_decision": "tolerance is expressed in two units"},
    ]}
    assert _annotate_source_chunks(plan, doc) == 2
    records = plan["source_assignment"]
    assert [r["status"] for r in records] == ["assigned", "assigned"]
    # Each scene landed on its own concept section, not on the overview.
    assert "Baseline snapshot" in records[0]["heading"]
    assert "Tolerance units" in records[1]["heading"]
    assert records[0]["heading"] != records[1]["heading"]
    # Provenance is recorded as a content digest, not just a display string.
    for record in records:
        assert len(record["section_digest"]) == 16


def test_source_chunk_assignment_refuses_a_weak_match() -> None:
    """No confident match means no excerpt, not an unrelated one."""
    from doc_to_video_tutor.studio.plan import _annotate_source_chunks

    doc = ("# Guide\n\n### Alpha\n\n"
           "zqx wibble frobnicate gronk.\n")
    plan = {"scenes": [{"title": "Tolerance units",
                        "bullets": ["narration safety gate"],
                        "design_decision": "block the provider call"}]}
    assert _annotate_source_chunks(plan, doc) == 0
    assert "source_chunk" not in plan["scenes"][0]
    record = plan["source_assignment"][0]
    assert record["status"] in ("low_confidence", "unassigned")

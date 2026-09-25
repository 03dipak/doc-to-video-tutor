"""Regressions pinned from real `build` runs.

Every case here was observed in an actual build of
`08_concepts_mod03_gates.md`, not constructed to suit a rule. They exist because
each one is a behaviour a future change could plausibly break while still
passing the unit tests: a gate that must *stop the build*, a band boundary, and
a detector that only fires on messy model output.

The identifying details are kept verbatim - the Devanagari opening the 7B
actually emitted, the 71-word scene, the fused token - because a paraphrased
fixture would test the idea rather than the incident.
"""

import pytest

from doc_to_video_tutor import studio as S

# --- the build that failed: Devanagari in the plan opening ------------------
#
# mod03_gates_v012_001 sample 1 was rejected with
#   plan opening SlideTextNotEnglish: non-Latin script:
#   'Chaliye regression samajh ke shuruwat karte hai. आज आपल्याला गेट'
# and the whole plan was resampled. The gate did its job; what was not pinned
# anywhere was that this class of failure forces a *resample* rather than being
# tolerated as a soft sample-level warning.

def test_real_devanagari_opening_is_rejected_by_the_plan_gate() -> None:
    problems = S.guard_plan(
        {"opening": "Chaliye regression samajh ke shuruwat karte hai. "
                    "आज आपल्याला गेट",
         "title": "Regression gates",
         "scenes": []},
        ["regression gates"])
    assert any("SlideTextNotEnglish" in p for p in problems), problems


def test_non_latin_opening_failure_is_not_soft_so_it_forces_a_resample() -> None:
    """The resample branch only fires for problems that survive the soft filter."""
    from doc_to_video_tutor.studio.cli import _SAMPLE_SOFT_EXTRA

    problems = ["plan opening SlideTextNotEnglish: non-Latin script: 'x'"]
    soft_prefixes = S._SOFT_PREFIXES + _SAMPLE_SOFT_EXTRA
    hard = [p for p in problems
            if not any(p.startswith(soft) for soft in soft_prefixes)]
    assert hard, "a non-Latin opening must count as hard, or the build lands it"


def test_sanitize_opening_does_not_strip_devanagari_so_rejection_is_unreproducible() -> None:
    """A known gap, pinned so it cannot be quietly forgotten.

    Observed in mod03_gates_v012_001: the build log rejected sample 1 for
    `plan opening SlideTextNotEnglish`, yet the persisted
    `*.sample1.plan.json` has no Devanagari in its opening and passes
    `guard_plan` with zero problems. So the artifact written beside the rejected
    sample does not reproduce the failure that rejected it, and the saved plan
    cannot be used to audit why the sample was discarded.

    `_sanitize_opening` is *not* the cause - it only strips field-name leakage
    such as a trailing " opening", and leaves non-Latin script untouched, which
    this asserts so the two are not confused again. Recorded as LLD 20.2
    defect 14.
    """
    plan = {"opening": "Chaliye regression samajh ke shuruwat karte hai. "
                       "आज आपल्याला गेट opening"}
    S._sanitize_opening(plan)
    # The field-leak strip happens...
    assert not str(plan["opening"]).rstrip().endswith("opening")
    # ...but the non-Latin script survives, so the gate is what rejects it.
    assert S._has_non_latin_script(str(plan["opening"]))


# --- the band boundary: a scene one word over the warn line ---------------
#
# mod03_gates_v012_002 reported `tts_too_long: scene:1 71 spoken words`. That is
# 1 word past the warn threshold and 19 short of the fail threshold, so the exact
# boundary is what decides whether a build continues or dies.

def test_seventy_one_spoken_words_warns_but_does_not_block() -> None:
    plan = {"scenes": [{
        "title": "Goldens",
        "narration": " ".join(
            f"Step {n} keeps the goldens honest." for n in range(14)),
        "bullets": ["Freeze the goldens before the first run."],
    }]}
    script = S.build_tts_script(plan, S._MHE_VOICE)
    # Pinned exactly: this is the boundary case the build actually hit.
    assert script["clips"][0]["word_count"] == 71
    codes = {item["code"] for item in script["audit"]}
    assert "tts_too_long" in codes
    assert "tts_too_long_critical" not in codes, (
        "71 words must not block; the fail line is 90")


def test_the_fail_threshold_is_ninety_spoken_words() -> None:
    plan = {"scenes": [{
        "title": "Data and testset",
        "narration": " ".join(
            f"Step {n} keeps the goldens honest across every single run."
            for n in range(24)),
        "bullets": ["Freeze the goldens before the first run."],
    }]}
    script = S.build_tts_script(plan, S._MHE_VOICE)
    assert any(item["code"] == "tts_too_long_critical"
               for item in script["audit"])


# --- the fusion detector, on the sentence the model actually wrote ---------
#
# mod03_gates_v012_002 scene 9:
#   "Reason yahi hai kideterministic offline gate not live online because ..."
# The token is model-authored and absent from the source, so it survives every
# deterministic repair pass. The detector is the only thing that sees it.

def test_real_fusion_sentence_is_detected_with_the_right_split() -> None:
    from doc_to_video_tutor.studio.speech import _fused_particle_tokens

    spoken = ("Is scene mein hum ek fresh concept samjhenge. Reason yahi hai "
              "kideterministic offline gate not live online because the gate runs "
              "a fully deterministic, offline evaluator, ensuring that the "
              "verdict is consistent and not influenced by anything else.")
    known = frozenset({"deterministic", "offline", "gate", "verdict", "reason",
                       "yahi", "hai", "online", "live", "concept", "scene",
                       "hum", "ek", "fresh", "samjhenge"})
    assert _fused_particle_tokens(spoken, known) == {
        "kideterministic": "ki deterministic"}


def test_fusion_surfaces_as_a_warning_and_does_not_block_the_build() -> None:
    """A reported fusion is advisory: the script still builds and still speaks."""
    plan = {"scenes": [{
        "title": "Two lane design",
        "narration": ("Is scene mein hum ek fresh concept samjhenge. Reason yahi "
                      "hai kideterministic offline gate not live online because "
                      "the gate runs a fully deterministic evaluator. "
                      "Deterministic results keep the verdict consistent across "
                      "every nightly run of the offline evaluation pipeline."),
        "bullets": ["The offline lane is deterministic."],
    }]}
    script = S.build_tts_script(plan, S._MHE_VOICE)
    codes = {item["code"] for item in script["audit"]}
    assert "tts_token_fusion" in codes
    # Advisory means advisory: no FAIL-code is raised for the same scene.
    assert not any(code.endswith("_critical") or code in
                   {"tts_text_corrupted", "tts_unknown_token"}
                   for code in codes), codes
    # And the narration is not silently rewritten.
    assert "kideterministic" in script["clips"][0]["spoken"]


# --- deck layout: text that overflows its card ----------------------------
#
# Reported from the rendered deck: a filled box sitting over the words
# "Why THIS (not the alternative)". The card was a fixed 0.72 in tall with a
# 0.6 in text box, and the fit check used that same fixed height, so a
# 194-character decision needing three lines reported room it did not have.
# A PowerPoint textbox does not clip, so the text drew over the card instead of
# being hidden - which is why nothing looked wrong in the geometry.

def test_long_decision_text_is_measured_not_guessed() -> None:
    from doc_to_video_tutor.studio.pptx import _est_text_height, _est_wrapped_lines

    short = "Why THIS (not the alternative): info: recorded only"
    long = ("Why THIS (not the alternative): structure before values, because "
            "the engine checks structure first and only then compares the two "
            "candidate baselines that were captured at different times")
    assert _est_wrapped_lines(short, 12.1, 17) <= 2
    # The long one must be recognised as taller than the 0.6 in box it used to
    # be given, which is the whole defect.
    assert _est_text_height(long, 12.1, 17) > 0.6
    assert _est_text_height(long, 12.1, 17) > _est_text_height(short, 12.1, 17)


def test_bullet_rows_are_sized_from_their_text() -> None:
    """The old rule was `+0.12in if len > 90`, which a 271-char bullet outgrew."""
    from doc_to_video_tutor.studio.pptx import _est_text_height

    b = ("Plain words: run the goldens over the current code and produce a "
         "report of metric values. On a known-good day you save that report as "
         "a baseline, which is the committed JSON file every later run is "
         "compared against.")
    assert len(b) > 90
    assert _est_text_height(b, 11.95, 18) > 0.79, "one flat +0.12in is not enough"


def test_layout_audit_catches_overflow_and_overlap(tmp_path) -> None:
    """The audit must fire on a deliberately broken deck and pass a sound one."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    from doc_to_video_tutor.studio.pptx import _audit_layout

    def build(path, long_text: bool) -> None:
        prs = Presentation()
        prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        body = ("This is a deliberately long sentence that has to wrap over more "
                "than a single line inside a narrow box to prove the auditor "
                "detects the spill. " * 3) if long_text else "Short."
        tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6),
                                      Inches(0.3))
        tb.text_frame.word_wrap = True
        para = tb.text_frame.paragraphs[0]
        para.text = body
        para.font.size = Pt(18)
        prs.save(str(path))

    broken = tmp_path / "broken.pptx"
    build(broken, long_text=True)
    findings = _audit_layout(broken)
    assert any("overflows its box" in f for f in findings), findings

    sound = tmp_path / "sound.pptx"
    build(sound, long_text=False)
    assert not [f for f in _audit_layout(sound) if "overflows" in f]


# --- visual claims the narration never makes -----------------------------
#
# Found by external review of the v012 build: the badge row showed
# `0 PASS` .. `4 CONFIG ERR` while the narration mentioned only 3 and 4, and
# the JSON payload slide showed schema_version / baseline_id / path without
# saying any of them. The viewer reads three to five facts the lesson never
# explains. Worth recording that the enrichment which puts those blocks on
# screen is what created the risk.

def test_unspoken_badge_and_payload_claims_are_reported() -> None:
    from doc_to_video_tutor.studio.validate import _unspoken_visual_claims

    plan = {"scenes": [{
        "title": "Structural error classes",
        "narration": ("Exit three is a structural error and exit four is a "
                      "broken pointer, and both must stop the run."),
        "status_badges": [{"code": "0", "label": "pass"},
                          {"code": "1", "label": "fail"},
                          {"code": "3", "label": "eval err"},
                          {"code": "4", "label": "config err"}],
        "json_snippet": '{"schema_version": 1, "baseline_id": "v1.2.0"}',
    }]}
    findings = _unspoken_visual_claims(plan)
    assert len(findings) == 1
    text = findings[0]
    # Spoken codes are not flagged...
    assert "3 eval err" not in text
    assert "4 config err" not in text
    # ...but unvoiced ones and the payload keys are.
    assert "0 pass" in text and "1 fail" in text
    assert "schema_version" in text and "baseline_id" in text


def test_a_fully_spoken_scene_reports_nothing() -> None:
    from doc_to_video_tutor.studio.validate import _unspoken_visual_claims

    plan = {"scenes": [{
        "title": "Error classes",
        "narration": ("Zero means pass, one means fail, two means review, three "
                      "is an eval error, four is a config error, and the "
                      "schema_version is pinned."),
        "status_badges": [{"code": "0", "label": "pass"},
                          {"code": "1", "label": "fail"}],
        "json_snippet": '{"schema_version": 1}',
    }]}
    assert _unspoken_visual_claims(plan) == []


def test_unspoken_visual_claim_is_soft_not_blocking() -> None:
    """It must warn without failing the build: the fix is a content decision."""
    from doc_to_video_tutor.studio.config import _SOFT_PREFIXES

    assert "unspoken visual claim" in _SOFT_PREFIXES


# --- a soft finding must never block the build ----------------------------
#
# This shipped as a real incident. `unspoken visual claim` was registered in
# _SOFT_PREFIXES, but the finding was formatted as
#   "scene 7 unspoken visual claim: ..."
# and the review-before-build loop separates hard from soft with
# `problem.startswith(prefix)`. A buried prefix means False, so the finding
# counted as hard: three consecutive samples were rejected for the same
# advisory finding, each burning a full plan generation (39s, 51s, 58s), and the
# build could not complete. The model was being asked to satisfy a check it was
# never prompted about.

def test_unspoken_visual_claim_is_filtered_out_of_the_hard_problem_set() -> None:
    """The exact mechanism that broke: the sample loop's soft filter."""
    from doc_to_video_tutor.studio.cli import _SAMPLE_SOFT_EXTRA
    from doc_to_video_tutor.studio.config import _SOFT_PREFIXES
    from doc_to_video_tutor.studio.validate import _unspoken_visual_claims

    plan = {"scenes": [{
        "title": "Structural error classes",
        "narration": "Exit three is a structural error and four is a pointer.",
        "status_badges": [{"code": "0", "label": "pass"},
                          {"code": "1", "label": "fail"}],
    }]}
    findings = _unspoken_visual_claims(plan)
    assert findings, "the finding must still be reported"
    soft_prefixes = _SOFT_PREFIXES + _SAMPLE_SOFT_EXTRA
    hard = [f for f in findings
            if not any(f.startswith(s) for s in soft_prefixes)]
    assert not hard, f"advisory finding would force a resample: {hard}"


def test_soft_finding_puts_the_prefix_first_by_construction() -> None:
    from doc_to_video_tutor.studio.config import _SOFT_PREFIXES
    from doc_to_video_tutor.studio.validate import soft_finding

    message = soft_finding("scene 3 something happened")
    assert any(message.startswith(p) for p in _SOFT_PREFIXES)
    # And a non-default prefix is still honoured.
    other = soft_finding("detail", prefix="fused slide token")
    assert other.startswith("fused slide token")


def test_every_registered_soft_prefix_is_reachable_as_a_message_start() -> None:
    """Guards the contract itself, not one call site.

    `_SOFT_PREFIXES` is only meaningful if findings are *written* to lead with
    it. This cannot enumerate every call site, so it pins the failure mode that
    actually happened: a soft finding whose text does not lead with its prefix.
    """
    from doc_to_video_tutor.studio.config import _SOFT_PREFIXES
    from doc_to_video_tutor.studio.validate import soft_finding

    for prefix in _SOFT_PREFIXES:
        assert soft_finding("x", prefix=prefix).startswith(prefix)


# --- layout is derived from content, resolved by one budget --------------
#
# The user's framing, and the right one: the layout question "does this fit?"
# was being re-asked independently by six blocks, each with its own idea of what
# to sacrifice. That is the fixed-length approach - the block decides for
# itself, so the slide has no policy and a long bullet silently starves whatever
# is below it. A content-driven model lets each block declare the height its
# content needs and lets one pass decide what survives, dropping by priority
# rather than clipping.

def test_row_stack_drops_lowest_priority_first_and_never_clips() -> None:
    from doc_to_video_tutor.studio.pptx import _RowStack

    stack = _RowStack(1.7, 3.7)          # 2.0 in of room
    # The cursor accumulates in floats, so compare with a tolerance.
    assert stack.reserve(0.5, _RowStack.REQUIRED, "bullets") == pytest.approx(1.7)
    assert stack.reserve(0.6, _RowStack.SUPPORTING, "decision") == pytest.approx(2.2)
    assert stack.reserve(0.5, _RowStack.OPTIONAL, "analogy") == pytest.approx(2.8)
    # 0.4 in left: the analogy does not fit and says so rather than clipping.
    assert stack.reserve(0.4, _RowStack.OPTIONAL, "payload") is None
    # Required content that cannot fit is refused, not silently truncated.
    assert stack.reserve(1.5, _RowStack.REQUIRED, "more bullets") is None
    assert stack.y <= 3.7
    labels = [label for _p, label in stack.dropped]
    assert "payload" in labels and "more bullets" in labels
    assert stack.report()


def test_row_stack_keeps_required_content_in_preference_to_optional() -> None:
    """Priority, not draw order, decides what a crowded slide loses."""
    from doc_to_video_tutor.studio.pptx import _RowStack

    stack = _RowStack(0.0, 1.0)
    stack.reserve(0.4, _RowStack.OPTIONAL, "analogy")
    stack.reserve(0.4, _RowStack.REQUIRED, "bullet 1")
    assert stack.reserve(0.4, _RowStack.REQUIRED, "bullet 2") is None
    assert stack.reserve(0.4, _RowStack.OPTIONAL, "payload") is None
    dropped = [label for _p, label in stack.dropped]
    assert "bullet 2" in dropped and "payload" in dropped
    assert "bullet 1" not in dropped


def test_a_crowded_scene_drops_blocks_instead_of_overflowing(tmp_path) -> None:
    """End to end: a scene with more content than fits must still lay out clean."""

    from doc_to_video_tutor.studio.pptx import _audit_layout, build_pptx

    scene = {
        "section": "Crowded",
        "title": "A scene carrying far more content than one slide can hold",
        "topic": "t",
        "narration": "n" * 40,
        "source_refs": ["s"],
        "bullets": [f"Bullet number {i} carrying a deliberately long sentence "
                    f"so that the measured height exceeds the safe area and the "
                    f"layout has to resolve a real overflow. " * 2
                    for i in range(9)],
        "design_decision": "why this and not the alternative, " * 6,
        "analogy": "an analogy long enough to need its own card, " * 5,
    }
    plan = {"title": "T", "scenes": [scene], "takeaways": []}
    out = tmp_path / "crowded.pptx"
    build_pptx(plan, out)
    assert out.exists()
    # Whatever survived, nothing overflowed and nothing overlapped.
    assert [f for f in _audit_layout(out) if "overflows" in f or "overlap" in f] == []

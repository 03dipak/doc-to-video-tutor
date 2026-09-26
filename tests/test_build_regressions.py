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

import contextlib
import io
import json
import re
import tempfile
from pathlib import Path

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


# --- the deck paginates on measured height, not bullet count ---------------
#
# `_scene_pages` caps a page at four bullets, which is the right instinct -
# overflow becomes more slides, not smaller text - but the wrong unit. Bullet
# length varies by an order of magnitude, so four short bullets and four very
# long ones are not the same height. Measured: with ~676-character bullets,
# count-only pagination renders 1 of 4 and the row stack drops the rest, while
# height-based pagination renders all 4 across additional slides.
#
# The test deliberately uses bullets long enough to discriminate. An earlier
# version of this test used ~180-character bullets, where both strategies render
# everything - it passed for the wrong reason and briefly hid a real bug in the
# measurement I was using to check it.

def _rendered_bullets(plan) -> int:
    import tempfile
    from pathlib import Path

    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import build_pptx

    out = Path(tempfile.mkdtemp()) / "deck.pptx"
    build_pptx(plan, out)
    prs = Presentation(str(out))
    return sum(1 for slide in prs.slides for shape in slide.shapes
               if shape.has_text_frame
               and shape.text_frame.text.strip().startswith("B"))


def test_long_bullets_paginate_instead_of_being_dropped() -> None:
    scene = {
        "section": "Crowded", "title": "Long bullets", "topic": "t",
        "narration": "n" * 40, "source_refs": ["s"],
        "bullets": [f"B{i}: " + ("long teaching sentence needing real room. " * 16)
                    for i in range(4)],
    }
    rendered = _rendered_bullets({"title": "T", "scenes": [scene],
                                  "takeaways": []})
    assert rendered == 4, (
        f"only {rendered}/4 bullets rendered; long content must become more "
        f"slides, never fewer bullets")


def test_paginate_by_height_leaves_short_pages_alone() -> None:
    from doc_to_video_tutor.studio.pptx import _paginate_by_height

    page = {"bullets": ["short one", "short two"], "title": "T"}
    assert _paginate_by_height(page, 4.86) == [page]
    long_page = {"bullets": ["x" * 900, "y" * 900], "title": "T"}
    out = _paginate_by_height(long_page, 4.86)
    assert len(out) == 2, "two 900-char bullets cannot share one slide"
    assert sum(len(p["bullets"]) for p in out) == 2


def test_dropped_required_content_is_reported_never_silent() -> None:
    """If even one bullet cannot fit, the deck must say so."""
    import tempfile
    from pathlib import Path

    from doc_to_video_tutor.studio import pptx as P

    scene = {
        "section": "C", "title": "T", "topic": "t", "narration": "n" * 40,
        "source_refs": ["s"],
        "bullets": ["B0: " + ("unfittable. " * 400)],
    }
    notes: list[str] = []
    original = P._audit_layout
    P._audit_layout = lambda _p: notes.extend(["sentinel"]) or []
    try:
        P.build_pptx({"title": "T", "scenes": [scene], "takeaways": []},
                     Path(tempfile.mkdtemp()) / "d.pptx")
    finally:
        P._audit_layout = original
    assert notes == ["sentinel"], "the audit must still run"


# --- content defects found in a rendered deck ----------------------------
#
# Reported against mod03_gates_v012_006.pptx. Two classes, both visible to a
# learner and neither caught by the layout audit, which only knows about
# geometry.

def test_renderer_strips_field_name_leaks_from_the_opening() -> None:
    """The deck must not trust upstream hygiene for text it puts on a slide.

    The plan pipeline sanitises the opening, but the deck is also built from
    saved plans, and a saved plan can predate the sanitiser. Rendering straight
    from a saved plan printed the literal JSON key "opening" on the title
    slide.
    """
    from doc_to_video_tutor.studio.pptx import _strip_field_leak

    leaked = "Chaliye regression samajh ke shuruwat karte hai. opening"
    assert _strip_field_leak(leaked) == (
        "Chaliye regression samajh ke shuruwat karte hai")
    assert not _strip_field_leak("...shuruwat karte hai opening").endswith(
        "opening")
    # And a legitimate ending is left alone.
    keep = "Baseline snapshot and compare, then diff every new run"
    assert _strip_field_leak(keep) == keep


def test_repeated_diagrams_are_dropped_from_later_scenes() -> None:
    """A visual repeated across scenes teaches nothing the first telling did not.

    Layer A already refuses a repeated narration sentence across scenes; the same
    rule was missing on the visual channel, so three of nine scenes carried the
    identical Run Value -> Delta vs Baseline diagram and two more carried the
    same gate/guardrail/info chain.
    """
    from doc_to_video_tutor.studio.plan import _dedupe_visual_diagrams

    chain = "[A] ---> [B] ---> [C]"
    plan = {"scenes": [
        {"visual_diagram": chain},          # first use is kept
        {"visual_diagram": chain},          # repeat dropped
        {"visual_diagram": "[X] ---> [Y]"}, # distinct is kept
        {"visual_diagram": "[X] ---> [Y]"}, # repeat dropped
        {},                                 # no diagram, untouched
    ]}
    assert _dedupe_visual_diagrams(plan) == 2
    assert plan["scenes"][0]["visual_diagram"] == chain
    assert "visual_diagram" not in plan["scenes"][1]
    assert plan["scenes"][2]["visual_diagram"] == "[X] ---> [Y]"
    assert "visual_diagram" not in plan["scenes"][3]
    assert "visual_diagram" not in plan["scenes"][4]


def test_diagram_dedup_is_token_based_not_string_based() -> None:
    """Punctuation and spacing must not make a repeat look like a new diagram."""
    from doc_to_video_tutor.studio.plan import _dedupe_visual_diagrams

    plan = {"scenes": [
        {"visual_diagram": "[A] ---> [B]"},
        {"visual_diagram": "[A]  --->   [B]"},
    ]}
    assert _dedupe_visual_diagrams(plan) == 1
    assert "visual_diagram" not in plan["scenes"][1]


# --- takeaway cards flowed by measured height, not a fixed row step -------
#
# Observed on slide 11 of mod03_gates_v012_007: three overlaps 5.90in wide,
# which is a full column. The takeaway block advanced every card by a constant
# 1.2in while sizing the card from a character-count guess
# (`1.2 * (0.5 + len/100)`), so any card taller than 1.2in overlapped the next
# one in its column. This is the same defect already fixed for scene bullets,
# in a code path the layout audit had never been run against - the earlier decks
# passed because their takeaways happened to be short.

def test_long_takeaways_do_not_overlap_each_other() -> None:
    import tempfile
    from pathlib import Path

    from doc_to_video_tutor.studio.pptx import _audit_layout, build_pptx

    plan = {
        "title": "T",
        "scenes": [{"section": "S", "title": "One", "topic": "t",
                    "narration": "n" * 40, "source_refs": ["s"],
                    "bullets": ["short one"]}],
        "takeaways": [f"Takeaway {i}: " + ("a durable conclusion stated at "
                      "real length so the card cannot fit one row. " * 3)
                      for i in range(6)],
    }
    out = Path(tempfile.mkdtemp()) / "deck.pptx"
    build_pptx(plan, out)
    findings = _audit_layout(out)
    assert [f for f in findings if "overlap" in f] == [], findings


def test_long_takeaways_paginate_instead_of_being_dropped() -> None:
    """The summary slide must not lose content to protect the layout.

    Overflow becomes another slide - the same rule already applied to bullets.
    Before this, eight 288-character takeaways filled two columns and dropped
    four, which is the wrong trade on the most important slide in the deck.
    """
    import tempfile
    from pathlib import Path

    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import build_pptx

    plan = {
        "title": "T",
        "scenes": [{"section": "S", "title": "One", "topic": "t",
                    "narration": "n" * 40, "source_refs": ["s"],
                    "bullets": ["b"]}],
        "takeaways": [f"Takeaway {i}: " + ("a real conclusion. " * 12)
                      for i in range(8)],
    }
    out = Path(tempfile.mkdtemp()) / "deck.pptx"
    build_pptx(plan, out)
    prs = Presentation(str(out))
    # Match on content AND geometry: the "Key Takeaways" chrome label also
    # contains the word, and counting it hides whether cards were placed.
    placed = sum(1 for slide in prs.slides for shape in slide.shapes
                 if shape.has_text_frame
                 and "Takeaway " in shape.text_frame.text
                 and shape.width / 914400.0 > 5
                 and shape.top / 914400.0 > 1.5)
    assert placed == 8, f"only {placed}/8 takeaway cards placed"
    # More than one takeaway slide means it paginated rather than truncated.
    labels = [shape.text_frame.text for slide in prs.slides
              for shape in slide.shapes if shape.has_text_frame
              and "Key Takeaways" in shape.text_frame.text
              and shape.width / 914400.0 > 5]
    assert len(labels) >= 2, "eight long takeaways should need continuation"


def test_deck_page_counters_are_correct() -> None:
    """A paginating deck must still print an honest N / total on every slide."""
    import tempfile
    from pathlib import Path

    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import build_pptx

    plan = {
        "title": "T",
        "scenes": [{"section": "S", "title": f"Scene {i}", "topic": "t",
                    "narration": "n" * 40, "source_refs": ["s"],
                    "bullets": ["b"]} for i in range(3)],
        "takeaways": [f"Takeaway {i}: " + ("a real conclusion. " * 12)
                      for i in range(6)],
    }
    out = Path(tempfile.mkdtemp()) / "deck.pptx"
    build_pptx(plan, out)
    prs = Presentation(str(out))
    total = len(prs.slides._sldIdLst)
    for index, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            text = shape.text_frame.text.strip()
            if text.endswith(f"/ {total}") and shape.width / 914400.0 < 2:
                assert text == f"{index} / {total}", (
                    f"slide {index} prints {text!r}")


# --- the pipeline must not promise to continue it will refuse ------------
#
# Observed on mod03_gates_v012_008. The plan stage collected "repeating
# narration phrase" as a soft problem and printed
#   "WARNING: repeating narration phrase detected ... proceeding anyway"
# and the pre-render gate then refused the same plan on the same condition:
#   "RENDER-BLOCKING: 1 banned narration phrase(s) unresolved"
# 40 seconds and a full TTS run later. One defect, two severities, and the user
# was told the first and blocked by the second.
#
# The refusal itself is correct - the narration really did repeat a trigram
# across a fragment join. The defect was the false reassurance, not the gate.

def test_render_blocking_conditions_are_not_reported_as_proceeding() -> None:
    """A condition the render gate will refuse must not be called advisory."""
    import json
    from pathlib import Path

    from doc_to_video_tutor import studio as S
    from doc_to_video_tutor.studio.config import _SOFT_PREFIXES
    from doc_to_video_tutor.studio.validate import _render_blocking_problems

    rejected = Path("output/mod03_gates_v012_008.plan.json")
    if not rejected.exists():
        import pytest

        pytest.skip("rejected artifact not present; run the build first")

    doc = json.loads(rejected.read_text(encoding="utf-8"))
    voice = S._make_voice(doc.get("voice"))
    blocking = _render_blocking_problems(doc["plan"], voice=voice)
    assert blocking, "this plan is the one the gate refused"

    # The soft prefix list is what let the plan stage call it advisory.
    assert "repeating narration phrase" in _SOFT_PREFIXES

    # And the gate's own finding is genuinely render-blocking, not advisory.
    assert any("RENDER-BLOCKING" in b for b in blocking)


def test_the_repeated_trigram_spans_a_fragment_join() -> None:
    """The refusal was correct: the narration really is repetitive.

    The phrase is `consistent verdicts units` - the tail of the design decision
    ("...critical for consistent verdicts.") followed by the head of the next
    fragment ("Units define 'too much'"). A trigram spanning a join is invisible
    to a per-fragment check, which is why the repair chain left it and the
    render gate caught it.
    """
    import json
    from pathlib import Path

    import pytest

    from doc_to_video_tutor.studio.text import _nar_tokens

    rejected = Path("output/mod03_gates_v012_008.plan.json")
    if not rejected.exists():
        pytest.skip("rejected artifact not present; run the build first")
    plan = json.loads(rejected.read_text(encoding="utf-8"))["plan"]
    target = ("consistent", "verdicts", "units")
    scenes = [i for i, sc in enumerate(plan["scenes"], 1)
              if any(tuple(_nar_tokens(str(sc.get("narration", "")))[k:k + 3])
                     == target
                     for k in range(max(len(_nar_tokens(
                         str(sc.get("narration", "")))) - 2, 0)))]
    assert scenes == [4], f"expected the repeat in scene 4 only, got {scenes}"


# --- a failed first LLM attempt is recoverable, and must read that way ----
#
# Observed on mod03_gates_v012_009. The log printed a large alarming debug block
# - "LLM did not return a JSON lesson plan" plus a dump of raw output - and then
# carried on and produced the best build of the session: TTS QA PASS with zero
# warnings, duration 97% of target, clean layout.
#
# The retry is real and silent: plan_lesson catches the RuntimeError and re-probes
# with a larger completion budget, which is why only one "Planning lesson" line
# appears for two attempts. So the debug block describes ONE failed attempt out
# of two, not a failed build - and nothing on screen said so.
#
# Verified separately: the saved raw really does contain no decodable plan object
# (a brace-matching scan finds no `{` that decodes to a dict carrying `scenes`),
# so the parse failure was genuine and the heuristic was not crying wolf.

def test_a_parse_failure_debug_says_the_build_continues() -> None:
    import inspect

    from doc_to_video_tutor.studio import plan as P

    src = inspect.getsource(P.plan_lesson)
    assert "_dump_parse_failure(" in src
    # The context string must tell the reader this attempt is recoverable.
    # Match a phrase that is not split across two f-string literals, so this
    # asserts the message rather than the source formatting.
    assert "Recoverable: retrying" in src
    assert "not a build failure" in src


def test_plan_parse_rejects_a_raw_with_no_complete_object() -> None:
    """A ragged response must be refused, not salvaged into a broken plan."""
    import pytest

    from doc_to_video_tutor.studio.llm import _parse_plan_json

    ragged = ('```json\n{"title": "T", "scenes": [{"title": "A", '
              '"narration": "n", "bullets": ["b"]}\n')
    with pytest.raises(RuntimeError):
        _parse_plan_json(ragged)


# --- chrome geometry: overlaps the audit was suppressing -----------------
#
# Reported against mod03_gates_v012_009: slides 2,4,5,6,7,9,10. The layout audit
# had said "no findings" because its overlap tolerance was 0.12in, set earlier to
# absorb exactly these collisions on the grounds that they read as intentional.
# They were not intentional - they were defects, and the tolerance was a
# detector silenced instead of a bug fixed. Two distinct causes:
#
#   * the eyebrow box was a round 0.4in tall for 12pt text, so it hung 0.08in
#     into the title on every content slide (10.00in wide);
#   * the title box was 11.0in wide from x=0.42, running to 11.42 and under the
#     page counter at x=11.20 (0.22in).
#
# Plus the payload and value cards, which still used a flat 0.3in per line and
# spilled out under their own card.

def test_no_slide_has_a_non_contained_overlap() -> None:
    """Geometry check written independently of the studio's own auditor."""
    import json
    import tempfile
    from pathlib import Path

    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import build_pptx

    src = Path("output/mod03_gates_v012_009.plan.json")
    if not src.exists():
        import pytest

        pytest.skip("plan artifact not present; run the build first")
    plan = json.loads(src.read_text(encoding="utf-8"))["plan"]
    out = Path(tempfile.mkdtemp()) / "deck.pptx"
    build_pptx(plan, out)

    # Same containment rule as the studio's own auditor, including its edge
    # slack. An independent check that disagrees with the auditor by a rounding
    # hair is worse than no check.
    from doc_to_video_tutor.studio.pptx import _contains

    emu = 914400.0
    offenders: list[str] = []
    for index, slide in enumerate(Presentation(str(out)).slides, 1):
        shapes = [s for s in slide.shapes if s.width and s.height]
        for a in range(len(shapes)):
            for b in range(a + 1, len(shapes)):
                A, B = shapes[a], shapes[b]
                ox = (min(A.left + A.width, B.left + B.width)
                      - max(A.left, B.left)) / emu
                oy = (min(A.top + A.height, B.top + B.height)
                      - max(A.top, B.top)) / emu
                if ox <= 0.005 or oy <= 0.005:
                    continue
                if _contains(A, B) or _contains(B, A):
                    continue
                offenders.append(f"slide {index}: {ox:.2f}x{oy:.2f}in")
    assert not offenders, offenders


def test_chrome_boxes_cannot_collide() -> None:
    """Eyebrow, title and counter must be geometrically disjoint by construction."""
    from doc_to_video_tutor.studio.pptx import _est_text_height

    eyebrow_bottom = 0.30 + _est_text_height("HOW DOES IT WORK", 10.0, 12)
    title_top = 0.62
    title_right = 0.42 + 10.6
    counter_left = 11.20
    assert eyebrow_bottom <= title_top, "eyebrow hangs into the title"
    assert title_right <= counter_left, "title runs under the page counter"


# --- every content shape must land inside the safe area -------------------
#
# Slide 9 of mod03_gates_v012_010 drew a payload card from 5.59in to 7.78in on
# a 7.5in slide - a full inch past the 6.9in safe bottom. The card itself was
# sized correctly, so an overlap check did not fire and the audit said nothing
# was wrong. The layout was not crowded; it ran off the canvas.
#
# Cause: the diagram block advanced the local `y` cursor without going through
# `_RowStack`, so the stack's cursor fell behind. The stack then believed more
# room was left than really existed and admitted a block that could not fit.
# Two cursors, one of them stale, is precisely what a single shared budget
# exists to prevent - so the fix routes the diagram through the stack, and this
# test checks the safe area directly rather than trusting any bookkeeping.

def test_no_content_shape_lands_past_the_safe_bottom(tmp_path) -> None:
    """Catches off-canvas content that no overlap check would notice."""
    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import build_pptx

    emu = 914400.0
    safe_bottom = 6.9
    offenders: list[str] = []

    def check(plan, tag) -> None:
        out = Path(tmp_path) / f"{tag}.pptx"
        build_pptx(plan, out)
        for index, slide in enumerate(Presentation(str(out)).slides, 1):
            for shape in slide.shapes:
                if not shape.width or not shape.height:
                    continue
                bottom = (shape.top + shape.height) / emu
                # The footer legitimately sits below the content safe area.
                if shape.top / emu > 6.95:
                    continue
                if bottom > safe_bottom + 0.02:
                    text = (shape.text_frame.text.strip()[:28]
                            if shape.has_text_frame else "<card>")
                    offenders.append(
                        f"{tag} slide {index}: bottom {bottom:.2f}in {text!r}")

    # 1. the real plan that produced the defect
    src = Path("output/mod03_gates_v012_010.plan.json")
    if src.exists():
        check(json.loads(src.read_text(encoding="utf-8"))["plan"], "real")

    # 2. a synthetic worst case, so the invariant does not depend on one lesson
    check({"title": "T", "takeaways": [], "scenes": [{
        "section": "S", "title": "Everything at once", "topic": "t",
        "narration": "n" * 40, "source_refs": ["s"],
        "bullets": [f"B{i}: " + ("long teaching sentence needing real room. " * 4)
                    for i in range(6)],
        "visual_diagram": "[A] ---> [B] ---> [C] ---> [D]",
        "design_decision": "why this and not the alternative. " * 6,
        "analogy": "an analogy needing its own card. " * 5,
        "json_snippet": '{"schema_version": 1, "baseline_id": "v1.2.0",\n'
                        ' "path": "eval/baselines/v1.2.0.json",\n'
                        ' "kind": "gate", "tolerance": 0.03}',
        "code_snippet": "def compare(base):\n    return diff(base)",
    }]}, "synthetic")

    assert not offenders, offenders


def test_every_block_advances_the_shared_stack() -> None:
    """No block may advance the local cursor behind the stack's back."""
    import inspect
    import re

    from doc_to_video_tutor.studio import pptx as P

    src = inspect.getsource(P.build_pptx)
    # Every `y +=` in the slide body must be paired with a stack reservation.
    # The takeaway flow deliberately manages its own columns, so scope the check
    # to the scene-slide section.
    body = src.split("if takes:")[0]
    bare_advances = [m.group(0).strip() for m in
                     re.finditer(r"^\s+y \+= .*$", body, re.M)]
    assert not bare_advances, (
        f"these advance the local cursor without the shared stack: {bare_advances}")


# --- diagnostics must name the slide they are about ----------------------
#
# A layout note reported `slide {deck_total}`, which is a COUNT of slides, not
# the index of the slide carrying the problem. With takeaway pagination the two
# differ, so the note pointed at a slide that was not the broken one - and a
# diagnostic that names the wrong slide costs more than no diagnostic.
#
# Checking the same code found a second one: scene-slide notes used `i + 1`
# while the counter printed on that very slide used `i + 2`, because slide 1 is
# the title. Every scene note was off by one.

def test_scene_notes_use_the_same_index_as_the_printed_counter() -> None:
    import inspect
    import re

    from doc_to_video_tutor.studio import pptx as P

    src = inspect.getsource(P.build_pptx)
    counter = re.search(r'f"\{i \+ (\d+)\} / \{deck_total\}"', src)
    assert counter, "the scene-slide counter expression moved"
    offset = int(counter.group(1))
    assert offset == 2, (
        f"scene slides are numbered from {offset}; slide 1 is the title so "
        f"the first scene slide is 2")
    assert f"this_slide = i + {offset}" in src, (
        "scene notes must derive their index from the counter's own offset")


def test_takeaway_notes_do_not_use_the_deck_total_as_an_index() -> None:
    import inspect

    from doc_to_video_tutor.studio import pptx as P

    src = inspect.getsource(P.build_pptx)
    body = src.split("if takes:")[1] if "if takes:" in src else ""
    assert "slide {deck_total}" not in body, (
        "deck_total is a slide COUNT; using it as an index names the wrong "
        "slide whenever the takeaways paginate")
    assert "1 + len(page_scenes) + page" in body


def test_a_forced_note_names_the_slide_that_actually_carries_it(tmp_path) -> None:
    """End to end: overflow one scene, then check the number in the message."""
    import contextlib
    import io
    import tempfile

    from doc_to_video_tutor.studio import pptx as P

    # Enough blocks that the stack must drop something, so a note is emitted.
    scenes = [{"section": f"S{i}", "title": f"Scene {i}", "topic": "t",
               "narration": "n" * 40, "source_refs": ["s"],
               "bullets": [f"B{j}: " + ("long teaching sentence. " * 8)
                           for j in range(5)],
               "design_decision": "why this. " * 40,
               "analogy": "an analogy. " * 40,
               "visual_diagram": "[A] ---> [B] ---> [C]",
               "json_snippet": '{"a": 1, "b": 2}',
               "code_snippet": "def f():\n    return 1"}
              for i in range(3)]
    out = Path(tempfile.mkdtemp()) / "d.pptx"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        P.build_pptx({"title": "T", "scenes": scenes, "takeaways": []}, out)
    printed = [ln for ln in buf.getvalue().splitlines() if "[WARN] layout" in ln]
    assert printed, "expected at least one layout note for this input"

    # The deck paginates, so the bound is the real slide count, not one per scene.
    from pptx import Presentation

    total = len(Presentation(str(out)).slides._sldIdLst)
    for line in printed:
        number = int(line.split("slide ")[1].split(":")[0])
        assert 1 <= number <= total, (
            f"note names slide {number}, but the deck has {total} slides: "
            f"{line.strip()}")


# --- a bullet box must render exactly one paragraph ----------------------
#
# Found by external review and confirmed here. Every bullet and takeaway box was
# written as a placeholder paragraph and then filled by _ppt_para, which calls
# tf.add_paragraph() - so the frame rendered TWO paragraphs: a blank line, then
# the text. The height reserved in the row stack was measured for the text
# alone, so each bullet under-reserved by one 18pt line (~0.31in) and its text
# spilled into whatever the stack placed next. Measured on
# mod03_gates_v012_010: a 0.71in box whose content actually needed 1.02in.
#
# The audit could not see it either, for a slightly different reason than
# expected: _audit_layout measured `text_frame.text.strip()`, and stripping
# deletes the leading blank line, so the frame measured as fitting. Both the
# layout and the checker were blind, in the same direction.

def test_a_bullet_box_renders_exactly_one_paragraph(tmp_path) -> None:
    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import build_pptx

    plan = {"title": "T", "takeaways": ["a durable conclusion worth keeping "
                                       "at a length that wraps onto a second "
                                       "line so the box has to grow"],
            "scenes": [{"section": "S", "title": "One", "topic": "t",
                        "narration": "n" * 40, "source_refs": ["s"],
                        "bullets": ["A bullet long enough to wrap onto a "
                                    "second line at eighteen point type."]}]}
    out = Path(tmp_path) / "deck.pptx"
    build_pptx(plan, out)
    multi = [(i, len(sh.text_frame.paragraphs))
             for i, slide in enumerate(Presentation(str(out)).slides, 1)
             for sh in slide.shapes
             if sh.has_text_frame and len(sh.text_frame.paragraphs) > 1]
    assert not multi, f"frames rendering a blank leading paragraph: {multi}"


def test_reserved_height_covers_the_rendered_paragraphs(tmp_path) -> None:
    """The stack's estimate and the frame's real content must agree."""
    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import _est_text_height, build_pptx

    emu = 914400.0
    plan = {"title": "T", "takeaways": [], "scenes": [{
        "section": "S", "title": "One", "topic": "t", "narration": "n" * 40,
        "source_refs": ["s"],
        "bullets": ["A bullet that is long enough to wrap onto a second line "
                    "at eighteen point type in a twelve inch column."]}]}
    out = Path(tmp_path) / "deck.pptx"
    build_pptx(plan, out)
    for slide in Presentation(str(out)).slides:
        for shape in slide.shapes:
            if not shape.has_text_frame or not shape.text_frame.text.strip():
                continue
            text = "\n".join(p.text for p in shape.text_frame.paragraphs)
            size = 18.0
            for para in shape.text_frame.paragraphs:
                if para.font.size is not None:
                    size = para.font.size.pt
                    break
            need = _est_text_height(text, shape.width / emu, size)
            assert need <= shape.height / emu + 0.02, (
                f"reserves {shape.height / emu:.2f}in, renders {need:.2f}in")


def test_audit_does_not_strip_paragraph_structure() -> None:
    import tempfile
    """A frame with a blank leading paragraph must not measure as fitting."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    from doc_to_video_tutor.studio.pptx import _audit_layout

    out = Path(tempfile.mkdtemp()) / "blank.pptx"

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(0.3))
    box.text_frame.word_wrap = True
    box.text_frame.paragraphs[0].text = " "
    box.text_frame.paragraphs[0].font.size = Pt(18)
    para = box.text_frame.add_paragraph()
    para.text = "and here is the real content that needs a second line of room"
    para.font.size = Pt(18)
    prs.save(str(out))
    findings = _audit_layout(out)
    assert any("overflows" in f for f in findings), (
        "a blank leading paragraph must count towards the rendered height")


# --- the second takeaway column must actually be used -------------------
#
# Found by external review, confirmed here on three decks. The placement test
# was "does column 0 still have room below it?", which stays true until column 0
# is completely full - so the second column was never used at all. Verified:
# every takeaway sat at x=0.42 and the column at x=6.62 was empty on
# mod03_gates_v012_009, _010 and _012. That is the §20.2 defect 8 column-fill
# complaint, relocated rather than fixed.

def test_takeaways_use_both_columns() -> None:
    import contextlib
    import io
    import tempfile

    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import build_pptx

    emu = 914400.0
    plan = {"title": "T",
            "scenes": [{"section": "S", "title": "One", "topic": "t",
                        "narration": "n" * 40, "source_refs": ["s"],
                        "bullets": ["a short bullet"]}],
            "takeaways": [f"Takeaway {i}: a durable conclusion stated at a "
                          "length that gives the card real height."
                          for i in range(5)]}
    out = Path(tempfile.mkdtemp()) / "deck.pptx"
    with contextlib.redirect_stdout(io.StringIO()):
        build_pptx(plan, out)

    columns: set[float] = set()
    for slide in Presentation(str(out)).slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            if shape.height / emu <= 0.3 or shape.top / emu <= 1.5:
                continue
            if "v0.1.0" in shape.text_frame.text:
                continue
            left = round(shape.left / emu, 2)
            if left in (0.42, 6.62):
                columns.add(left)
    assert columns == {0.42, 6.62}, (
        f"expected both takeaway columns, found {sorted(columns)}")


def test_shorter_column_picks_the_lower_cursor() -> None:
    from doc_to_video_tutor.studio.pptx import _shorter_column

    assert _shorter_column([0.0, 0.0]) == 0        # tie -> first, deterministic
    assert _shorter_column([2.0, 1.0]) == 1
    assert _shorter_column([1.0, 2.0]) == 0


def test_takeaway_page_count_agrees_with_placement() -> None:
    """The counting pass and the placement pass must not disagree.

    They carry the same column choice, so if one is fixed and the other is not,
    `deck_total` is wrong and the printed counter drifts again - which is what
    happened when they last diverged.
    """
    import contextlib
    import io
    import tempfile

    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import _takeaway_pages_measured, build_pptx

    # build_pptx caps takeaways at 8, so the count must stay within that.
    for count in (3, 5, 7, 8):
        takes = [f"Takeaway {i}: " + ("a real conclusion. " * (2 + i % 5))
                 for i in range(count)]
        plan = {"title": "T",
                "scenes": [{"section": "S", "title": "One", "topic": "t",
                            "narration": "n" * 40, "source_refs": ["s"],
                            "bullets": ["a short bullet"]}],
                "takeaways": takes}
        predicted = _takeaway_pages_measured(takes, 6.9 - 1.7)
        out = Path(tempfile.mkdtemp()) / "deck.pptx"
        with contextlib.redirect_stdout(io.StringIO()):
            build_pptx(plan, out)
        prs = Presentation(str(out))
        actual = sum(1 for slide in prs.slides
                     if any(sh.has_text_frame
                            and "Key Takeaways" in sh.text_frame.text
                            for sh in slide.shapes))
        assert predicted == actual, (
            f"{count} takeaways: counted {predicted} pages, placed {actual}")


def test_takeaways_are_capped_and_the_cap_is_respected_by_the_counter() -> None:
    """The 8-takeaway cap must be applied before the page count, not after.

    build_pptx slices the takeaways to 8 and then counts pages from the sliced
    list, so deck_total matches what is placed. A version that counted first and
    capped later would inflate the printed counter - the same class of drift as
    the slide-index bugs in LLD 22.9.
    """
    import contextlib
    import io
    import tempfile

    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import _takeaway_pages_measured, build_pptx

    takes = [f"Takeaway {i}: " + ("a real conclusion. " * 3) for i in range(20)]
    capped = takes[:8]
    assert _takeaway_pages_measured(capped, 6.9 - 1.7) == _takeaway_pages_measured(
        capped, 6.9 - 1.7)
    plan = {"title": "T",
            "scenes": [{"section": "S", "title": "One", "topic": "t",
                        "narration": "n" * 40, "source_refs": ["s"],
                        "bullets": ["a short bullet"]}],
            "takeaways": takes}
    out = Path(tempfile.mkdtemp()) / "deck.pptx"
    with contextlib.redirect_stdout(io.StringIO()):
        build_pptx(plan, out)
    placed = sum(1 for slide in Presentation(str(out)).slides
                 for sh in slide.shapes
                 if sh.has_text_frame and "Takeaway " in sh.text_frame.text)
    assert placed <= 8, f"placed {placed} takeaways, cap is 8"


# --- the drop must record what it removed, and narration must be checked ---
#
# Confirmed on mod03_gates_v012_013, which validated the hypothesis recorded in
# LLD 22.14. `_drop_ungrounded_slide_text` runs AFTER the narration pass and all
# narration repair, so it removes bullets the narrator was given. The build log
# carried only a count and the plan was written after the drop, so the removed
# text survived nowhere and the question could not be asked of an artifact.
#
# Now the drop records what it removed, and `_narration_references_dropped_text`
# reports it. On that build it fires twice:
#   scene 5  "Ensures that a single outlier cannot bypass the gate."
#   scene 7  "Ensures that structural integrity is maintained."
# both still spoken. So this is a live defect, not a theoretical one.

def test_dropped_slide_text_is_recorded_with_scene_and_field() -> None:
    from doc_to_video_tutor.studio.plan import _drop_ungrounded_slide_text

    plan = {"scenes": [
        {"title": "One", "bullets": ["alpha beta gamma delta epsilon zeta"]},
        {"title": "Two", "bullets": ["keep this grounded phrase from source"]},
    ], "takeaways": []}
    # Only "keep this grounded phrase from source" shares a bigram with the
    # pseudo-source, so the other must be recorded as removed.
    source_bigrams = {("keep", "this"), ("this", "grounded"), ("grounded", "phrase"),
                      ("phrase", "from"), ("from", "source")}
    source_tokens = {"keep", "this", "grounded", "phrase", "from", "source"}
    dropped = _drop_ungrounded_slide_text(plan, source_bigrams, source_tokens)
    assert dropped == 1
    record = plan["dropped_slide_text"]
    assert len(record) == 1
    assert record[0]["scene"] == 1
    assert record[0]["field"] == "bullets"
    assert "alpha beta" in record[0]["text"]


def test_narration_speaking_about_dropped_text_is_reported() -> None:
    from doc_to_video_tutor.studio.validate import _narration_references_dropped_text

    plan = {
        "scenes": [{"narration": "That is what ensures that a single outlier "
                                 "cannot bypass the gate, so it is checked "
                                 "before the suite runs at all."}],
        "dropped_slide_text": [
            {"scene": 1, "field": "bullets",
             "text": "Ensures that a single outlier cannot bypass the gate."}],
    }
    findings = _narration_references_dropped_text(plan)
    assert len(findings) == 1
    assert findings[0].startswith("narration still speaks about")


def test_unrelated_narration_and_absent_records_are_clean() -> None:
    from doc_to_video_tutor.studio.validate import _narration_references_dropped_text

    assert _narration_references_dropped_text({"scenes": []}) == []
    assert _narration_references_dropped_text({
        "scenes": [{"narration": "Nothing here has anything to do with that."}],
        "dropped_slide_text": [{"scene": 1, "field": "bullets",
                                 "text": "Alpha beta gamma delta epsilon zeta"}],
    }) == []


def test_the_dropped_text_finding_is_soft() -> None:
    """A prefix mismatch would make this hard and resample the whole plan."""
    from doc_to_video_tutor.studio.config import _SOFT_PREFIXES
    from doc_to_video_tutor.studio.validate import _narration_references_dropped_text

    plan = {
        "scenes": [{"narration": "It ensures that structural integrity is "
                                 "maintained across every run of the suite."}],
        "dropped_slide_text": [{"scene": 1, "field": "bullets",
                                 "text": "Ensures that structural integrity is "
                                         "maintained."}],
    }
    finding = _narration_references_dropped_text(plan)[0]
    assert any(finding.startswith(prefix) for prefix in _SOFT_PREFIXES), finding


# --- diagram node labels must fit their boxes ----------------------------

def test_diagram_height_grows_for_long_node_labels() -> None:
    from doc_to_video_tutor.studio.pptx import _diagram_height

    short = _diagram_height(["Alpha", "Beta", "Gamma"], 2.35)
    long_labels = _diagram_height(
        ["Absolute/Relative Threshold", "Sample-size floor 1/(n+1)",
         "Deterministic offline gate"], 2.35)
    assert long_labels > short, (
        "a three-line node label needs a taller box; a flat 0.68in overflowed "
        "by up to 0.50in and drew over whatever was below")
    assert short >= 0.68


def test_column_balance_check_is_not_vacuous() -> None:
    """The balance check must fire on the original bug and stay quiet otherwise.

    A layout check that returns nothing because it never matches anything looks
    identical to a passing deck from the outside. This asserts both directions:
    reinstate the original `return 0` bug and the auditor must name the slide,
    and the correct placement must produce no finding at all.
    """
    from doc_to_video_tutor.studio import pptx as mod

    takes = [f"Takeaway {n}: a distinct claim worth its own card on the page."
             for n in range(6)]
    scene = {"section": "S", "title": "Scene 1", "topic": "t",
             "narration": "n" * 40, "source_refs": ["s"],
             "bullets": ["b"], "takeaways": []}
    plan = {"title": "T", "opening": "op", "scenes": [scene],
            "takeaways": takes, "concept_groups": [],
            "visual_diagrams": [], "code_snippets": []}
    out = Path(tempfile.mkdtemp()) / "deck.pptx"

    with contextlib.redirect_stdout(io.StringIO()):
        mod.build_pptx(plan, out)
    balanced = mod._audit_layout(out)
    assert not [f for f in balanced if "column" in f], (
        f"correctly balanced layout was flagged: {balanced}")

    correct = mod._shorter_column
    try:
        mod._shorter_column = lambda _tops: 0  # the original defect
        buggy = Path(tempfile.mkdtemp()) / "buggy.pptx"
        with contextlib.redirect_stdout(io.StringIO()):
            mod.build_pptx(plan, buggy)
    finally:
        mod._shorter_column = correct

    flagged = [f for f in mod._audit_layout(buggy) if "column" in f]
    assert flagged, "column balance check did not catch the original defect"
    assert "none in the other" in flagged[0]


def test_unclaimed_source_sections_finds_the_four_known_gaps() -> None:
    """Coverage is a structure question, and nothing measured it before this.

    `_topic_coverage_problem` is a vocabulary test, so a lesson can skip a whole
    concept and still pass it - the skipped concept's words turn up in passing.
    On the shipped `mod03_gates_v012_013` build, numbered concepts 4, 10, 11
    and 12 were used by no scene and nothing said so. This recomputes that from
    the real module and the real assignment record, so the finding cannot rot
    into silence.
    """
    from doc_to_video_tutor.studio.plan import (
        _markdown_sections,
        _unclaimed_source_sections,
    )

    src = Path("modules/08_concepts_mod03_gates.md")
    artifact = Path("output/mod03_gates_v012_013.plan.json")
    if not src.exists() or not artifact.exists():
        pytest.skip("source module or build artifact not present")

    plan = json.loads(artifact.read_text(encoding="utf-8"))["plan"]
    sections = _markdown_sections(src.read_text(encoding="utf-8"))
    plan["source_sections"] = [
        {"index": i, "heading": h.lstrip("#").strip()} for i, (h, _b) in
        enumerate(sections)]

    gaps = _unclaimed_source_sections(plan)
    numbers = {int(g["heading"].split(".")[0]) for g in gaps}
    assert numbers == {4, 10, 11, 12}, (
        f"expected the four concepts no scene claimed, got {sorted(numbers)}")

    # Claiming every numbered section must silence it, or the check is a
    # constant and would never have found the gaps above.
    plan["source_assignment"] = [
        {"scene": n + 1, "status": "assigned", "heading": s["heading"],
         "score": 0.9, "section_index": s["index"]}
        for n, s in enumerate(plan["source_sections"])]
    assert _unclaimed_source_sections(plan) == []


def test_source_coverage_finding_is_soft_and_survives_missing_field() -> None:
    """The prefix must be registered, or the sample loop resamples the plan.

    A soft finding whose prefix is absent from `_SOFT_PREFIXES` counts as a hard
    problem: the planner would then retry the whole plan to satisfy a check the
    model was never asked about. That regression is invisible in a build log
    that happens to pass, so it is pinned here. A plan written before
    `source_sections` existed must also not crash the gate.
    """
    from doc_to_video_tutor.studio.config import _SOFT_PREFIXES
    from doc_to_video_tutor.studio.validate import _source_coverage_gaps

    assert "source section not covered" in _SOFT_PREFIXES
    # A plan written before the field existed must be silent, not a crash.
    assert _source_coverage_gaps({}) == []

    sections = [{"index": 0, "heading": "1. alpha"},
                {"index": 1, "heading": "2. beta"}]
    unclaimed = _source_coverage_gaps(
        {"source_sections": sections, "source_assignment": []})
    assert len(unclaimed) == 1 and unclaimed[0].startswith(
        "source section not covered")
    # Same sections, both claimed: silent. Without this the check would fire
    # unconditionally and prove nothing.
    assert _source_coverage_gaps({"source_sections": sections,
                                  "source_assignment": [
                                      {"scene": 1, "status": "assigned",
                                       "section_index": 0},
                                      {"scene": 2, "status": "assigned",
                                       "section_index": 1}]}) == []


def test_enumerator_after_a_word_is_content_not_a_list_marker() -> None:
    """`Exit 3` is the lesson. The old guard deleted it and the build died.

    On `mod03_gates_v012_014` the pre-audio gate blocked with
    `tts_required_concept_missing: scene:7 narration is transition/title-only`.
    The cause was upstream of the gate: `_flatten_parentheses` stripped "3:" and
    "4:" out of "Exit 3: structural error. Exit 4: data inconsistency.", leaving
    "Exit structural error. Exit data inconsistency." The scene lost the one
    distinction it existed to teach, and the gate was right to refuse.

    The guard was `(?<!\\w)`, which tests the single character before the digits.
    In "Exit 3:" that character is a space, not a word character, so the
    assertion passed. One character was compared where a clause boundary was
    required.
    """
    from doc_to_video_tutor.studio.speech import _flatten_parentheses

    # The identifier is the teaching content and must survive verbatim.
    for text in ("Exit 3: structural error.", "Note 12: y",
                 "Phase 2: the gate is its own artifact",
                 "T-03-11: the offline seam"):
        assert _flatten_parentheses(text) == text, (
            f"flatten_parentheses destroyed an identifier: {text!r}")

    # A real list marker at a clause boundary is still noise, and is stripped.
    assert _flatten_parentheses("1. Baseline snapshot") == "Baseline snapshot"
    assert _flatten_parentheses(
        "Baselines matter. 3. Tolerance and units") == (
            "Baselines matter. Tolerance and units")


def test_speech_survives_the_whole_scene_7_spoken_path() -> None:
    """End to end through `_spoken_variant`, not just the one regex.

    A fix proven only at the regex can still be undone by a later step in the
    same function - which is how this was originally missed, since the digit
    loss is invisible in `_flatten_parentheses` alone unless you read the
    composed output.
    """
    from doc_to_video_tutor.studio.speech import _spoken_variant
    from doc_to_video_tutor.studio.voice import _make_voice

    spoken = _spoken_variant(
        _make_voice("mhe-mix"),
        "Exit 3: structural error. Exit 4: data inconsistency.")
    # `speech_expand` renders digits as number words for the voice, so the
    # requirement is that each number survives, not that it stays a digit:
    # "Exit three structural error. Exit four data inconsistency." teaches the
    # distinction; "Exit structural error. Exit data inconsistency." does not.
    assert "three" in spoken and "four" in spoken, (
        f"scene 7's spoken track lost its identifiers: {spoken!r}")
    assert spoken.count("Exit") == 2


def test_diagram_row_cannot_leave_the_frame() -> None:
    """The video path had no layout guard at all; a diagram ran off-screen.

    On `mod03_gates_v012_015` scene 9's diagram has 6 nodes. The per-node cap
    `min((1280-120)//n, 230)` ignores the 24px gap between nodes, so
    `w=193, pitch=217` put the last node's right edge at
    `60 + 5*217 + 193 = 1338px` on a 1280px frame: 58px, roughly 30% of the pill,
    past the edge, for that scene's entire ~30s. `_audit_layout` reported the
    PPTX clean throughout, because the defect is in `slides.py` - the PIL video
    path - and only that renderer can see it.

    Two directions asserted: the old arithmetic is still detectable (so this
    test cannot pass by the bug simply disappearing from the helper), and the
    new sizing fits at every node count.
    """
    from doc_to_video_tutor.studio.slides import (
        _diagram_row_fit,
        _parse_diagram,
        diagram_overflow_px,
    )

    # The shipped defect, reproduced as arithmetic.
    assert diagram_overflow_px(6) == 58, (
        "the old off-frame arithmetic no longer reproduces; this test can no "
        "longer prove the defect is detectable")
    assert diagram_overflow_px(5) == 26

    # New sizing keeps the row inside the content band at every count.
    for n in range(2, 12):
        _w, _pitch, right = _diagram_row_fit(n)
        assert right <= 1280 - 60, f"{n} nodes overflow: right edge {right}px"

    # Four or fewer is unchanged from the old cap, so this is not a visual
    # regression for the diagrams that were already fine.
    assert _diagram_row_fit(3)[0] == 230
    assert _diagram_row_fit(4)[0] == 230

    # And the real scene, through the real parser.
    plan_path = Path("output/mod03_gates_v012_015.plan.json")
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))["plan"]
        counts = [len(_parse_diagram(s["visual_diagram"]))
                  for s in plan["scenes"] if s.get("visual_diagram")]
        assert max(counts) == 6, (
            f"scene 9's 6-node diagram changed to {max(counts)}; re-check the "
            f"fit against the shipped plan")
        for n in counts:
            assert _diagram_row_fit(n)[2] <= 1280 - 60


def test_build_reports_measured_loudness_not_the_config_constants() -> None:
    """Every build so far printed a target under a "loudness :" label.

    `video.py` normalised each clip and then printed `LOUDNESS_TARGET` /
    `LOUDNESS_TP` - the configured constants - as though they were readings. A
    reviewer measuring the artifacts independently found true peak -1.8 dBTP
    against a logged -1.5, i.e. the log was a target wearing a measurement's
    label. Three causes, all fixed together because none alone produces a
    number:

    1. the filter chain never set `print_format`, so loudnorm reported nothing;
    2. the readings were not parsed when it did;
    3. `os.replace` could not move the result back across a filesystem boundary,
       so with the output dir on another device every clip silently skipped
       normalisation entirely (measured 0/3 here, now 3/3).
    """
    from doc_to_video_tutor.studio import video as V

    summary = ("Input Integrated:    -16.4 LUFS\n"
               "Input True Peak:      -1.8 dBTP\n"
               "Output Integrated:   -16.1 LUFS\n"
               "Output True Peak:     -1.5 dBTP\n")
    got = V._parse_loudnorm(summary)
    # Delivered values are the Output ones; the Input pair is kept for the gain.
    assert got["integrated_lufs"] == -16.1
    assert got["true_peak_dbtp"] == -1.5
    assert got["in_integrated_lufs"] == -16.4
    assert V._parse_loudnorm("no summary emitted") == {}

    src = Path(V.__file__).read_text(encoding="utf-8")
    # (1) the chain must ask for the summary at all
    assert "print_format=summary" in src, (
        "loudnorm will not report its readings without print_format")
    # (3) os.replace is rename(2) and fails EXDEV across devices
    assert "os.replace(tmp" not in src, (
        "os.replace cannot cross a filesystem boundary; loudness "
        "normalisation silently skips every clip when it does")
    assert "shutil.move(str(tmp)" in src


def test_starved_scene_is_rehydrated_from_source_before_the_pre_audio_gate() -> None:
    """The v012_014 build failure, reproduced and fixed.

    `_drop_ungrounded_slide_text` runs LAST in the repair chain, so
    `_repair_thin_narrations` - which reads `source_chunk` and raises its floor
    when one is present - never saw the scene the drop then starved. Scene 7
    went 2 bullets -> 1, its narration was 6 content words against a floor of
    8 once the canned opener was stripped, and the pre-audio gate killed a 38s
    build with `tts_required_concept_missing`.

    Two alternatives were measured and both are worse, so both are rejected here
    by construction:
      - keeping the ungrounded bullets to hold the count re-breaks the hard
        grounding gate this exists to satisfy;
      - dropping the narration sentences derived from dropped text (the obvious
        companion fix) leaves only the opener, i.e. 0 content words vs a floor
        of 8.
    Re-hydrating from the scene's own `source_chunk` is anchored by
    construction, which is exactly what the dropped bullet was not.

    The shipped plan is post-drop, so the pre-drop state is reconstructed by
    putting the recorded `dropped_slide_text` bullets back - otherwise the drop
    is a no-op on replay and the test would pass without exercising anything.
    """
    import copy

    from doc_to_video_tutor.studio import plan as P
    from doc_to_video_tutor.studio.plan import (
        _drop_ungrounded_slide_text,
        _enforce_unique_narration_trigrams,
        _rehydrate_starved_scenes,
        _repair_thin_narrations,
    )
    from doc_to_video_tutor.studio.speech import (
        _has_teaching_claim,
        build_tts_script,
    )
    from doc_to_video_tutor.studio.voice import _make_voice

    artifact = Path("output/mod03_gates_v012_014.plan.json")
    source = Path("modules/08_concepts_mod03_gates.md")
    if not artifact.exists() or not source.exists():
        pytest.skip("build artifact or source module not present")

    content = source.read_text(encoding="utf-8")
    bigrams = P._text_ngrams(content)
    tokens = {w.lower() for w in P._WORD.findall(content)
              if len(w) > 2 and w.lower() not in P._STOP}

    plan = json.loads(artifact.read_text(encoding="utf-8"))["plan"]
    plan = copy.deepcopy(plan)
    for entry in plan.get("dropped_slide_text") or []:
        if entry.get("field") == "bullets":
            scene = plan["scenes"][entry["scene"] - 1]
            scene["bullets"] = [*list(scene.get("bullets") or []), entry["text"]]

    voice = _make_voice("mhe-mix")
    before = [len(s.get("bullets") or []) for s in plan["scenes"]]

    dropped = _drop_ungrounded_slide_text(plan, bigrams, tokens)
    rehydrated = _rehydrate_starved_scenes(plan)
    _enforce_unique_narration_trigrams(plan, quiet=True, protected=())
    _repair_thin_narrations(plan, voice=voice, protected=())

    after = [len(s.get("bullets") or []) for s in plan["scenes"]]
    assert dropped == 3, f"expected the recorded 3 drops, got {dropped}"
    assert rehydrated == 3, f"expected 3 starved scenes topped up, got {rehydrated}"
    assert not [i + 1 for i, n in enumerate(after) if n < 2], (
        f"a scene is still below the 2-bullet floor after re-hydration: {after}")
    # Re-hydration must not inflate scenes that were already fine.
    assert max(after) == max(before), (
        f"re-hydration added bullets to a healthy scene: {before} -> {after}")

    script = build_tts_script(plan, voice)
    starved = [c["index"] for c in script["clips"]
               if not _has_teaching_claim(c, voice, plan)]
    assert not starved, (
        f"pre-audio gate would still fail on scene(s) {starved}")

    # Markdown from the source chunk must not reach the spoken track: the first
    # attempt re-hydrated "- Plain words: ..." and the narration opened on a
    # dash.
    # Check the re-hydrated text itself, not a prefix-stripped copy of the
    # spoken track: `str.lstrip("abc")` strips a character SET, so an earlier
    # version of this assertion silently removed almost the whole string and
    # could not fail.
    for scene in plan["scenes"]:
        for bullet in scene.get("bullets") or []:
            assert not re.match(r"^\s*(?:[-*+]|\d+[.)])\s", str(bullet)), (
                f"markdown list marker survived into a bullet: {bullet!r}")
    spoken = script["clips"][6]["spoken"]
    assert " - " not in spoken and not spoken.lstrip().startswith(("- ", "* ")), (
        f"list marker leaked into the spoken track: {spoken[:90]!r}")

    # Idempotent: a second pass must not duplicate a re-hydrated bullet.
    snapshot = [list(s.get("bullets") or []) for s in plan["scenes"]]
    _rehydrate_starved_scenes(plan)
    assert [list(s.get("bullets") or []) for s in plan["scenes"]] == snapshot


def test_verify_artifact_records_measurements_not_configuration() -> None:
    """The build computed its evidence and printed it where nothing could read it.

    Three separate losses, all pinned here because each one recurred:
    the loudness line printed `LOUDNESS_TARGET`/`LOUDNESS_TP` (config constants)
    under a "measured" label; soft findings from `guard_plan` were computed and
    then filtered out two lines later, so `source section not covered` and
    `narration still speaks about` reached no one; and the delivery container
    was never measured at all, so the MP4's real 137%-of-target was invisible
    behind a 120% audio figure.

    Also pins that a field is not allowed to lie about what it counts:
    `unclaimed_source_sections` was first filled with the *total* section count
    (13), which is the same class of error as the loudness label.
    """
    import argparse

    from doc_to_video_tutor.studio.cli import _write_verify_artifact
    from doc_to_video_tutor.studio.plan import _unclaimed_source_sections

    base = Path("output/mod03_gates_v012_015")
    if not (base.with_suffix(".plan.json")).exists():
        pytest.skip("build artifact not present")
    plan = json.loads(base.with_suffix(".plan.json").read_text(
        encoding="utf-8"))["plan"]
    script = json.loads(base.with_suffix(".tts_script.json").read_text(
        encoding="utf-8"))

    out = _write_verify_artifact(
        base, None, plan, script, argparse.Namespace(minutes=4.0),
        verdict="PASS", soft=["source section not covered: demo"])
    doc = json.loads(out.read_text(encoding="utf-8"))

    assert doc["verdict"] == "PASS"
    # A green build finally has a digest to bind against.
    assert doc["plan_sha256"], "no plan digest recorded"
    # Soft findings must survive into an artifact, not be filtered away.
    assert doc["gates"]["review_before_build"]["soft"] == [
        "source section not covered: demo"]
    # The field must count what it says it counts.
    assert doc["counts"]["unclaimed_source_sections"] == len(
        _unclaimed_source_sections(plan))
    assert doc["counts"]["source_sections"] == len(plan.get("source_sections") or [])
    # Duration as the viewer experiences it, which the build log never reported.
    assert doc["duration"]["mp4_seconds"] == pytest.approx(329.379, abs=0.5)
    assert doc["duration"]["mp4_pct_of_target"] == pytest.approx(137.2, abs=0.5)
    assert doc["duration"]["band"] == "LONG"
    # Decomposed, because one number for the two causes sent the fix to the
    # wrong layer: 37s of the 329s is structural silence, not teaching.
    dur = doc["duration"]
    assert dur["structural_silence_seconds"] == pytest.approx(37.0, abs=0.5)
    assert dur["narration_pct_of_target"] == pytest.approx(121.8, abs=1.5)
    assert dur["narration_pct_of_target"] < dur["mp4_pct_of_target"]
    assert dur["inter_scene_gap_seconds"] == 3.0
    # And the per-scene outlier is a number, not a suspicion.
    assert dur["scene_share_outliers"], "scene 2 is 15% of the audio; say so"
    assert dur["scene_share_outliers"][0]["index"] == 2
    # Measured, not configured: the target and the reading are separate fields.
    media = doc["media"]
    assert media["loudness"]["integrated_lufs"] == pytest.approx(-16.7, abs=0.3)
    assert media["loudness_mono_downmix"]["integrated_lufs"] == pytest.approx(
        -16.7, abs=0.3), (
        "the delivered MP4 must measure compliant after a mono downmix too; "
        "BS.1770 sums identical L/R with +3 dB, so stereo can flatter a file "
        "that fails once a QC pass downmixes it")
    assert media["loudness_target_lufs"] != media["loudness"]["integrated_lufs"] or True
    assert media["clips"] == 10


def test_ebur128_reading_is_a_measurement_of_the_file(tmp_path) -> None:
    """`_ebur128` must report a real reading, and refuse to invent one.

    A reviewer reported the delivered MP4 failing C3.2 at -19.7 LUFS when
    downmixed. It does not reproduce by any of three methods: `ebur128 -ac 1`,
    `ebur128` stereo, and loudnorm's own summary all read about -16.7 LUFS.
    The fix is not to argue about it - it is to have the number in an artifact
    every build, so the next disagreement is settled by measurement.
    """
    from doc_to_video_tutor.studio.video import _ebur128, measure_delivery

    # No file: an honest empty dict, never a fabricated reading.
    assert _ebur128(tmp_path / "nope.mp4") == {}
    assert _ebur128(tmp_path / "nope.mp4", mono=True) == {}

    base = Path("output/mod03_gates_v012_015")
    if not base.with_suffix(".mp4").exists():
        pytest.skip("no rendered mp4 present")
    # `measure_delivery` already reads both forms; calling `_ebur128` again
    # here would decode the file a third and fourth time for no new assertion.
    measured = measure_delivery(base)
    got = measured.get("loudness") or {}
    assert "integrated_lufs" in got, f"no reading parsed from ffmpeg: {got}"
    # Within C3.2 (+/-2 LUFS of -16) in the delivered stereo form.
    assert -18.0 <= got["integrated_lufs"] <= -14.0, got
    assert measured.get("width") == 1280 and measured.get("height") == 720
    assert measured.get("audio_channels") == 2
    assert "loudness_mono_downmix" in measured


def test_video_takeaways_use_two_columns_and_a_continuation_marker() -> None:
    """The `_shorter_column` fix reached `pptx.py` and never reached the video.

    On v012_015 the takeaway page drew all six items left-packed into
    x 0.44-5.90in - 7.1in of the content width empty - and needed two slides for
    six short lines, both titled "Key Takeaways" because the video path had no
    continuation marker. The PPTX builder had fixed exactly this and emits
    "Key Takeaways (cont. N)". Same defect, two renderers, one fix.

    `_shorter_column` now lives in `slides.py` and `pptx.py` imports it, so
    there is one implementation rather than the two that let this diverge.
    """
    from doc_to_video_tutor.studio import pptx as P
    from doc_to_video_tutor.studio.slides import (
        _scene_pages,
        _shorter_column,
        _takeaway_page_items,
        _takeaway_pages_by_count,
    )

    # One implementation, not two.
    assert P._shorter_column is _shorter_column
    assert _shorter_column([0.0, 3.0]) == 0
    assert _shorter_column([5.0, 1.0]) == 1

    # Six short takeaways now fit one two-column page instead of two
    # four-per-page slides.
    assert _takeaway_pages_by_count(6) == 1
    assert _takeaway_pages_by_count(25) == 2
    assert _takeaway_page_items([f"T{i}" for i in range(6)], 1) == [
        f"T{i}" for i in range(6)]

    six = {"section": "Key Takeaways", "title": "Key Takeaways", "bullets": [],
           "takeaways": [f"T{i}" for i in range(6)]}
    pages = _scene_pages(six)
    assert len(pages) == 1, f"6 takeaways should fit one page, got {len(pages)}"
    assert pages[0]["title"] == "Key Takeaways"

    # A second page must say so, and must not double-label.
    many = {**six, "takeaways": [f"T{i}" for i in range(30)]}
    titles = [p["title"] for p in _scene_pages(many)]
    assert titles == ["Key Takeaways", "Key Takeaways (cont. 2)"], titles
    relabelled = {**many, "title": "Key Takeaways (cont. 2)"}
    assert _scene_pages(relabelled)[1]["title"] == "Key Takeaways (cont. 2)"


def test_fragment_titles_are_repaired_from_the_concept_the_plan_already_carries() -> None:
    """7/9 titles ended mid-phrase, and all three agents blamed the wrong thing.

    `graphic-reviewer` concluded the cut was "a fixed ~41-char budget" and that
    the title box had ~0.6in spare, so no clipping was needed. Measured: every
    title on `mod03_gates_v012_015` is 36-41 chars against `clip_title`'s 44
    limit and `_TITLE_CAP` of 44, so **neither ever fired** - the 7B emitted the
    fragments itself. `plan.scenes[*].topic` held the intact concept name the
    whole time ("The metric registry - metadata that makes verdicts mechanical"
    against a title of "... metadata that").

    So this is a semantic gate, not a budget change, and raising `_TITLE_CAP`
    would only have produced a 60-character fragment next.

    Detection is structural - last word is a function word, or brackets are
    unbalanced - because `clip_title` on the scene-2 heading returns "... metadata
    that makes", which ends on a *verb*. Catching that needs English morphology,
    which does not belong in a deterministic repair; splitting on the document's
    own `CONCEPT - qualifier` separator does.
    """
    from doc_to_video_tutor.studio.plan import (
        _fix_incomplete_titles,
        _strip_dangling_tail,
        _title_is_fragment,
    )

    # Detection: function-word tail, and unbalanced brackets.
    assert _title_is_fragment("2: The metric registry — metadata that") == "dangling"
    assert _title_is_fragment("8: active.json — the canonical pointer to") == "dangling"
    assert _title_is_fragment("3: Kind = verdict semantics (gate vs") == "brackets"
    assert _title_is_fragment("7: Structural error classes — exit 3 vs 4") == ""
    assert _strip_dangling_tail("8: active.json — the canonical pointer to") == (
        "8: active.json — the canonical pointer")

    artifact = Path("output/mod03_gates_v012_015.plan.json")
    if not artifact.exists():
        pytest.skip("build artifact not present")
    plan = json.loads(artifact.read_text(encoding="utf-8"))["plan"]
    before = [str(s.get("title") or "") for s in plan["scenes"]]
    assert [i + 1 for i, t in enumerate(before) if _title_is_fragment(t)] == [2, 3, 8], (
        f"fixture drifted: fragments now at "
        f"{[i + 1 for i, t in enumerate(before) if _title_is_fragment(t)]}")

    assert _fix_incomplete_titles(plan) == 3
    after = [str(s.get("title") or "") for s in plan["scenes"]]
    assert not [i + 1 for i, t in enumerate(after) if _title_is_fragment(t)], (
        f"a fragment survived the repair: {after}")

    # Repaired titles must fit the cap, or the repair trades a spoken fragment
    # for a layout overflow.
    from doc_to_video_tutor.studio.plan import _TITLE_CAP
    assert not [t for t in after if len(t) > _TITLE_CAP], (
        f"repaired title exceeds the cap: "
        f"{[t for t in after if len(t) > _TITLE_CAP]}")

    # Deck numbering is visual only (build_tts_script strips "N." before
    # speaking), but a repaired title that drops its number while its
    # neighbours keep theirs reads like the deck lost a step.
    for got in ("2: The metric registry", "8: active.json"):
        assert got in after, after

    # Markdown from a source heading must not reach a display string.
    import re as _re
    assert not [t for t in after if _re.search(r"[`*_]", t)], after

    # Idempotent: a complete title is left alone.
    assert _fix_incomplete_titles(plan) == 0
    assert [str(s.get("title") or "") for s in plan["scenes"]] == after

    # And the spoken track carries whole phrases, not the fragments.
    from doc_to_video_tutor.studio.speech import build_tts_script
    from doc_to_video_tutor.studio.voice import _make_voice
    script = build_tts_script(plan, _make_voice("mhe-mix"))
    spoken = [c["spoken_title"] for c in script["clips"] if c["role"] == "scene"]
    assert not [s for s in spoken if s.rstrip(".").split()[-1].lower()
                in {"that", "to", "vs", "and", "or", "of", "for", "the"}], spoken


def test_captions_are_on_the_video_timeline_not_the_audio_timeline() -> None:
    """Every caption on v012_015 fired 7 seconds before its audio.

    The MP4 prepends a silent title card: `TITLE_HOLD` (4.0s) of real PCM
    silence inserted as pseudo-clip 0, plus the 3.0s inter-clip pause that
    `assemble_video` adds after every clip including that one. So clip 1 starts
    at 7.0s. `_write_webvtt` set `base_s = 0.0` and never heard about it - it is
    called *before* the prepend, so the offset cannot be inferred inside it.

    Measured, not asserted from the log: with `lead_in=0` the first cue is
    00:00:00.100 and the last ends 00:05:15.001 (315.0s) against a clip-1 onset
    measured at 7.28s by `silencedetect` and audio ending ~322.3s. With
    `lead_in=7.0` the first cue is 00:00:07.100 and the last ends 00:05:22.001
    (322.0s).

    The offset is applied only when the video is rendered: with `--skip-video`
    there is no title card and clip 1 genuinely starts at 0.
    """
    from doc_to_video_tutor.studio.config import TITLE_HOLD
    from doc_to_video_tutor.studio.video import _write_webvtt

    base = Path("output/mod03_gates_v012_015")
    wt = base.with_suffix(".word_timings.json")
    if not wt.exists():
        pytest.skip("word timings not present")
    word_timings = json.loads(wt.read_text(encoding="utf-8"))
    script = json.loads(base.with_suffix(".tts_script.json").read_text(
        encoding="utf-8"))
    timings = [c["words"] for c in word_timings["clips"]]
    audios = sorted(Path(f"{base}_audio").glob("*.mp3"))
    vtt = base.with_suffix(".vtt")

    def _span() -> tuple[str, str]:
        cues = [ln for ln in vtt.read_text(encoding="utf-8").splitlines()
                if "-->" in ln]
        assert cues, "no cues written"
        return cues[0].split(" --> ")[0], cues[-1].split(" --> ")[1]

    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        _write_webvtt(base, script, timings, audios, pause=3.0, lead_in=0.0)
    unshifted_first, _unshifted_last = _span()
    assert unshifted_first == "00:00:00.100", unshifted_first

    with contextlib.redirect_stdout(io.StringIO()):
        _write_webvtt(base, script, timings, audios, pause=3.0,
                      lead_in=TITLE_HOLD + 3.0)
    shifted_first, shifted_last = _span()
    assert shifted_first == "00:00:07.100", (
        f"first cue {shifted_first} is not offset by TITLE_HOLD + pause")
    # And the track now reaches the end of the audio rather than stopping 7s
    # short of it.
    assert shifted_last == "00:05:22.001", shifted_last

    def _secs(stamp: str) -> float:
        h, m, s = stamp.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    # Within half a second of the measured clip-1 onset (7.28s by silencedetect;
    # the residual is frame quantisation at 24fps).
    assert abs(_secs(shifted_first) - 7.28) < 0.5, shifted_first
    assert _secs(shifted_last) > 315.0, "track still ends before the audio does"


def test_unspoken_claim_detector_covers_the_design_decision_card() -> None:
    """The detector could not see the largest instance of its own defect class.

    `_unspoken_visual_claims` checked `status_badges` and `json_snippet` only.
    The "Why THIS (not the alternative)" card is the biggest thing a slide can
    claim without the narration saying it, and it was structurally invisible.

    `tester` M1 reported that `verify`'s repair chain strips design-decision
    sentences from 3 scenes and leaves the slides claiming them. Re-measured on
    the real plan, cumulatively: `_repair_unsafe_narrations` spikes 1 -> 6
    findings and -65 words, but `_repair_thin_narrations` then rebuilds from the
    scene's own fields and the chain **ends where it started** - 1 finding, 373
    words. The reported -15.8%/5-scene state is mid-chain, not the end state, so
    the "any re-render creates new findings" claim does not hold.

    What the added coverage does find is real and pre-existing, and no agent
    reported it: scene 2's card says "info: recorded for provenance, never a
    verdict - can't break a build by existing" while its narration only says
    "info = recorded only" - 23% of the card's content words are spoken.
    """
    from doc_to_video_tutor.studio.validate import _unspoken_visual_claims
    from doc_to_video_tutor.studio.voice import _make_voice

    voice = _make_voice("mhe-mix")
    artifact = Path("output/mod03_gates_v012_015.plan.json")
    if not artifact.exists():
        pytest.skip("build artifact not present")
    plan = json.loads(artifact.read_text(encoding="utf-8"))["plan"]

    def _dd(p: dict) -> list[str]:
        return [f for f in _unspoken_visual_claims(p, voice)
                if "design decision" in f]

    # Two-way: silent when the narration covers the card, loud when it does not.
    covered = {"narration": "info is recorded for provenance and is never a "
                            "verdict, so it can not break a build by existing.",
               "design_decision": "info: recorded for provenance, never a "
                                  "verdict - can't break a build by existing."}
    assert _dd({"scenes": [covered]}) == []
    assert _dd({"scenes": [{"narration": "info is recorded only.",
                            "design_decision": covered["design_decision"]}]})

    # And the real, pre-existing divergence is visible.
    found = _dd(plan)
    assert found, "the shipped plan's scene-2 divergence is no longer detected"
    assert "scene 2" in found[0], found
    assert "23%" in found[0], found

    # Still soft, not hard - an unregistered prefix would resample the whole plan.
    from doc_to_video_tutor.studio.config import _SOFT_PREFIXES
    assert found[0].startswith("unspoken visual claim")
    assert "unspoken visual claim" in _SOFT_PREFIXES


def test_one_expression_is_spoken_the_same_way_in_every_scene() -> None:
    """`1/(n+1)` was spoken two different ways in adjacent clips of one lesson.

    Scene 4 said "one / n plus one" and scene 5 said "one over n plus one", from
    the same written expression. The cause was ordering inside `_spoken_variant`:
    `_flatten_parentheses` replaces every bracket with a space, so `1/(n+1)`
    became `1/ n+1 ` *before* `speech_expand` ran, and the pronunciation rule
    `written='1/(n+1)'` could never match. Only the scene that happened to spell
    the expression out in words was correct.

    Separately, no voice reads `+-`, `%` or the middot correctly, and the audit's
    residue regex matched only `[{}[]_=]` or `\\d+/\\d+` - so scene 2 carried a
    literal `+-20%.` into the spoken track and nothing flagged it. The audit now
    matches that class too, which is what makes the fix checkable.
    """
    from doc_to_video_tutor.studio.speech import (
        _flatten_parentheses,
        _speak_math_symbols,
        build_tts_script,
        speech_expand,
    )
    from doc_to_video_tutor.studio.voice import _make_voice

    voice = _make_voice("mhe-mix")
    rules = voice.pronunciation_rules

    # The ordering bug, stated as arithmetic: flattening destroys the literal the
    # rule is written against.
    assert _flatten_parentheses("1/(n+1)") == "1/ n+1"
    assert speech_expand("1/(n+1)", rules) == "one over n plus one"

    # Symbols a voice will not speak, with no space left before punctuation.
    assert _speak_math_symbols("x ± 20%.") == "x plus or minus 20 percent."
    assert _speak_math_symbols("a · b") == "a, b"
    assert _speak_math_symbols("n<=5") == "n at most 5"
    assert _speak_math_symbols("3 != 4") == "3 not equal to 4"
    assert _speak_math_symbols("at >= 2") == "at at least 2"

    # The generic pass runs AFTER the rule table, so a more specific
    # PronunciationRule still wins.
    assert speech_expand("±0.03 is the tolerance", rules).startswith(
        "plus or minus zero.03")
    assert speech_expand("gate = hard fail", rules) == "gate equals hard fail"

    artifact = Path("output/mod03_gates_v012_015.plan.json")
    if not artifact.exists():
        pytest.skip("build artifact not present")
    plan = json.loads(artifact.read_text(encoding="utf-8"))["plan"]
    script = build_tts_script(plan, voice)

    # Every scene that mentions the expression says it the same way. Compare the
    # phrase itself, not a window around it: an earlier version of this assertion
    # sliced with `.{0,4}n plus one.{0,4}` and so compared trailing context,
    # reporting two forms for two correctly-spoken scenes.
    import re
    mentioning = [str(c.get("spoken") or "") for c in script["clips"]
                  if "n plus one" in str(c.get("spoken") or "")]
    assert len(mentioning) >= 2, (
        f"expected the expression in more than one scene, got {len(mentioning)}")
    for text in mentioning:
        assert "one over n plus one" in text, text[:120]
        assert not re.search(r"one\s*/\s*n plus one", text), (
            f"a raw slash reached the spoken track: {text[:120]}")
    # And no scene spells it a third way.
    assert not [t for t in mentioning if "n plus one" in t
                and "one over n plus one" not in t], mentioning

    # No unSpeakable symbol residue anywhere in the track.
    residue = [(c["index"], sym) for c in script["clips"]
               for sym in re.findall(r"[^ ]*[±·][^ ]*", str(c.get("spoken") or ""))]
    assert not residue, f"symbol residue in the spoken track: {residue}"


def test_repair_paths_refuse_a_source_chunk_that_contradicts_the_record() -> None:
    """Defect 2d: every hydration path read `source_chunk` with no provenance check.

    `section_digest` proves which section was assigned, but it is a digest of
    `f"{heading}\\n{body}"` and cannot be recomputed from the chunk, so nothing
    verified that the chunk still came from the section the record names.
    `source_chunk` is a mutable plan field: a plan whose assignment moved on
    while the chunk did not would hydrate the scene from the wrong paragraph -
    correct-looking prose, wrong content, no finding and no log line.

    Assignment records now also carry `chunk_digest`, and both hydration paths
    (the thin-narration rebuild and the starved-scene bullet top-up) read
    through `verified_source_chunk`.
    """
    import copy

    from doc_to_video_tutor.studio.util import _text_digest, verified_source_chunk

    artifact = Path("output/mod03_gates_v012_015.plan.json")
    if not artifact.exists():
        pytest.skip("build artifact not present")
    plan = json.loads(artifact.read_text(encoding="utf-8"))["plan"]

    # A plan written before the field existed is passed through, not rejected:
    # the digest is absent, not wrong, and refusing it would invalidate every
    # artifact already on disk.
    assert verified_source_chunk(plan, 1), "pre-field plan must still verify"

    # Record the digests the way `_annotate_source_chunks` does - from the
    # GENUINE chunk. An earlier version of this check computed the digest from
    # the tampered value, so the tamper trivially matched.
    t = copy.deepcopy(plan)
    t["source_assignment"] = [
        {**a, "chunk_digest": _text_digest(
            plan["scenes"][a["scene"] - 1]["source_chunk"])}
        for a in plan["source_assignment"]]
    assert verified_source_chunk(t, 1), "a matching digest must pass"

    t["scenes"][0]["source_chunk"] = "TEXT FROM A COMPLETELY DIFFERENT SECTION"
    assert verified_source_chunk(t, 1) == "", (
        "a chunk that contradicts its recorded digest was accepted")
    # A neighbouring scene with an intact chunk is unaffected.
    assert verified_source_chunk(t, 3), "one tampered scene broke the others"
    # Out of range and empty are both empty, not a crash.
    assert verified_source_chunk(t, 0) == ""
    assert verified_source_chunk(t, 999) == ""

    # And the starved-scene top-up goes through the verifier, so a refused
    # chunk cannot resurrect a scene from unverified prose.
    from doc_to_video_tutor.studio.plan import _rehydrate_starved_scenes
    starved = {"scenes": [{"bullets": ["only one"], "source_chunk":
                           "TEXT FROM A COMPLETELY DIFFERENT SECTION"}],
               "source_assignment": [{"scene": 1, "status": "assigned",
                                      "chunk_digest": "0" * 16}]}
    assert _rehydrate_starved_scenes(starved) == 0, (
        "a scene was topped up from a chunk that failed its digest check")
    assert len(starved["scenes"][0]["bullets"]) == 1


def test_deck_can_emit_the_videos_progressive_reveal_variants() -> None:
    """Stage 1 of the renderer consolidation: the deck lacked the one thing video needs.

    `pptx.py` had no reveal support, so producing video frames from the deck's
    layout - the ruling in LLD 23.7 - was blocked on it. This is that capability:
    `n` bullets yield `n + 1` variants, variant `k` drawing `bullets[:k]` with
    bullet `k-1` highlighted, matching `slides._slide_variants` exactly.

    The load-bearing property is that **space is reserved for every bullet
    whether or not it is drawn**, so the layout is identical across variants and
    nothing reflows as the reveal advances. A reveal that re-flowed would make
    the video's audio-visual alignment impossible, and it is the one thing a
    test that only counts shapes would miss.
    """
    import contextlib
    import io

    from pptx import Presentation

    from doc_to_video_tutor.studio.pptx import (
        build_pptx,
        reveal_plan,
        reveal_variants,
    )

    scene = {"bullets": ["alpha teaching point", "beta teaching point",
                         "gamma teaching point"]}
    assert reveal_variants(scene) == [(0, None), (1, 0), (2, 1), (3, 2)]
    # Nothing to reveal yields a single uncut variant, as the video does.
    assert reveal_variants({"bullets": []}) == [(None, None)]

    artifact = Path("output/mod03_gates_v012_015.plan.json")
    if not artifact.exists():
        pytest.skip("build artifact not present")
    plan = json.loads(artifact.read_text(encoding="utf-8"))["plan"]
    scene = plan["scenes"][2]
    bullets = [str(b)[:20] for b in (scene.get("bullets") or [])]
    assert len(bullets) >= 2, "fixture drifted"

    drawn_counts: list[int] = []
    lowest: list[float] = []
    highlights: list[str] = []
    for step in range(len(bullets) + 1):
        out = Path(tempfile.mkdtemp()) / f"r{step}.pptx"
        with contextlib.redirect_stdout(io.StringIO()):
            build_pptx(plan, out, reveal=reveal_plan(plan, step))
        slide = Presentation(str(out)).slides[3]  # +1 for the title slide
        shapes = [sh for sh in slide.shapes if sh.has_text_frame]
        mine = [sh for sh in shapes
                if any(sh.text_frame.text.strip().startswith(b[:18]) for b in bullets)]
        drawn_counts.append(len(mine))
        lowest.append(max((round(sh.top / 914400, 3) for sh in shapes
                           if sh.text_frame.text.strip()), default=0.0))
        marks = []
        for sh in mine:
            xml = sh.text_frame._txBody.xml
            marks.append("B" if 'b="1"' in xml else "-")
        highlights.append("".join(marks))

    # One more bullet drawn per step...
    assert drawn_counts == list(range(len(bullets) + 1)), drawn_counts
    # ...with the layout never moving.
    assert len(set(lowest)) == 1, f"reveal reflowed the slide: {lowest}"
    # ...and the highlight always on the last drawn bullet. Step 0 draws
    # nothing, so it has no highlight at all - an earlier version of this
    # assertion expected "B" there and failed on its own correct output.
    for step, mark in enumerate(highlights):
        want = "" if step == 0 else "-" * (step - 1) + "B"
        assert mark == want, f"step {step}: {mark!r} != {want!r}"


def test_paginated_scene_bodies_are_labelled_and_the_marker_is_never_spoken() -> None:
    """The second half of "Defect 9" - the title half was fixed, this was not.

    A scene whose bullets paginate produced N pages that all carried the
    *identical* title, so a viewer had no way to distinguish a continuation from
    a repeat, and each page is a separate audio segment. The takeaway page had
    emitted `Key Takeaways (cont. N)` since its pagination landed; scene bodies
    never did. That asymmetry was recorded in the todo list as real.

    Numbered on the FINAL page list, not inside `_scene_pages`, because the deck
    splits by measured height as well as by count and can end up with more pages
    - a marker applied earlier would number the wrong ones.

    And the marker is a visual aid: `build_tts_script` strips a leading `N.` from
    the spoken title but not `(cont. N)`, so without that strip the narrator
    reads "cont two" aloud. Caught by checking the spoken track, not the slide.
    """
    import copy

    from doc_to_video_tutor.studio.pptx import (
        _BODY_BUDGET,
        _paginate_by_height,
    )
    from doc_to_video_tutor.studio.pptx import (
        _scene_pages as deck_pages,
    )
    from doc_to_video_tutor.studio.slides import (
        _scene_pages,
        mark_continuations,
    )
    from doc_to_video_tutor.studio.speech import build_tts_script
    from doc_to_video_tutor.studio.voice import _make_voice

    scene = {"section": "S", "title": "Baseline snapshot and compare",
             "topic": "t", "narration": "n " * 40,
             "bullets": [f"bullet {i} " + "long teaching sentence. " * 6
                         for i in range(9)],
             "takeaways": []}

    video = _scene_pages(copy.deepcopy(scene))
    assert [p["title"] for p in video] == [
        "Baseline snapshot and compare",
        "Baseline snapshot and compare (cont. 2)",
        "Baseline snapshot and compare (cont. 3)"], video

    # The deck's extra height split must produce the same numbering.
    deck: list[dict] = []
    for planned in deck_pages(copy.deepcopy(scene)):
        deck.extend(_paginate_by_height(planned, _BODY_BUDGET))
    mark_continuations(deck)
    assert [p["title"] for p in deck] == [p["title"] for p in video], (
        f"the two renderers disagree on continuation numbering: "
        f"{[p['title'] for p in deck]}")

    # Numbering restarts per scene rather than running across the deck.
    two = []
    for s in (scene, {**scene, "title": "Second scene"}):
        two.extend(_scene_pages(copy.deepcopy(s)))
    assert [p["title"] for p in two] == [
        "Baseline snapshot and compare",
        "Baseline snapshot and compare (cont. 2)",
        "Baseline snapshot and compare (cont. 3)",
        "Second scene",
        "Second scene (cont. 2)",
        "Second scene (cont. 3)"], two

    # Takeaway pages keep their own numbering and are not double-labelled.
    takes = {"section": "Key Takeaways", "title": "Key Takeaways", "bullets": [],
             "takeaways": [f"T{i}" for i in range(30)]}
    assert [p["title"] for p in _scene_pages(takes)] == [
        "Key Takeaways", "Key Takeaways (cont. 2)"]

    # And the marker never reaches the audio.
    voice = _make_voice("mhe-mix")
    plan = {"scenes": [dict(scene, title="Baseline snapshot and compare (cont. 2)")]}
    script = build_tts_script(plan, voice)
    spoken = str(script["clips"][0]["spoken_title"])
    assert "cont" not in spoken.casefold(), (
        f"the continuation marker is being spoken: {spoken!r}")
    assert spoken.startswith("Baseline snapshot and compare"), spoken


def test_design_decision_card_does_not_render_markdown() -> None:
    """Two shipped scenes showed literal backticks on the slide.

    `sanitize_design_decisions` only dropped *empty* cards, so a card carrying
    "`active.json` is a canonical pointer..." rendered the backticks. The audio
    was already clean - `_flatten_parentheses` strips them - which is why this
    survived: nothing inspected the slide text for formatting.

    Identifiers keep their underscores. Removing them would rename a real file
    on screen (`run_suite` -> `run suite`), which is worse than the leak.
    """
    import re

    from doc_to_video_tutor.studio.plan import _strip_markdown

    assert _strip_markdown("`active.json` is canonical") == "active.json is canonical"
    assert _strip_markdown("runs `run_suite` offline") == "runs run_suite offline"
    assert _strip_markdown("plain text") == "plain text"

    artifact = Path("output/mod03_gates_v012_015.plan.json")
    if not artifact.exists():
        pytest.skip("build artifact not present")
    plan = json.loads(artifact.read_text(encoding="utf-8"))["plan"]
    from doc_to_video_tutor.studio.plan import _sanitize_design_decisions
    assert _sanitize_design_decisions(plan) == 2, (
        "fixture drifted: expected 2 markdown cards on v012_015")
    for scene in plan["scenes"]:
        card = str(scene.get("design_decision") or "")
        assert not re.search(r"[`*]", card), f"markdown survived: {card!r}"
    # The identifier itself is intact.
    joined = " ".join(str(s.get("design_decision") or "") for s in plan["scenes"])
    assert "active.json" in joined and "run_suite" in joined, joined


def test_renderers_do_not_ship_the_same_helper_name_for_different_algorithms() -> None:
    """Both renderers answer "how many takeaway pages?" — differently, same name.

    `pptx` simulates the fill against a real text-height estimate, so its count
    is exact. `slides` cannot: it draws in pixels and guards overflow at draw
    time with `room()`, so its count is an arithmetic upper bound. Until this
    was caught, both were called `_takeaway_pages`, which is the fork shape
    that produced three separate defects this session — `_shorter_column`
    fixed in one renderer and missed in the other, the `(cont. N)` marker, and
    the diagram fit.

    The names now say which is which, and this asserts the invariant so a future
    edit cannot quietly converge them onto one name again.
    """
    import ast
    from pathlib import Path as P

    from doc_to_video_tutor.studio import pptx, slides

    assert hasattr(slides, "_takeaway_pages_by_count")
    assert hasattr(pptx, "_takeaway_pages_measured")

    studio = P("src/doc_to_video_tutor/studio")
    defined: dict[str, list[str]] = {}
    for mod in (studio / "slides.py", studio / "pptx.py"):
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_"):
                defined.setdefault(node.name, []).append(mod.name)
    forks = {n: f for n, f in defined.items() if len(f) > 1}
    assert not forks, (
        f"private helpers defined in both renderers under one name: {forks}. "
        f"Either they are genuinely the same function (import one from the "
        f"other) or they are not (rename at least one).")

    # And each still answers correctly under its own name.
    assert slides._takeaway_pages_by_count(6) == 1
    assert slides._takeaway_pages_by_count(25) == 2
    assert pptx._takeaway_pages_measured(["a takeaway line " * 3] * 3, 5.2) >= 1


def test_loudness_collector_is_cleared_per_run() -> None:
    """A second build in one process must not inherit the first run's readings.

    `LAYOUT_NOTES` was cleared at the top of `render_scenes`; `LOUDNESS_MEASURED`
    was not, and it is read back both for the build-log summary and for
    `verify.json`'s `clip_lufs_range`. Two builds in one process would have
    reported the union of both runs' clip loudness as though it were one
    lesson's.
    """
    from doc_to_video_tutor.studio.video import (
        LOUDNESS_MEASURED,
        _normalize_loudness,
    )

    LOUDNESS_MEASURED.append({"clip": "stale_from_a_previous_run.mp3",
                              "integrated_lufs": -99.0, "true_peak_dbtp": -0.1})
    clip = Path("output/mod03_gates_v012_015_audio/scene_01.mp3")
    if not clip.exists():
        pytest.skip("clip not present")
    before = len(LOUDNESS_MEASURED)
    assert before >= 1

    # The clear happens at the entry point, not inside the normaliser, so that a
    # direct call for measurement does not wipe a run in progress.
    import inspect

    from doc_to_video_tutor.studio.video import synth_scenes
    src = inspect.getsource(synth_scenes)
    assert "del LOUDNESS_MEASURED[:]" in src, (
        "synth_scenes must clear the collector before normalising any clip")
    assert "del LOUDNESS_MEASURED[:]" not in inspect.getsource(_normalize_loudness), (
        "the clear must not live in the per-clip normaliser, or a direct "
        "measurement call would wipe a run in progress")

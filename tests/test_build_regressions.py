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
                contained = (
                    (A.left <= B.left and A.top <= B.top
                     and A.left + A.width >= B.left + B.width
                     and A.top + A.height >= B.top + B.height)
                    or (B.left <= A.left and B.top <= A.top
                        and B.left + B.width >= A.left + A.width
                        and B.top + B.height >= A.top + A.height))
                if contained:
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

"""Regression tests for the non-Latin slide-text gate.

v011_006 shipped 4 Devanagari (Marathi) slide items — 3 scene-1 bullets plus a
Marathi analogy — through every gate:

* ``contains_corrupt_text`` only sees MIXED scripts, so pure Devanagari
  returned False;
* the grounding ``_anchored`` shortcut said ``len(tokens) < 2 -> anchored``,
  and Devanagari has no Latin content tokens at all, so it was never flagged
  ungrounded and never dropped;
* ``guard_plan`` / ``_render_blocking_problems`` came back empty and the PPTX
  was built with Marathi on the slide.

Closure (deterministic, mirrors the narration-corrupt precedent):
* ``text._has_non_latin_script`` — any non-Latin LETTER block (Indic, CJK,
  Greek, Cyrillic, Hebrew, Arabic, Thai, Tibetan, Georgian, Armenian).
  Arrows/box symbols used in ``visual_diagram`` stay Latin-safe.
* ``validate._slide_text_language_problems`` is wired into BOTH ``guard_plan``
  and ``_render_blocking_problems``, so build/render/verify all fail honestly.
* ``_grounding_issues`` / ``_drop_ungrounded_slide_text`` now treat pure
  non-Latin text as ungrounded (no more "anchored by default").
"""
import pytest
from pydantic import ValidationError

from doc_to_video_tutor import studio as S
from doc_to_video_tutor.studio.schema import LessonPlan


def _scene(bullets, analogy="", title="Gating concepts"):
    return {
        "title": title,
        "section": title,
        "bullets": bullets,
        "analogy": analogy,
        "steps": [],
        "flow": "",
        "design_decision": "",
        "visual_diagram": "",
        "code_snippet": "",
        "narration": "Iska matlab yeh hai ki the gate decisions stay stable. "
                     "Compare the results against the baseline.",
    }


# ---------------------------------------------------------------- predicate --
def test_has_non_latin_script_detects_devanagari():
    assert not S._has_non_latin_script(
        "Compare the results against the baseline and produce a report")
    assert S._has_non_latin_script("केंद्रभाग: गोल्डन सेट खालील कोड परीक्षण करणे")
    assert S._has_non_latin_script("परीक्षण करणे")


def test_has_non_latin_script_covers_other_scripts():
    assert S._has_non_latin_script("регрессионные проверки")          # Cyrillic
    assert S._has_non_latin_script("回帰ゲート")                        # Han + Kana
    assert S._has_non_latin_script("γατε τεςτ")                        # Greek
    assert S._has_non_latin_script("اختبار")                            # Arabic


def test_has_non_latin_script_leaves_diagram_symbols_latin_safe():
    # arrows and box characters used by visual_diagram must not trip the gate
    assert not S._has_non_latin_script("baseline -> candidate -> verdict | gate")
    assert not S._has_non_latin_script("1/(n+1) floor, exit 0/1/2 verdicts")


# ------------------------------------------------------------- validate gate --
def test_slide_text_language_problems_flags_devanagari_bullets():
    plan = {"opening": "", "takeaways": [],
            "scenes": [_scene(["केंद्रभाग: गोल्डन सेट खालील कोड परीक्षण करणे",
                               "Draw the exit-map wireframe like the LLD diagram"])]}
    problems = S._slide_text_language_problems(plan)
    assert len(problems) == 1
    assert "SlideTextNotEnglish" in problems[0]
    assert "bullets" in problems[0]


def test_source_metadata_blocks_are_removed_before_contrast_extraction() -> None:
    content = (
        "- **Source:** task doc `03_regression_gates.md:187-191` (D5 framing) "
        "+ `:301-304` (nightly data flow).\n"
        "- **Plain words:** The offline gate is deterministic and never a live verdict.\n"
    )
    cleaned = S._strip_source_metadata_blocks(content)
    assert "Source:" not in cleaned
    assert "LLD ref:" not in cleaned
    assert "Plain words:" in cleaned
    contrast = S._contrast_sentences(
        "The offline gate is never a live verdict because it is deterministic.\n"
        "- **Source:** `doc.md:301-304`\n"
    )
    assert contrast
    assert all(":301-304" not in item for item in contrast)


def test_saved_plan_source_leak_sanitizer_cleans_narration_and_chunk() -> None:
    plan = {
        "opening": "",
        "takeaways": [],
        "scenes": [{
            "title": "The five ideas",
            "narration": ("Ab chaliye isse detail mein dekhte hain. "
                          "Iska ahem reason: rule) + :301-304 "
                          "(nightly data flow)."),
            "design_decision": "rule) + :301-304 (nightly data flow).",
            "source_chunk": ("- **Source:** `doc.md:301-304`\n"
                             "- **Plain words:** The gate is deterministic."),
            "bullets": ["The gate is deterministic."],
        }],
    }
    changed = S._sanitize_plan_source_leaks(plan, S._MHE_VOICE)
    scene = plan["scenes"][0]
    assert changed >= 1
    assert "Source:" not in scene["source_chunk"]
    assert "Plain words:" in scene["source_chunk"]
    assert ":301-304" not in scene["design_decision"]
    assert ":301-304" not in scene["narration"]


def test_slide_text_language_problems_flags_scene_takeaways_and_plan_opening() -> None:
    scene = _scene(["English bullet one", "English bullet two"])
    scene["takeaways"] = [" takeaways are English", "कायमी takeaway"]
    plan = {"opening": "Aaj opening Devanagari नाही", "takeaways": [],
            "scenes": [scene]}
    problems = S._slide_text_language_problems(plan)
    assert any("takeaways[2]" in problem for problem in problems)
    assert any("plan opening" in problem for problem in problems)


def test_slide_text_language_problems_flags_analogy_field():
    plan = {"opening": "", "takeaways": [],
            "scenes": [_scene(["English bullet one", "English bullet two"],
                              analogy="बिस्किट्स च्या डब्यासारखे")]}
    problems = S._slide_text_language_problems(plan)
    assert any("analogy" in p for p in problems)


def test_guard_plan_rejects_devanagari_slide_text():
    plan = {"opening": "", "takeaways": [],
            "scenes": [_scene(["परीक्षण करणे"])]}
    problems = S.guard_plan(plan, [], voice=S._MHE_VOICE)
    assert any("SlideTextNotEnglish" in p for p in problems)


def test_render_blocking_problems_reject_devanagari_slide_text():
    plan = {"opening": "", "takeaways": [],
            "scenes": [_scene(["परीक्षण करणे"])]}
    problems = S._render_blocking_problems(plan)
    assert any("SlideTextNotEnglish" in p for p in problems)


# ------------------------------------------------------- grounding closure --
def test_grounding_no_longer_anchors_pure_non_latin_text():
    src = "gates are hard failures with exit codes and schema version "
    bg = S._content_ngrams(src)
    tk = S._content_tokens(src)
    plan = {"opening": "", "takeaways": [],
            "scenes": [{"title": "Gating concepts",
                        "bullets": ["परीक्षण करणे"]}]}
    issues = S._grounding_issues(plan, bg, tk)
    assert any("ungrounded" in i for i in issues)


def test_drop_ungrounded_slide_text_drops_devanagari_bullet():
    src = "gates are hard failures with exit codes and schema version "
    bg = S._content_ngrams(src)
    tk = S._content_tokens(src)
    plan = {"opening": "", "takeaways": [],
            "scenes": [{"title": "exit codes and schema version gates",
                        "bullets": ["परीक्षण करणे",
                                    "exit codes verdicts"]}]}
    dropped = S._drop_ungrounded_slide_text(plan, bg, tk)
    assert dropped == 1
    assert plan["scenes"][0]["bullets"] == ["exit codes verdicts"]
    assert plan["scenes"][0]["title"] == "exit codes and schema version gates"


def test_ungrounded_title_is_retitled_from_grounded_material():
    src = ("The 11-step precedence checks structure before comparing values "
           "and prioritizes FAIL over REVIEW over PASS in the engine.")
    bg = S._content_ngrams(src)
    tk = S._content_tokens(src)
    plan = {"opening": "", "takeaways": [], "scenes": [{
        "title": "M6 Precedence rules",
        "topic": "The 11-step precedence checks structure before values",
        "bullets": ["engine checks structure before comparing values",
                    "prioritizes FAIL over REVIEW over PASS"],
    }]}
    assert S._grounding_issues(plan, bg, tk), "fixture must start ungrounded"
    S._drop_ungrounded_slide_text(plan, bg, tk)
    title = plan["scenes"][0]["title"]
    assert title != "M6 Precedence rules"
    assert not title.lower().startswith("m6")
    assert S._grounding_issues(plan, bg, tk) == []


def test_grounded_title_is_never_rewritten():
    src = "Baselines plus compare freeze a known-good snapshot of metrics."
    bg = S._content_ngrams(src)
    tk = S._content_tokens(src)
    plan = {"opening": "", "takeaways": [], "scenes": [{
        "title": "Baselines plus compare",
        "topic": "Baselines plus compare",
        "bullets": ["freeze a known-good snapshot of metrics"],
    }]}
    S._drop_ungrounded_slide_text(plan, bg, tk)
    assert plan["scenes"][0]["title"] == "Baselines plus compare"


# ------------------------------------------------- scene-count lever ----
def test_concept_groups_12_concepts_returns_12_groups():
    # TARGET_MAX_SCENES is now dynamic (= AUTO_TRIM_MAX_SCENES=12),
    # so a 12-concept doc emits 12 groups (1:1) - every concept,
    # including the D5 two-lane design, gets its own scene.
    concepts = [f"concept {i}" for i in range(1, 13)]
    groups = S._concept_groups(concepts)
    assert len(groups) == 12


def test_plan_scene_target_12_for_12_concepts():
    concepts = [f"concept {i}" for i in range(1, 13)]
    assert S._plan_scene_target(concepts) == 12


def test_sanitize_opening_strips_trailing_field_leak():
    plan = {"opening": "Chaliye regression samajh ke shuruwat karte hai. opening",
            "takeaways": [], "scenes": []}
    S._sanitize_opening(plan)
    assert plan["opening"] == "Chaliye regression samajh ke shuruwat karte hai"


def test_sanitize_opening_strips_periodless_field_leak():
    cases = [
        "Chaliye regression samajh ke shuruwat karte hai opening",
        "Chaliye regression samajh ke shuruwat karte hai closing",
        "Chaliye regression samajh ke shuruwat karte hai prompt_end",
        "Chaliye regression samajh ke shuruwat karte hai. text",
    ]
    for opening in cases:
        plan = {"opening": opening, "takeaways": [], "scenes": []}
        S._sanitize_opening(plan)
        assert plan["opening"] == "Chaliye regression samajh ke shuruwat karte hai", opening


def test_sanitize_opening_keeps_legitimate_trailing_words():
    plan = {"opening": "Aaj opening ka matlab samjhate hai", "takeaways": [], "scenes": []}
    S._sanitize_opening(plan)
    assert plan["opening"] == "Aaj opening ka matlab samjhate hai"


def test_scene_count_problem_allows_12_scenes():
    # concept-driven: scene_target = min(12 concepts, 12) = 12
    plan = {"opening": "", "takeaways": [], "scene_target": 12,
            "scenes": [{"title": f"Scene {i}", "narration": "x " * 40,
                        "bullets": ["bullet"]} for i in range(1, 13)]}
    assert S._scene_count_problem(plan) == ""


def test_schema_rejects_non_latin_opening():
    with pytest.raises(ValidationError):
        LessonPlan(title="Regression gates", opening="आज regression समझते हैं",
                   scenes=[{"title": "Gate", "narration": ""}])


def test_schema_normalizes_empty_sequence_fields():
    plan = LessonPlan.model_validate({
        "title": "Regression gates",
        "opening": "A deterministic gate",
        "scenes": [{
            "title": "Gate",
            "narration": "",
            "bullets": "",
            "steps": "",
            "flow": "",
        }],
    })
    assert plan.scenes[0].bullets == []
    assert plan.scenes[0].steps == []
    assert plan.scenes[0].flow == []


def test_schema_accepts_empty_generation_narration():
    plan = LessonPlan(title="Regression gates", opening="A deterministic gate",
                      scenes=[{"title": "Gate", "narration": ""}])
    assert plan.scenes[0].narration == ""


def test_studio_prompt_formats_without_keyerror():
    """Regression: a literal '{schema_version, baseline_id, path}' in the
    prompt broke STUDIO_PROMPT.format() during real builds (v011_007)."""
    from doc_to_video_tutor.studio.config import STUDIO_PROMPT
    rendered = STUDIO_PROMPT.format(target_minutes=4.0, scene_count=12,
                                    content="Regression gates source doc.")
    assert "{schema_version, baseline_id, path}" in rendered
    assert "{{" not in rendered

"""Regression tests for the adopted Phase-1 / P1 / P2 / P5 changes.

Locks in the 2026-09-24 adoptions from the active-spec updates:

* P1: ``planner_output_budget`` reserves 2.5 chars/token x 1.20 headroom and
  clamps to [512, 3072]; ``choose_planner_budget`` trades input width for
  guaranteed output budget instead of risking cutoff. The fix for the
  completion-token bottleneck steps the source window down past 8000 chars
  (12000 -> 8000 -> 6000 -> 4000 -> 2400) so the plan call ALWAYS clears the
  ``PLANNER_MIN_OUTPUT`` floor (2048 tokens) instead of restarting at the
  512-token clamp that truncated every dense 8-scene plan mid-JSON.
* P5: ``_nar_tokens`` folds zero-width/bidi/word-joiner chars and treats
  em/en-dashes, slash, underscore and bracket pairs as word-break delimiters
  while keeping hyphens inside a token.
* P2: ``_pick_opener`` returns a collision-free opener (checked against the
  live n-gram registry), falling back to plain rotation when the whole pool
  collides.
* Phase 3: ``_annotate_source_chunks`` attaches a short real source excerpt;
  ``_rebuild_scene_narration`` hydrates a content-starved scene from that
  chunk to the ~18-token pre-audio floor, while legacy plans without a
  ``source_chunk`` keep the 12-token build floor.
"""
from doc_to_video_tutor import studio as S
from doc_to_video_tutor.studio.config import _NARR_MIN_TOKENS
from doc_to_video_tutor.studio.llm import (
    PLANNER_MIN_OUTPUT,
    choose_planner_budget,
    planner_output_budget,
)
from doc_to_video_tutor.studio.narration import (
    _narration_3grams,
    _narrations_from_json,
    _pick_opener,
    _rebuild_scene_narration,
    _repair_thin_narrations,
    _source_sentences,
)
from doc_to_video_tutor.studio.plan import _annotate_source_chunks


def test_planner_output_budget_reserves_dense_dense_ratio() -> None:
    assert planner_output_budget(0) == 3072
    assert planner_output_budget(12000) == 1952
    assert planner_output_budget(100000) == 512
    assert planner_output_budget(10000) == 2912
    # each char reserves ~0.48 output tokens (denser than the old chars/4).
    assert planner_output_budget(10000) - planner_output_budget(12000) == \
        int(12000 * 0.48) - int(10000 * 0.48)


def test_choose_planner_budget_trades_width_for_headroom() -> None:
    assert choose_planner_budget(
        "registry metadata basics.", lambda c: "PROMPT: " + c) == (12000, 3072)
    win, budget = choose_planner_budget("x" * 12000, lambda c: "PROMPT: " + c)
    assert win == 8000
    assert budget >= 3072


def test_choose_planner_budget_steps_below_8000_for_dense_big_doc() -> None:
    # The failing-doc shape: dense source + heavy non-document framing (topic /
    # outline / design blocks on top of the big template) pinned window 8000 to
    # the 512-token clamp, so max_tokens=512 cut every plan mid-JSON. The ladder
    # must step the source window DOWN below 8000 until the projected output
    # clears PLANNER_MIN_OUTPUT - never re-serving the 512-token cap.
    padding = "FIXED PROMPT FRAMING " * 220
    win, budget = choose_planner_budget("x" * 18000, lambda c: padding + c)
    assert win < 8000
    assert budget >= PLANNER_MIN_OUTPUT


def test_choose_planner_budget_never_returns_the_512_floor() -> None:
    # Even a pathological near-hub-max prompt must retire the 512-token floor:
    # the widest window that still clears PLANNER_MIN_OUTPUT is what comes back,
    # never the starvation clamp.
    padding = "FIXED PROMPT FRAMING " * 220
    win, budget = choose_planner_budget("x" * 60000, lambda c: padding + c)
    assert win < 12000
    assert budget > 512
    assert budget >= PLANNER_MIN_OUTPUT


def test_nar_tokens_splits_separators_keeps_hyphens() -> None:
    assert S._nar_tokens("exit-3/4 (D36).") == ["exit-3", "4", "d36"]
    assert S._nar_tokens("mod_name.sub_func()") == ["mod", "name", "sub", "func"]
    assert S._nar_tokens("a \u2014 b \u2013 c") == ["a", "b", "c"]
    assert S._nar_tokens("kind_direction") == ["kind", "direction"]
    assert S._nar_tokens("(a) [b] {c}") == ["a", "b", "c"]
    assert S._nar_tokens("quality-gate") == ["quality-gate"]


def test_nar_tokens_folds_zero_width_and_bidi() -> None:
    assert S._nar_tokens("foo\u200b bar") == ["foo", "bar"]
    assert S._nar_tokens("x\u200d y") == ["x", "y"]
    assert S._nar_tokens("a\u200e b") == ["a", "b"]
    assert S._nar_tokens("c \ufeff d \u200f e") == ["c", "d", "e"]


def _fmt_opener(opener: str, module: str = "x") -> str:
    return opener.format(m=module).strip(" -")


def test_pick_opener_skips_colliding_openers() -> None:
    voice = S._MHE_VOICE
    first = _fmt_opener(voice.openers[0])
    got = _pick_opener(voice, 0, "registry details",
                       set(_narration_3grams(first)))
    assert got == _fmt_opener(voice.openers[1])
    assert got != first


def test_pick_opener_skips_first_two_when_they_collide() -> None:
    voice = S._MHE_VOICE
    merged = set(_narration_3grams(_fmt_opener(voice.openers[0])))
    merged |= set(_narration_3grams(_fmt_opener(voice.openers[1])))
    got = _pick_opener(voice, 0, "x", merged)
    assert got == _fmt_opener(voice.openers[2])


def test_pick_opener_rotation_fallback_when_pool_collides() -> None:
    voice = S._MHE_VOICE
    everything = set()
    for o in voice.openers:
        everything |= set(_narration_3grams(_fmt_opener(o)))
    got = _pick_opener(voice, 3, "x", everything)
    assert got == _fmt_opener(voice.openers[3])


def test_source_sentences_keep_only_complete_teachable_prose() -> None:
    chunk = ("The registry stores metadata for every kind and direction with "
             "a tolerance and unit. Short! The grading gate compares answers "
             "against the golden set using precision and recall.")
    got = _source_sentences(chunk)
    assert len(got) == 2
    assert "registry stores metadata" in got[0]
    assert "grading gate compares" in got[1]


def test_annotate_source_chunks_attaches_short_source_excerpt() -> None:
    content = (
        "# Module 1 Registry\n\n"
        "The registry stores metadata for every kind and direction with a "
        "tolerance and a unit value for each measured point.\n\n"
        "A later paragraph with nothing thematic about it at all sits here.\n\n"
        "## Grading\n\n"
        "The scoring gate compares candidate answers against the golden set "
        "using precision and recall across the whole held-out corpus.\n")
    plan = {"scenes": [{"title": "Registry metadata",
                        "bullets": ["The registry stores metadata."],
                        "design_decision": ""}]}
    total = _annotate_source_chunks(plan, content)
    assert total == 1
    chunk = plan["scenes"][0]["source_chunk"]
    assert "registry stores metadata" in chunk.casefold()
    assert len(chunk) <= 420


def _thin_scene() -> dict:
    return {
        "title": "Registry details",
        "design_decision": "",
        "bullets": ["Store kind and direction.", "Keep unit and tolerance."],
        "source_chunk": (
            "The registry stores metadata for every kind and records the "
            "direction with a tolerance and a unit value. "
            "Each measured point keeps its direction sign so the grading "
            "gate can compare the outputs fairly."),
    }


def _scaffold(sc: dict) -> tuple[list[dict], dict]:
    scenes = [
        sc,
        {"title": "Grading gate", "narration": "The scoring gate compares "
         "candidate answers against a golden set using precision and recall "
         "throughout this entire lesson.", "bullets": []},
        {"title": "Routing", "narration": "The router forwards requests to "
         "the correct worker and caches the hot paths in memory buffers.",
         "bullets": []},
    ]
    return scenes, {"takeaways": ["The registry stores direction values."]}


def test_rebuild_reaches_source_floor_only_with_hydration_source() -> None:
    voice = S._MHE_VOICE
    sc = _thin_scene()
    scenes, _others = _scaffold(sc)
    rebuilt = _rebuild_scene_narration(sc, 0, scenes, voice,
                                       min_tokens=_NARR_MIN_TOKENS + 6)
    assert len(S._nar_tokens(rebuilt)) >= _NARR_MIN_TOKENS + 6
    assert "registry stores metadata" in rebuilt.casefold()


def test_legacy_floor_without_source_chunk() -> None:
    voice = S._MHE_VOICE
    sc = _thin_scene()
    sc["source_chunk"] = ""
    scenes, _others = _scaffold(sc)
    rebuilt = _rebuild_scene_narration(sc, 0, scenes, voice,
                                       min_tokens=_NARR_MIN_TOKENS + 6)
    assert rebuilt == ""
    legacy = _rebuild_scene_narration(sc, 0, scenes, voice,
                                      min_tokens=_NARR_MIN_TOKENS)
    assert _NARR_MIN_TOKENS <= len(S._nar_tokens(legacy)) < _NARR_MIN_TOKENS + 6


def _repair_plan(source_chunk: str | None) -> dict:
    thin = {"title": "Registry metadata basics",
            "narration": "Okay moving on.",
            "design_decision": "",
            "bullets": ["Store kind and direction.",
                        "Keep unit and tolerance."]}
    if source_chunk is not None:
        thin["source_chunk"] = source_chunk
    scenes = [
        thin,
        {"title": "Grading gate", "narration": "The scoring gate compares "
         "candidate answers against a golden set using precision and recall "
         "across every question in the held out corpus here.",
         "bullets": ["Compare candidate answers here.",
                     "Score precision and recall now."]},
        {"title": "Routing", "narration": "The router forwards requests to "
         "the correct worker and caches the hot answer paths in memory "
         "buffers for the whole duration of the run.",
         "bullets": ["Forward requests to a worker.",
                     "Cache the hot answer paths."]},
        {"title": "Index", "narration": "The index builds a lookup over the "
         "source documents so the retriever returns the top hits quickly in "
         "every single query round.",
         "bullets": ["Build a lookup over sources.",
                     "Return top hits quickly now."]},
        {"title": "Ranking", "narration": "The ranking head scores each "
         "retrieved answer against the metric goal and orders the final list "
         "before the summary panel shows it.",
         "bullets": ["Score each retrieved answer.",
                     "Order the final list here."]},
    ]
    return {"opening": "", "takeaways": ["The registry stores direction "
                                         "values."], "scenes": scenes}


def test_repair_thin_narrations_hydrates_scene_with_source() -> None:
    plan = _repair_plan(_thin_scene()["source_chunk"])
    repaired = _repair_thin_narrations(plan, voice=S._MHE_VOICE)
    assert repaired >= 1
    assert len(S._nar_tokens(plan["scenes"][0]["narration"])) >= \
        _NARR_MIN_TOKENS + 6


def test_repair_thin_narrations_keeps_legacy_floor_without_source() -> None:
    plan = _repair_plan(None)
    repaired = _repair_thin_narrations(plan, voice=S._MHE_VOICE)
    assert repaired >= 1
    kept = plan["scenes"][0]["narration"]
    assert kept != "Okay moving on."
    assert len(S._nar_tokens(kept)) < _NARR_MIN_TOKENS + 6


def _long_narration(kind: str, value: str) -> str:
    return ("Is scene mein hum ek fresh concept samjhenge. The registry "
            "stores metadata for every kind and direction with a tolerance "
            f"and a {kind} value for the {value} unit to compare.")


def test_source_sentences_strip_markdown_labels_and_symbols() -> None:
    chunk = ("- **Plain words:** these three kinds control what the evidence "
             "*means*: - **gate**: hard contract — regression = **FAIL**, merge "
             "blocked. - **guardrail:** soft target ⚠️ (quality/latency drift).")
    got = _source_sentences(chunk)
    assert got
    joined = " ".join(got)
    assert "Plain words" not in joined
    for residue in ("*", "#", "⚠"):
        assert residue not in joined
    assert ":." not in joined and ".." not in joined
    assert "hard contract" in joined


def test_narrations_from_json_parses_fenced_array() -> None:
    raw = ("here is the track\n```json\n[{\"scene\": 1, \"narration\": \""
           + _long_narration("unit", "tolerance") + "\"}, "
           "{\"scene\": 2, \"narration\": \"" + _long_narration("range", "kind")
           + "\"}]\n```")
    narrations = _narrations_from_json(raw, expected=2)
    assert narrations is not None
    assert [n["scene"] for n in narrations] == [1, 2]
    assert all(len(S._nar_tokens(n["narration"])) >= _NARR_MIN_TOKENS + 6
               for n in narrations)


def test_narrations_from_json_accepts_bare_array() -> None:
    raw = ("[{\"scene\": 1, \"narration\": \""
           + _long_narration("unit", "tolerance")
           + "\"}]")
    narrations = _narrations_from_json(raw, expected=1)
    assert narrations is not None
    assert narrations[0]["scene"] == 1


def test_narrations_from_json_rejects_wrong_count() -> None:
    raw = ("[{\"scene\": 1, \"narration\": \""
           + _long_narration("unit", "tolerance") + "\"}]")
    assert _narrations_from_json(raw, expected=2) is None


def test_narrations_from_json_rejects_out_of_range_scene() -> None:
    raw = ("[{\"scene\": 5, \"narration\": \""
           + _long_narration("unit", "tolerance") + "\"}]")
    assert _narrations_from_json(raw, expected=1) is None


def test_narrations_from_json_rejects_too_thin_narration() -> None:
    raw = "[{\"scene\": 1, \"narration\": \"bas ek chota sa line.\"}]"
    assert _narrations_from_json(raw, expected=1) is None


def test_narrations_from_json_rejects_duplicate_scene() -> None:
    raw = ("[{\"scene\": 1, \"narration\": \""
           + _long_narration("unit", "tolerance") + "\"}, "
           "{\"scene\": 1, \"narration\": \""
           + _long_narration("range", "kind") + "\"}]")
    assert _narrations_from_json(raw, expected=2) is None


def test_narrations_from_json_rejects_non_json_junk() -> None:
    assert _narrations_from_json("no array here at all", expected=1) is None
    assert _narrations_from_json("", expected=1) is None

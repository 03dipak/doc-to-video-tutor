"""Spoken-script normalizer + TTS audit tests (studio.speech)."""

from __future__ import annotations

from doc_to_video_tutor import studio as S


def test_trim_narration_word_count_repairs_long_scene() -> None:
    scene = {
        "title": "Metric registry",
        "narration": " ".join(
            f"Typed metadata makes verdict number {n} mechanical."
            for n in range(18)),
        "bullets": ["Store direction, kind, tolerance, and unit."],
    }
    plan = {"scenes": [scene]}
    before = S.build_tts_script(plan, S._MHE_VOICE)
    assert any(item["code"] == "tts_too_long_critical"
               for item in before["audit"])
    assert S._trim_narration_word_count(plan, S._MHE_VOICE) == 1
    after = S.build_tts_script(plan, S._MHE_VOICE)
    assert not any(item["code"] == "tts_too_long_critical"
                   for item in after["audit"])

def test_speech_expand_splits_unmapped_snake_case_identifiers() -> None:
    out = S.speech_expand("Save baseline_report.json for the golden set",
                          S._MHE_VOICE.pronunciation_rules)
    flat = " ".join(out.split())
    assert "baseline report dot json" in flat
    assert "_" not in out


def test_audit_has_no_code_fragment_for_unmapped_identifier() -> None:
    plan = {"scenes": [{
        "title": "M1 Data and testset",
        "bullets": ["Freeze a known-good snapshot"],
        "narration": ("Yeh report ek baseline ke form mein save hota hai, "
                      "jiska naam baseline_report.json hota hai, aur wahi "
                      "golden_set_path pointer use karta hai."),
    }]}
    script = S.build_tts_script(plan, S._MHE_VOICE)
    assert not [f for f in script["audit"] if f["code"] == "tts_code_fragment"]


def test_audit_word_count_bands_separate_warn_from_critical() -> None:
    soft = {"role": "scene", "index": 1, "title": "Goldens",
            "spoken": " ".join(["word"] * 77)}
    critical = {"role": "scene", "index": 2, "title": "Registry",
                "spoken": " ".join(["word"] * 91)}
    soft_codes = {f.code for f in S.audit_tts_script([soft], S._MHE_VOICE)}
    critical_codes = {f.code for f in
                      S.audit_tts_script([critical], S._MHE_VOICE)}
    assert "tts_too_long" in soft_codes
    assert "tts_too_long_critical" not in soft_codes
    assert "tts_too_long_critical" in critical_codes


def test_speech_expand_longest_first_and_boundary() -> None:
    rules = S._MHE_VOICE.pronunciation_rules
    out = S.speech_expand(
        "M1 Data & testset, Active.json, plan.json, Gate = hard fail, "
        "1/(n+1) loop, rasm1", rules)
    # Whitespace-normalised: the subject here is longest-match-first ordering and
    # the word boundary below, not the incidental double space left by padded
    # replacements. `_speak_math_symbols` now collapses runs of spaces, so
    # pinning the exact spacing would fail on formatting rather than behaviour.
    assert "module one Data and testset" in " ".join(out.split())
    assert "active dot json" in out
    assert "plan dot json" in out
    assert "equals" in out
    assert "one over n plus one" in out
    assert "rasm1" in out  # 'm1' must not fire inside 'rasm1' (word boundary)


def test_speech_expand_repairs_active_jso_typo() -> None:
    for voice in (S._MHE_VOICE, S._ENGLISH_VOICE):
        out = S.speech_expand("active.jso", voice.pronunciation_rules)
        assert "active dot json" in out
        assert "active.jso" not in out


    out = S.speech_expand(
        "schema_version baseline_id run_suite llm_eval_gate live_eval_nightly",
        S._MHE_VOICE.pronunciation_rules)
    flat = " ".join(out.split())
    assert "schema version" in flat
    assert "baseline id" in flat
    assert "run suite" in flat
    assert "l l m eval gate" in flat
    assert "live eval nightly" in flat
    assert not any(token in flat for token in (
        "schema_version", "baseline_id", "run_suite", "llm_eval_gate",
        "live_eval_nightly"))


def test_speech_expand_digit_words_word_boundary() -> None:
    out = S.speech_expand("Exit 3. M1 vs D36 keep 7 modules.",
                          S._MHE_VOICE.pronunciation_rules)
    flat = " ".join(out.split())
    assert "Exit three" in flat
    assert "module one" in flat     # M1 untouched by the digit rule
    assert "D36" in flat            # two+ digits untouched
    assert "seven" in flat
    assert " 1 " not in f" {flat} "

def test_audit_teaching_claim_accepts_two_spoken_bullets() -> None:
    plan = {"scenes": [{
        "title": "2. The metric registry", "index": 1,
        "bullets": ["Kind (gate/guardrail/info)",
                    "Direction (higher/lower)", "Tolerance"],
    }]}
    clip = {
        "role": "scene", "index": 1, "title": "2. The metric registry",
        "section": "kind",
        "spoken_title": "The metric registry",
        "narration": "Is scene mein hum ek fresh concept samjhenge. "
                     "Yahan hum ek important piece samjhte hain. "
                     "Kind (gate/guardrail/info). Direction (higher/lower). Tolerance.",
        "spoken": "The metric registry. "
                  "Is scene mein hum ek fresh concept samjhenge. "
                  "Yahan hum ek important piece samjhte hain. "
                  "Kind gate/guardrail/info. Direction higher/lower. Tolerance.",
    }
    findings = S.audit_tts_script([clip], S._MHE_VOICE, plan)
    assert not {f.code for f in findings if f.severity == "FAIL"}

def test_strip_slide_meta_removes_renderer_leaks() -> None:
    text, hits = S.strip_slide_meta(
        "Slide pe yeh points likhe hain: Metric types every metric. "
        "Aur slide ke exact points: Gate runs offline.", S._MHE_LEAK_PHRASES)
    assert hits >= 2
    assert "points likhe hain" not in text
    assert "Metric types every metric" in text


def test_repeated_title_collapsed() -> None:
    text, removed = S.collapse_repeated_title(
        "The metric registry. The metric registry is the point of it.", "The metric registry")
    assert removed == 1
    assert text.count("The metric registry") == 1


def test_repair_binds_orphan_punctuation() -> None:
    text, _ = S.repair_speech_punctuation(
        "x. ? is a. to iska matlab kya hua? : run gates.")
    assert "? :" not in text
    assert text.endswith(".")


def test_build_tts_script_contract() -> None:
    plan = {
        "title": "gates",
        "opening": "aaj gates ke baare mein.",
        "scenes": [
            {"section": "How does it work", "title": "M1 Data & testset",
             "narration": "Is scene mein baat karte hain M1 Data testset "
                          "ke baare mein - ye ek goldens baseline hai. "
                          "Aur slide ke exact points: Gate runs offline.",
             "bullets": ["Gate runs offline"]},
            {"section": "Takeaway", "title": "M2 The registry",
             "narration": "Chaliye dekhte hain M2 The registry kaise "
                          "kaam karta hai. Registry types every metric.",
             "bullets": ["Registry types every metric"]},
        ],
        "takeaways": ["Goldens are stores", "Events are a lie"],
    }
    script = S.build_tts_script(plan, S._MHE_VOICE)
    clips = script["clips"]
    assert len(clips) == 3
    assert clips[0]["role"] == "scene" and clips[0]["index"] == 1
    assert clips[-1]["role"] == "final"
    assert "module one" in clips[0]["spoken"]
    assert "slide ke exact points" not in clips[0]["spoken"]
    assert clips[-1]["spoken"].startswith("Key takeaways.")
    assert S._MHE_VOICE.pronunciation_rules
    assert all("M1" not in c["spoken"] for c in clips)
    assert script["rate"] == "-8%"


def test_audit_flags_slide_meta_and_symbols() -> None:
    clip = {"role": "scene", "index": 1, "title": "X",
            "spoken": "Slide pe yeh likha hai _x_y compiled",
            "slide_meta_hits": 1}
    findings = S.audit_tts_script([clip], S._MHE_VOICE)
    codes = {f.code for f in findings}
    assert "tts_code_fragment" in codes
    assert "tts_slide_meta_leak" in codes


def test_render_script_txt_is_exact() -> None:
    script = {
        "voice_profile": "mhe-mix", "tts_voice": "hi-IN-SwaraNeural",
        "rate": "-8%", "pitch": "+0Hz", "volume": "+0%", "audit": [],
        "clips": [{"role": "scene", "index": 1, "section": "What is this",
                   "title": "M1 Data", "narration": "n", "spoken_title": "module one Data",
                   "spoken": "module one Data. yahan hum goldens dekhenge.",
                   "word_count": 7}],
    }
    txt = S.render_script_txt(script)
    assert "SPOKEN: module one Data. yahan hum goldens dekhenge." in txt
    assert "module one Data" in txt


def test_arrow_and_takeaway_punctuation_expanded() -> None:
    expanded = S.speech_expand("measurement→decision framing; verdict",
                               S._MHE_VOICE.pronunciation_rules)
    assert "measurement to decision framing, verdict" in " ".join(expanded.split())


def test_audit_blocks_voice_unset() -> None:
    bare = S.NarrationVoice(
        name="bare", openers=(), closers=(), dd_leads=(),
        points_leads=(), narr_heads=())
    findings = S.audit_tts_script([], bare)
    assert any(f.code == "tts_voice_unset" and f.severity == "FAIL"
               for f in findings)


def test_audit_blocks_corrupt_mixed_script() -> None:
    clip = {"role": "scene", "index": 6, "title": "gate", "spoken":
            "गेट एक ठोस बट्याड़ है, गेडरेनल एक мян-मान verdict deta hai."}
    findings = S.audit_tts_script([clip], S._MHE_VOICE)
    assert any(f.code == "tts_text_corrupted" and f.severity == "FAIL"
               for f in findings)


def test_audit_blocks_symbol_heavy_residue() -> None:
    clip = {"role": "scene", "index": 4, "title": "baseline", "spoken":
            "1. the snapshot measurement→decision loop. See active.json."}
    findings = S.audit_tts_script([clip], S._MHE_VOICE)
    assert any(f.code == "tts_symbol_heavy" and f.severity == "FAIL"
               for f in findings)


def test_audit_blocks_slide_fragment_sentence() -> None:
    clip = {"role": "scene", "index": 4, "title":
            "1: Baseline snapshot & compare (measurement→decision framing)",
            "spoken": "Baseline snapshot compare. Run the goldens now."}
    findings = S.audit_tts_script([clip], S._MHE_VOICE)
    assert any(f.code == "tts_sentence_fragment" and f.severity == "FAIL"
               for f in findings)


def test_audit_blocks_unknown_halucinated_token() -> None:
    plan = {"title": "gates", "takeaways": [], "scenes": [
        {"title": "gate verdicts", "narration": "x", "bullets": ["verdict"]}]}
    clip = {"role": "scene", "index": 1, "title": "gate verdicts", "spoken":
            "yahan hum Prahlad ka role samjhenge. Latin script only."}
    findings = S.audit_tts_script([clip], S._MHE_VOICE, plan)
    assert any("Prahlad" in f.message and f.code == "tts_unknown_token"
               and f.severity == "FAIL" for f in findings)


def test_audit_blocks_transition_only_teaching_claim() -> None:
    clip = {"role": "scene", "index": 3, "title": "Concept by concept", "spoken":
            "Concept by concept. Ab aage badhte hain concept by concept ki taraf."}
    findings = S.audit_tts_script([clip], S._MHE_VOICE)
    assert any(f.code == "tts_required_concept_missing" and f.severity == "FAIL"
               for f in findings)


def test_audit_word_count_blockers() -> None:
    short = {"role": "scene", "index": 1, "title": "", "spoken": "a b c d"
             " e f g h i j k l m n o p q r s"}
    long = {"role": "scene", "index": 2, "title": "", "spoken": " ".join(
        ["word"] * 95)}
    kinds = {f.code for f in S.audit_tts_script([short, long], S._MHE_VOICE)}
    assert "tts_too_short_critical" in kinds
    assert "tts_too_long_critical" in kinds
    assert any(f.code == "tts_too_short_critical" and f.severity == "FAIL"
               for f in S.audit_tts_script([short], S._MHE_VOICE))


def test_audit_soft_warns_for_23_and_24_word_scene() -> None:
    clips = [
        {"role": "scene", "index": index, "title": "",
         "spoken": " ".join(["word"] * count)}
        for index, count in ((6, 24), (9, 23))
    ]
    findings = S.audit_tts_script(clips, S._MHE_VOICE)
    short = [finding for finding in findings
             if finding.code == "tts_too_short"]
    assert len(short) == 2
    assert all(finding.severity == "WARN" for finding in short)
    assert not any(finding.code in {"tts_too_short_critical",
                                    "tts_too_long_critical"}
                   for finding in findings)


    clip = {"role": "scene", "index": 1, "title": "The metric registry",
            "spoken_title": "The metric registry",
            "spoken": "The metric registry. Yahan hone vaala comparison "
                      "registry ke metadata pe depend karta hai aur isse verdict "
                      "mechanical ho jaata hai."}
    findings = S.audit_tts_script([clip], S._MHE_VOICE)
    assert not any(f.code == "tts_sentence_fragment" and f.severity == "FAIL"
                   for f in findings)


def test_audit_digit_title_head_is_not_a_fragment() -> None:
    clip = {"role": "scene", "index": 2, "title": "Is 02 me baselines + compare",
            "spoken_title": "Is 02 me baselines plus compare",
            "spoken": "Is 02 me baselines plus compare. Ab chaliye isse detail "
                      "mein dekhte hain. Baseline stores known-good snapshot."}
    findings = S.audit_tts_script([clip], S._MHE_VOICE)
    assert not any(f.code == "tts_sentence_fragment" and f.severity == "FAIL"
                   for f in findings)


def test_speech_expand_py_file_token() -> None:
    out = S.speech_expand("Exit code from compare.py", S._MHE_VOICE.pronunciation_rules)
    assert "compare dot py" in " ".join(out.split())
    assert ".py" not in out


def test_audit_still_blocks_non_head_fragment() -> None:
    clip = {"role": "scene", "index": 2, "title": "The metric registry",
            "spoken_title": "The metric registry",
            "spoken": "The metric registry. Metric registry."}
    findings = S.audit_tts_script([clip], S._MHE_VOICE)
    assert any(f.code == "tts_sentence_fragment" and f.severity == "FAIL"
               for f in findings)


def test_thin_narration_rebuilt_deterministically() -> None:
    plan = {"title": "gates", "takeaways": [], "scenes": [
        {"title": "Baseline snapshot means the compare loop",
         "narration": "Is 02 me baselines plus compare.",
         "design_decision": "baseline first so that every gate has a real "
                            "reference point to judge the new run against",
         "bullets": ["each gate registers its own baseline before judging",
                     "a compare runs the new metric against that snapshot"]},
    ]}
    assert S._thin_narration(plan["scenes"][0]["narration"],
                             plan["scenes"][0], S._MHE_VOICE)
    n = S._repair_thin_narrations(plan, voice=S._MHE_VOICE)
    assert n == 1
    rebuilt = plan["scenes"][0]["narration"]
    assert len(S._nar_tokens(rebuilt)) >= 18
    assert not S._thin_narration(rebuilt, plan["scenes"][0], S._MHE_VOICE)


def test_hydrate_spoken_fragments_wraps_bare_bullet() -> None:
    # Scene-2 class: a 6-word bullet whose words are 4/6 the title's words.
    title = "Baseline snapshot & compare — the core loop"
    bare = "Aaj hum dekhenge. Compare the results against the baseline."
    out = S._hydrate_spoken_fragments(bare, title, "", S._MHE_VOICE, 1)
    # the bullet stays verbatim (A2.13 spoken-teaching contract) ...
    assert "Compare the results against the baseline" in out
    # ... but it is no longer spoken as a bare title-echo fragment.
    frag = next(s for s in S.sentences(out) if "against the baseline" in s)
    assert frag != "Compare the results against the baseline."
    assert not S._is_slide_fragment(frag, title)


def test_build_tts_script_hydrates_and_gate_passes() -> None:
    plan = {"title": "gates", "opening": "aaj gates ke baare mein.",
            "takeaways": [], "scenes": [
        {"section": "How", "title": "Baseline snapshot & compare — the core loop",
         "narration": "Aaj hum baseline ka compare loop dekhenge. "
                      "Run the goldens over the current code → produce a report. "
                      "Compare the results against the baseline. "
                      "Deliverables include snapshot and report.",
         "bullets": ["Run the goldens over the current code → produce a report",
                     "Compare the results against the baseline",
                     "Deliverables include snapshot and report"]}]}
    clips = S.build_tts_script(plan, S._MHE_VOICE)["clips"]
    spoken = clips[0]["spoken"]
    assert "against the baseline" in spoken
    # no spoken sentence may present as a bare title/bullet fragment anymore
    assert all(not S._is_slide_fragment(s, clips[0]["title"])
               for s in S.sentences(spoken))
    findings = S.audit_tts_script(clips, S._MHE_VOICE, plan)
    assert not any(f.code == "tts_sentence_fragment" and f.severity == "FAIL"
                   for f in findings)


def test_voices_have_spoken_bullet_leads() -> None:
    # >=3 spoken words per lead: a 3-word carrier dissolves the 60%-overlap
    # fragment threshold for every realistic 2-6 word sentence.
    for voice in (S._MHE_VOICE, S._ENGLISH_VOICE):
        assert len(voice.bullet_leads) >= 3
        for lead in voice.bullet_leads:
            assert len(lead.split()) >= 3


def test_json_payload_variants_are_both_valid_json() -> None:
    import json as _json

    scene = {"json_snippet": (
        '{\n  "schema_version": 1,\n  "baseline_id": "v1.2.0",\n'
        '  "path": "eval/baselines/v1.2.0.json"\n}')}
    candidates = S._json_payload_candidates(scene)
    assert len(candidates) == 2
    assert len(candidates[0]) == 5
    assert len(candidates[1]) == 1
    for variant in candidates:
        _json.loads("\n".join(variant))


def test_json_payload_candidates_empty_without_snippet() -> None:
    assert S._json_payload_candidates({}) == []
    assert S._json_payload_candidates({"json_snippet": "  "}) == []


def test_module_labels_cover_the_whole_scene_range() -> None:
    for index, word in ((1, "one"), (6, "six"), (7, "seven"), (9, "nine"),
                        (12, "twelve")):
        for voice in (S._MHE_VOICE, S._ENGLISH_VOICE):
            out = S.speech_expand(f"M{index}", voice.pronunciation_rules)
            assert out == f"module {word}", (index, voice.name, out)
    # Word-boundary matching must keep m1 from firing inside a longer token.
    assert S.speech_expand("rasm1", S._MHE_VOICE.pronunciation_rules) == "rasm1"


def test_clip_title_trims_on_a_word_boundary() -> None:
    assert S.clip_title("Short title") == "Short title"
    trimmed = S.clip_title(
        "The 11-step precedence — structure before values, FAIL over REVIEW")
    assert trimmed == "The 11-step precedence — structure before"
    assert not trimmed.endswith("va")
    assert len(trimmed) <= 44


def test_deck_text_always_declares_an_explicit_font_face() -> None:
    """Regression: the deck silently rendered in the theme font (Calibri).

    Neither text helper set `font.name`, so 132 of 171 paragraphs inherited the
    theme's minor font instead of Arial, and the one code panel in the
    enrichment blocks rendered proportional instead of Courier New. Only
    paragraphs with visible text matter: the empty placeholder paragraph inside
    an autoshape carries no glyphs.
    """
    import re
    import zipfile
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from doc_to_video_tutor.studio.pptx import build_pptx

    plan = {
        "title": "Regression gates",
        "opening": "A deterministic gate",
        "takeaways": ["Exit codes are the verdict language"],
        "scenes": [{
            "section": "What Is This",
            "title": "Metric registry",
            "narration": "The registry types every metric so verdicts stay mechanical.",
            "bullets": ["Store direction, kind, tolerance, and unit."],
            "design_decision": "Types make the verdict mechanical.",
            "code_snippet": "compare.py --baseline active.json",
            "code_context": "Run the comparison against the pointer.",
            "visual_diagram": "[Metric] ---> [Verdict]",
            "status_badges": [
                {"code": "0", "label": "PASS", "state": "PASS"},
                {"code": "1", "label": "FAIL", "state": "FAIL"},
            ],
            "json_snippet": '{\n  "schema_version": 1,\n  "path": "b.json"\n}',
        }],
    }
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "deck.pptx"
        build_pptx(plan, out)
        faces: set[str] = set()
        text_without_face = 0
        with zipfile.ZipFile(out) as archive:
            for name in archive.namelist():
                if not re.match(r"ppt/slides/slide\d+\.xml$", name):
                    continue
                xml = archive.read(name).decode("utf-8")
                faces.update(re.findall(r'<a:latin typeface="([^"]+)"', xml))
                for para in re.findall(r"<a:p>.*?</a:p>", xml, re.S):
                    body = "".join(re.findall(r"<a:t>(.*?)</a:t>", para, re.S))
                    if body.strip() and "<a:latin " not in para:
                        text_without_face += 1
    assert text_without_face == 0
    assert faces <= {"Arial", "Courier New"}, faces
    assert {"Arial", "Courier New"} <= faces


def _timings(words: list[tuple[str, float]]) -> list[dict]:
    return [{"text": text, "start": start, "duration": 0.3}
            for text, start in words]


def test_bullet_start_times_match_spoken_words() -> None:
    from doc_to_video_tutor.studio.video import _bullet_start_times

    timings = _timings([("frozen", 1.0), ("golden", 1.3), ("report", 1.6),
                        ("store", 1.9), ("delta", 5.0), ("nikalte", 5.3)])
    # A reveal starts when the bullet's *first* word is spoken, so the second
    # bullet resolves to 5.0 ("delta"), not to 5.3 ("nikalte"): anchoring on the
    # single longest token used to start the reveal a word late.
    starts = _bullet_start_times(["Frozen golden report", "delta nikalte hain"],
                                 timings)
    assert starts == [1.0, 5.0]


def test_bullet_start_times_prefer_the_window_over_an_early_mention() -> None:
    """A token the narrator mentions in passing must not win the match.

    Mirrors scene 6 of the real lesson, where the narrator sets the topic up in
    prose before actually speaking the bullet: "structure before values not
    values before structure because the engine checks structure before
    comparing values".
    """
    from doc_to_video_tutor.studio.video import _bullet_start_times

    timings = _timings([("structure", 1.0), ("before", 1.4), ("values", 1.8),
                        ("not", 2.2), ("values", 2.5), ("before", 2.8),
                        ("structure", 3.2), ("because", 3.6), ("the", 4.0),
                        ("engine", 6.0), ("checks", 6.3), ("structure", 6.6),
                        ("before", 6.9), ("comparing", 7.2), ("values", 7.6)])
    starts = _bullet_start_times(
        ["engine checks structure before comparing values"], timings)
    # The bullet begins at 6.0, not at the 1.0 mention in the setup clause.
    assert starts == [6.0]


def test_bullet_start_times_report_unmatched_bullets() -> None:
    from doc_to_video_tutor.studio.video import _bullet_start_times

    timings = _timings([("alpha", 1.0), ("beta", 1.4)])
    assert _bullet_start_times(["nothing spoken here"], timings) == [-1.0]
    assert _bullet_start_times(["a b c"], timings) == [-1.0]
    # No timings at all yields no result rather than a bogus time.
    assert _bullet_start_times(["alpha beta"], []) == []
    assert _bullet_start_times([], timings) == []


def test_variant_durations_follow_speech_and_conserve_duration() -> None:
    from doc_to_video_tutor.studio.video import _variant_durations

    timings = _timings([("snapshot", 2.0), ("compare", 6.0), ("tolerance", 10.0)])
    parts = _variant_durations(20.0, 4,
                              ["frozen snapshot", "compare the metrics",
                               "tolerance gate"], timings)
    assert len(parts) == 4
    assert abs(sum(parts) - 20.0) < 1e-6
    # The first reveal waits for the first bullet, not a fixed 15%.
    assert abs(parts[0] - 2.0) < 1e-6
    assert all(p > 0 for p in parts)


def test_variant_durations_fall_back_without_usable_timings() -> None:
    from doc_to_video_tutor.studio.video import _variant_durations

    even = _variant_durations(20.0, 4, ["a b", "c d", "e f"], [])
    assert len(even) == 4
    assert abs(sum(even) - 20.0) < 1e-6
    # Unmatched bullets, no bullets, and too few bullets all degrade identically.
    timings = _timings([("zzz", 1.0)])
    assert _variant_durations(20.0, 4, ["qqq www"], timings) == even
    assert _variant_durations(20.0, 4, None, timings) == even
    assert _variant_durations(20.0, 4, ["a b"], timings) == even
    assert _variant_durations(20.0, 1, ["a b"], timings) == [20.0]


def test_code_line_colors_preserve_text_and_never_invent() -> None:
    """Pygments may only recolour existing characters, never add or drop any."""
    from doc_to_video_tutor.studio.slides import _code_line_colors

    lines = ["def compare(base):", "    return 1  # done"]
    for hint in ("compare.py", "", "unknown.zzz", "noextension"):
        rebuilt = ["".join(token for token, _ in parts)
                   for parts in _code_line_colors(lines, hint)]
        assert rebuilt == lines, hint


def test_code_line_colors_use_the_extension_and_degrade_flat() -> None:
    from doc_to_video_tutor.studio.slides import _code_line_colors

    coloured = _code_line_colors(["def compare(base):"], "compare.py")
    assert len(coloured[0]) > 1
    assert any(colour != (140, 200, 255) for _, colour in coloured[0])
    # A hint with no recognisable extension degrades to one flat run.
    assert _code_line_colors(["def compare(base):"], "noextension") == [
        [("def compare(base):", (140, 200, 255))]]


def test_bullet_start_times_expands_bullets_into_spoken_form() -> None:
    """A digit-bearing bullet must match a stream that spells the digit out.

    The word stream comes from the provider, which segments the post-expansion
    spoken text. Before canonicalisation a bullet holding `3` produced the token
    "3" and could never match a stream holding "three" - silently, at any
    weighting, because no score function can compare unequal strings.
    """
    from doc_to_video_tutor.studio.video import _bullet_start_times, _profile_rules

    script = {"voice_profile": "mhe-mix"}
    rules = _profile_rules(script)
    assert rules, "the mhe-mix profile must resolve pronunciation rules"

    # The stream as edge-tts actually returns it for a spoken "... exit 3 ...".
    timings = [{"text": "exit", "start": 1.0},
               {"text": "three", "start": 1.4},
               {"text": "wins", "start": 1.9}]
    bullet = ["the 3 chip"]

    assert _bullet_start_times(bullet, timings) == [-1.0]
    assert _bullet_start_times(bullet, timings, rules) == [1.4]


def test_profile_rules_is_safe_for_unknown_profile() -> None:
    from doc_to_video_tutor.studio.video import _profile_rules

    assert _profile_rules({}) is None
    assert _profile_rules({"voice_profile": "no-such-voice"}) is None


def test_write_webvtt_groups_words_and_advances_by_clip_duration(
        tmp_path, monkeypatch) -> None:
    """Captions must be readable phrases on the real timeline, not one word each."""
    from pathlib import Path

    from doc_to_video_tutor.studio import video

    # Two clips; clip 1's last word ends at 1.0 s but the clip is 5.0 s long.
    # Advancing on word offsets instead of clip duration would drop clip 2's
    # cues back inside clip 1.
    timings = [
        [{"text": "alpha", "start": 0.1, "duration": 0.4},
         {"text": "beta", "start": 0.6, "duration": 0.4}],
        [{"text": "gamma", "start": 0.1, "duration": 0.4},
         {"text": "delta", "start": 0.6, "duration": 0.4}],
    ]
    monkeypatch.setattr(video, "_clip_seconds", lambda _p: 5.0)
    out = tmp_path / "cap"
    cues = video._write_webvtt(out, {}, timings, [Path("a.mp3"), Path("b.mp3")],
                              pause=3.0)
    assert cues == 2
    vtt = (tmp_path / "cap.vtt").read_text(encoding="utf-8")
    assert vtt.startswith("WEBVTT\n")
    assert "," not in vtt, "WebVTT uses a dot before milliseconds"
    # Both words of a clip form one cue, not one cue per word.
    assert "alpha beta" in vtt
    # Clip 2 starts after clip 1's real duration plus the pause (5 + 3 = 8 s).
    assert "00:00:08.100" in vtt
    starts = [ln for ln in vtt.splitlines() if "-->" in ln]
    assert len(starts) == 2


def test_fused_particle_tokens_catches_real_fusion_without_false_positives() -> None:
    """A lowercase particle+word fusion must not slip past the capital-only gate."""
    from doc_to_video_tutor.studio.speech import _fused_particle_tokens

    # "deterministic" is attested vocabulary elsewhere in the narration, which is
    # what makes the split safe to report.
    known = frozenset({"deterministic", "offline", "gate", "evaluation",
                       "reason", "yahi", "hai"})
    spoken = ("Is scene mein hum ek fresh concept samjhenge. Reason yahi hai "
              "kideterministic offline gate vs live evaluation because the gate "
              "runs a fully deterministic evaluator.")
    assert _fused_particle_tokens(spoken, known) == {
        "kideterministic": "ki deterministic"}

    # Ordinary English that splits attractively must stay clean, otherwise the
    # rule cries wolf on every technical lesson.
    for clean in ("We killed the stale cache and together we shipped it.",
                  "The takeaway is deterministic and reproducible.",
                  "Keep the cache warm and the latency low."):
        assert _fused_particle_tokens(clean, known) == {}, clean

    # A token whose tail is NOT attested is not a fusion we can claim.
    assert _fused_particle_tokens("Reason yahi hai kizarbitrary.", known) == {}


def test_fusion_is_reported_as_a_warning_not_an_audit_failure() -> None:
    """Below the auto-repair bar the finding must surface, not mutate the script."""
    from doc_to_video_tutor.studio.speech import TtsFinding, _fused_particle_tokens

    assert TtsFinding("tts_token_fusion", "WARN", "x").severity == "WARN"
    # And the rule itself never rewrites text: it only reports a candidate split.
    known = frozenset({"deterministic"})
    assert _fused_particle_tokens("kideterministic", known)

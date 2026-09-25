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

def test_speech_expand_longest_first_and_boundary() -> None:
    rules = S._MHE_VOICE.pronunciation_rules
    out = S.speech_expand(
        "M1 Data & testset, Active.json, plan.json, Gate = hard fail, "
        "1/(n+1) loop, rasm1", rules)
    assert "module one Data  and  testset" in out
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

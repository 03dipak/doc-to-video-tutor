# Reviewer brief — narration safety and language-aware voice layer

Condensed from `docs/LLD-narration-voice.md` (~1000 lines). Self-contained: you
do not need the full LLD to review this. Section pointers to the full document
are given as `LLD §n` throughout.

**What I want from you:** the five questions in the last section. Everything
else is context so you can judge those answers against a real design.

---

## 1. What the system is

`doc-to-video-tutor` turns a technical Markdown document into a narrated lesson:
a PPTX deck plus an MP4, in a voice-matched style (currently Hinglish-leaning
technical Hindi-English). Input is one or more `.md` files.

The central design rule, and the one most worth attacking:

> **The model proposes; deterministic code disposes.** The LLM emits a lesson
> plan. Every safety, grounding, length, and consistency property is then
> enforced, repaired, or rejected by code. No property depends on the model
> behaving.

The consequence is that defects should be *caught*, not *prevented by prompt
politeness*. A defective step is meant to produce an auditable rejected build,
never a silently-wrong one.

**Non-goals:** no avatar/video generation, no cloud TTS, no model-based
grounding or fact-checking, no renderer rewrite. Free/open-source only.

## 2. Module map (LLD §4)

| Module | Role |
|---|---|
| `util.py` | document loading; **strips source citation blocks before the LLM sees them** |
| `llm.py` | plan generation, retries, planner budget |
| `schema.py` | Pydantic `LessonPlan` / `SlideScene` / `TTSClip` |
| `plan.py` | concept extraction, scene frame, deterministic repair, pagination, enrichment |
| `narration.py` | Layer A safety: tokenizer, n-grams, repair chain, enforcer, rebuild |
| `voice.py` | voice profiles, pronunciation rules |
| `speech.py` | spoken-form normalization + pre-audio audit |
| `validate.py` | plan, TTS, and media gates |
| `slides.py` / `pptx.py` | Pillow slide images; python-pptx deck |
| `video.py` | TTS synthesis, loudness, reveal sync, MP4 assembly |
| `cli.py` | `build` / `render` / `verify` / `review` / `tts-check` |

## 3. Two layers (LLD §5)

**Layer A — language-agnostic, token-only, no model.** The canonical tokenizer
(`_nar_tokens`) NFKC-normalises, folds zero-width/bidi/whitespace to ASCII
spaces, treats `/ _ ( ) [ ] { }` and dashes as word breaks, strips punctuation,
and lowercases. Hyphens are *kept* inside a token, so `quality-gate` stays one
token and prose/code remain symmetric. Everything else in Layer A is built on
that token stream: trigrams, the protected allowlist, the repair chain, the
enforcer.

Layer A is intentionally degenerate for unsegmented scripts (CJK/Hangul) — it
warns and asks for transliteration rather than pretending to segment.

**Layer B — the selected voice profile**: rate, pitch, volume, openers, closers,
`dd_leads`, Hinglish particles, and pronunciation rules (`M1`–`M12`).

## 4. Narration contract (LLD §7.5)

Two rules that everything else depends on:

1. The pipeline does **not** read slide bullets into narration. The slide
   carries the bullets; the narration carries the explanation and the decision.
2. `_has_teaching_claim` requires a scene's spoken track to carry at least two
   distinct scene bullets — so a scene cannot pass as "taught" while saying
   nothing about its own content.

## 5. Repair chain (LLD §7.5)

Deterministic and idempotent; ordering matters because text-changing repairs run
before the enforcer's final pass.

```
_dedupe_plan_bullets                 exact duplicate bullets across scenes
_prune_bullet_takeaway_echo          bullets near-copying a takeaway
_sanitize_design_decisions           drop degenerate/duplicated decision text
_source_design_decisions             fill empties from real source contrast sentences
_dedupe_narration_templates
  ├ _drop_repeated_filler            within-narration sentence dedup
  ├ cleanup_rules                    collapse known stall skeletons, re-attach content
  └ _drop_shared_narration_sentences cross-scene whole-sentence dedup
_deepen_narrations                   speak each scene's decision once; append takeaways
_enforce_unique_narration_trigrams   sentence-aware enforcer (pass 1)
_repair_unsafe_narrations            deterministic rebuild for still-unsafe scenes
_repair_thin_narrations              deterministic rebuild driven by the audio gate
_trim_narration_word_count           drop trailing sentences above the spoken ceiling
```

`_rebuild_scene_narration` (LLD §7.7) assembles `opener + design_decision lead +
distinct bullets`, then, **only if still under the token floor**, appends real
sentences from the scene's annotated `source_chunk`. Every appended fragment is
3-gram-checked against everything already spoken, so a rebuild cannot introduce a
new banned repeat. Bullets whose 3-grams collide with earlier speech are
**skipped**, not force-fitted.

## 6. Pre-audio audit (LLD §9.1)

| Severity | Condition |
|---|---|
| FAIL | `tts_voice_unset`, `tts_text_corrupted` (mixed script / replacement / control char), `tts_slide_meta_leak`, `tts_symbol_heavy`, `tts_punctuation_error`, `tts_sentence_fragment`, `tts_unknown_token`, `tts_required_concept_missing`, `tts_too_short_critical` (< 20 words), `tts_too_long_critical` (> 90 words) |
| WARN | `tts_too_short` (< 25), `tts_too_long` (> 70), `tts_long_sentence` (> 30-word sentence), `tts_code_fragment`, `tts_title_repeated` |

Any FAIL blocks TTS: the CLI writes `*.rejected.tts_script.json`, prints the
findings, and exits 1 **before the provider is called**.

Note the deliberate asymmetry: short narration is a failure, long narration is
trimmed. Under-running is a content defect; over-running is a pacing defect.

## 7. Planning and media (LLD §10, §14)

- **Concept frame** (§10.1): scene count is derived from source headings and must
  equal the plan's own `scene_target` (range 5–12). There is deliberately no
  fixed scene band.
- **Pagination** (§10.3): scenes exceeding a density threshold split, and
  `bullet_pages` must match `bullets` per scene.
- **Enrichment**: source-grounded blocks — status badges, a canonical-pointer
  JSON payload, and a guarded value table. The value table stays **empty** when
  the source carries its values only inside citation blocks that are stripped
  pre-LLM; inventing numbers there would be worse than an empty panel.
- **Reveal sync**: `edge-tts` `WordBoundary` events are captured while
  streaming, giving per-word offsets for free. Per-bullet reveals start at the
  first-spoken offset, falling back to an even split when a bullet's words are
  not located. A missing timing degrades the animation; it never fails the build.
- Timings are written to a `<name>.word_timings.json` sidecar, not into
  `tts_script.json`, because that file is the exact provider input written
  *before* synthesis (LLD §19.6).

## 8. TTS provider risk (LLD §22.1)

`edge-tts` wraps a **consumer reading endpoint, not a published API**. It can
change without notice. Current mitigations: pinned to `edge-tts==7.2.8` (was a
`>=6.1` floor), and a `tts-check` CI probe that fails on a transport error, an
empty clip, missing `WordBoundary` events, or an implausible offset.

**Still open:** a backend seam (offline Piper is the candidate) and per-clip
retry. A fallback must never substitute silently, because the reproducibility
contract pins provider, voice, rate, pitch, and volume in the plan.

## 9. Measured results (mod03_gates_v021/v022)

| Metric | Value |
|---|---|
| Scenes / clips | 9 scenes, 10 clips |
| Loudness | −16.0 LUFS (target −16 ±2) |
| True peak | −1.5 dBTP, no clipping |
| Spoken duration | 2.8 min vs 4.0 min target (−30%) |
| Word timings | 383 over 10 clips; 9/9 scenes aligned against real clip durations |
| Banned repeats | 0 |
| Slide-meta leaks | 0 |
| `bullet_pages` == `bullets` | 9/9 |
| Gate verdict | `review` exit 0, `VERDICT: PASS` |

## 10. Open defects I want reviewed

| # | Defect | Why it matters |
|---|---|---|
| 1 | **Narration under-runs by 30%** (2.8 min vs 4.0 min target) | The gates accept it: every scene clears the 25-word floor, but the lesson is materially shorter than intended. Is the target wrong, or is the floor too permissive? |
| 2 | **`source_chunk` misalignment** on ~5/9 scenes; one scene received the "5 big ideas" chunk instead of its tolerance concept | Grounding and hydration both read `source_chunk`, so a wrong chunk means wrong source enrichment *and* wrong repair material. Root cause looks like chunk assignment before scene construction. |
| 3 | **Fused token**: `ki` + `deterministic` renders as `kideterministic` | TTS reads a non-word. Needs a fusion guard, but any guard risks false positives on legitimate joins. |
| 4 | **Dangling semantic title** on a continuation page | Reads as unfinished. |
| 5 | **Column fill averages 61%**, bottom whitespace 0.83–2.72 in | Cosmetic density complaint, consistent across content slides. |
| 6 | **No continuation marker** on paginated scenes | A reader cannot tell page 2 continues page 1. |
| 7 | **Provenance**: a saved `plan.audit.json` can record a `plan` path from a different file than the one it ships beside | An audit artifact should name the file it describes. |
| 8 | **Reveal sync is precise-looking but not verified precise** | All 9/9 scenes locate every bullet, so recall is fine. But the anchor is the bullet's *single longest token* and the match is greedy-first: in scene 6 `structure` matches at 2.26 s inside the setup clause, and in scene 1 two bullets both anchor on `baseline` and resolve only because it occurs 3 times. A reveal can therefore fire at the wrong occurrence, or drop a whole scene to an even split when a repeated token appears once. See question 4. |

## 11. The five questions

1. **Under-run (defect 1).** The narration gate is per-scene; the target is
   per-lesson. Should there be a whole-lesson duration floor, or is the per-scene
   word floor the right control and the 4.0 min target simply wrong for a
   9-scene lesson? I lean toward the latter but cannot justify 2.8 min as
   intentional.

2. **Grounding (defect 2).** What is the right way to assign source chunks to
   scenes so that the chunk feeding a scene is the one about that scene's
   concept? Vocabulary overlap per heading, or assign after scene titles exist?

3. **Token fusion (defect 3).** How do I detect a bad join between two tokens
   without flagging legitimate ones? My worry is that this is a vocabulary
   question with no purely token-level rule.

4. **Reveal sync (defect 8).** When narration paraphrases a bullet, per-word
   offsets are exact but the bullet→offset mapping is fuzzy. Weighted token
   overlap is order-free and survives rewording; monotonic assignment prevents
   backward jumps. Is that the right trade, or is there a better signal? I would
   rather instrument ground truth first than pick a heuristic on taste.

5. **TTS provider risk (§8).** Is pinning plus a CI health probe the right level
   of defence for an unofficial wrapper, or should the backend seam land before
   any further enrichment work?

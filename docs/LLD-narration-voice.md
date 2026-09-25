# LLD — Narration Safety and Language-Aware Voice Layer

Version: **v001**
Status: **Implemented** (`src/doc_to_video_tutor/studio/`)
Scope: lesson-plan generation, narration safety, spoken-script normalization, and the
gates that stand between a language model and a rendered lesson.

---

## 1. Purpose

`doc-to-video-tutor` turns one or more source documents into a narrated lesson: a
scene plan, a PPTX deck, per-scene voice-over, and an MP4. The lesson director is a
small open-weights model served on a free endpoint, so the pipeline is designed
around one assumption:

> the model is a fast, cheap *proposal* engine, never a source of truth.

Everything a viewer hears or reads is therefore validated and, where possible,
repaired by deterministic code. The design goal is that a defective model response
produces a *rejected build with an audit artifact*, never a shipped lesson.

---

## 2. Problem statement

Four failure classes drive the design.

| # | Problem | Consequence if unhandled |
|---|---|---|
| P1 | The model converges on the same filler sentence in every scene ("aaj aapne dekha ki …") | Repetitive, low-quality voice-over that sounds machine-generated |
| P2 | Repetition cannot be removed naively | Deleting repeated text mutates real teaching content and corrupts terminology |
| P3 | The spoken layer is language-coupled (Hinglish/Marathi-English) while safety rules must be language-agnostic | Changing language silently disables the safety machinery |
| P4 | The model emits structurally valid but unusable plans (non-Latin slide text, empty scenes, over-long narration, stale pagination, truncated JSON) | Defects reach TTS, burn an expensive render, or ship |

---

## 3. Design goals and non-goals

### Goals

- **G1** — Language-agnostic safety: every repeat, integrity, and language gate
  operates on tokens only and never references a word or script.
- **G2** — Deterministic repair before rejection: a single bad scene is repaired
  from its own validated fields, not regenerated.
- **G3** — Hard pre-render gate: damaged narration can never reach TTS or video.
- **G4** — Explicit switchable voice: the spoken register is a data object, so
  changing narration language is a data change, not a code change.
- **G5** — Auditable and reproducible: exact TTS script, plan metadata, and
  idempotent repair are persisted artifacts.
- **G6** — Fail loudly and early: the cheap deterministic commands run before the
  expensive render.

### Non-goals

- Runtime language detection or machine translation of the narration.
- Per-scene LLM resampling (resampling is whole-plan; single scenes are repaired).
- External voice-file profiles (TOML/JSON) — voices are Python constants.
- Word-level timestamps, subtitles, or kinetic text (see §18).

---

## 4. System context and module map

```
document files (.md/.txt/.pptx)
        │
        ▼
  util.load_documents ──► text (tokenizer, script detection, citations)
        │
        ▼
  llm (topic extraction, plan generation, scene patching)   ← budget router
        │
        ▼
  plan.plan_lesson ──► schema (Pydantic contract) ──► narration (Layer A + B)
        │                                                 │
        │                                                 ▼
        │                                           speech (TTS script)
        ▼
  validate (guard_plan / _render_blocking_problems)          │
        │                                                     ▼
        ▼                                              video / slides / pptx
  cli (build | verify | review | render)
```

| Module | Responsibility |
|---|---|
| `config.py` | All constants: prompts, palette, scene limits, TTS and loudness targets, brand footer |
| `util.py` | Document loading, atomic JSON writes, retry backoff, progress |
| `text.py` | Canonical tokenizer, n-gram helpers, non-Latin script detection, citation stripping |
| `llm.py` | Endpoint access, planner budget router, truncation detection, JSON parsing |
| `voice.py` | `NarrationVoice`, `NarrationLanguagePolicy`, `CleanupRule`, pronunciation rules |
| `narration.py` | Layer A repeat machinery, deterministic rebuild, thin-scene repair, narration generation |
| `schema.py` | `SlideScene`, `LessonPlan`, `TTSClip` Pydantic models |
| `plan.py` | Concept extraction, scene frame, deterministic plan repair, pagination, scene metadata |
| `validate.py` | Gate evaluation (`guard_plan`, `_render_blocking_problems`, `review_plan`) |
| `speech.py` | Spoken normalization and the pre-audio audit |
| `slides.py` / `pptx.py` / `video.py` | Rendering, deck assembly, TTS synthesis, loudness, video encode |
| `cli.py` | Command orchestration and artifact writing |

---

## 5. Architecture — two layers

```
┌────────────────────────────────────────────────────────────────┐
│ LAYER A — language-agnostic (token-only, no words, no scripts)  │
│                                                                │
│  canonical tokenizer        _nar_tokens  (NFKC, lower, strip)   │
│  n-gram keying              _nar_3grams_t / _nar_gram_key       │
│  protected allowlist        _protected_terms                     │
│  detection                  _narration_repeat_report             │
│                             _unsafe_repeat_scenes                │
│  sentence sweeps            _drop_repeated_filler                │
│                             _drop_shared_narration_sentences     │
│  sentence-aware enforcer    _enforce_unique_narration_trigrams   │
│  deterministic rebuild      _repair_unsafe_narrations            │
│                             _repair_thin_narrations              │
│  length ceiling             _trim_narration_word_count           │
│  integrity checks           _narration_integrity_problems        │
│  hard gate                  _render_blocking_problems            │
│                                                                │
│  Contract: identical behaviour for any whitespace-delimited     │
│  narration language. Contains no Hinglish constant.             │
└────────────────────────────────────────────────────────────────┘
┌────────────────────────────────────────────────────────────────┐
│ LAYER B — selected voice profile (what the engine WRITES)      │
│                                                                │
│  NarrationVoice: openers, closers, dd_leads, points_leads,      │
│                  narr_heads, bullet_leads, takeaways labels,    │
│                  pronunciation rules, TTS voice settings       │
│  NarrationLanguagePolicy: expected language, required markers,  │
│                  min marker hits, cleanup rules                │
│                                                                │
│  Consumed by: _assign_openers, _assign_closers,                │
│  _dedupe_narration_templates, _deepen_narrations,               │
│  _rebuild_scene_narration, speech expansion, advisory language  │
│  check.                                                         │
└────────────────────────────────────────────────────────────────┘
```

**Why the split.** The studio *writes* Hinglish narration by design; that is a
product feature, not an accident. Rather than auto-detecting what the model
produced, the language is a user-declared voice. If the declaration is wrong the
consequence is aesthetic (odd openers, a soft language warning) — never a repeat
failure and never a crash.

---

## 6. Data contracts

### 6.1 Plan document (`*.plan.json`)

```json
{
  "schema_version": 2,
  "repeat_policy_version": 2,
  "voice": "mhe-mix",
  "narration_voice": {
    "name": "mhe-mix",
    "version": 1,
    "fingerprint": "sha256:…",
    "language_tag": "mixed"
  },
  "tts": {
    "provider": "edge-tts",
    "voice": "hi-IN-SwaraNeural",
    "rate": "-8%",
    "pitch": "+0Hz",
    "volume": "+0%",
    "target_minutes": 4.0,
    "source": "env/CLI defaults"
  },
  "protected_trigrams": ["…"],
  "topics": ["regression gates", "metric registry"],
  "source_files": ["doc/learn/08_concepts_mod03_gates.md"],
  "source_bigrams": ["…"],
  "source_tokens": ["…"],
  "plan": { "title": "…", "opening": "…", "scenes": [], "takeaways": [] }
}
```

`schema_version` and `repeat_policy_version` make stored plans re-interpretable
after a rule change. The voice fingerprint pins the exact spoken register, so a
plan can be re-rendered later with the same voice even if the constants move.
All writes go through `atomic_json_write` (temp file, `fsync`, same-directory
rename), so an interrupted run never leaves a truncated plan.

### 6.2 Scene schema (`schema.py`)

```python
class SlideScene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    narration: str
    bullets: list[str] = Field(default_factory=list, max_length=4)
    steps: list[str] = Field(default_factory=list)
    flow: list[str] = Field(default_factory=list)
    visual_diagram: str = ""
    code_snippet: str = ""
    code_context: str = ""
    analogy: str = ""
    design_decision: str = ""
    section: str = ""
    topic: str = ""
    source_refs: list[str] = Field(default_factory=list)

class LessonPlan(BaseModel):
    title: str
    opening: str
    scenes: list[SlideScene] = Field(min_length=1)
    takeaways: list[str] = Field(default_factory=list)
```

Validation rules, all applied at generation time:

| Rule | Rationale |
|---|---|
| `bullets` ≤ 4 | Slide density ceiling; longer content is paginated, not shrunk |
| `bullets` / `steps` / `flow`: `None`, `""`, and tuples normalize to lists | The model emits `""` for absent sequences as often as `[]` |
| Slide-visible fields must be Latin script | Non-Latin text on a slide is unspeakable by the configured voice and is a hard render failure |
| `title`, `opening` must be Latin script | Same, for the cover and spoken intro |
| `title` must be non-empty | Prevents a nameless slide |
| `narration` may be empty **at plan time | The planner is instructed to leave narration blank; a dedicated pass writes it later |
| `takeaways` trimmed and capped at 8 | Bounds the closing panel |
| Unknown keys ignored | Forward-compatible with richer model output |

Schema validation is a *structural* contract only. Semantic quality (grounding,
coverage, repetition, teaching content) is enforced by the gates in §12.

### 6.3 TTS script artifact (`*.tts_script.json`)

The exact input handed to the TTS provider, per clip:

```json
{
  "role": "scene", "index": 2, "section": "What Is This",
  "title": "M2 Metadata & types",
  "narration": "…plan narration…",
  "spoken_title": "module two Metadata and types.",
  "spoken": "module two Metadata and types. …",
  "slide_meta_hits": 0,
  "word_count": 66
}
```

plus a top-level `audit` array of findings and the resolved TTS settings. This file
is the audio contract: what it contains is what the voice speaks.

---

## 7. Layer A — algorithms

### 7.1 Canonical tokenizer

`_nar_tokens(text)` is the single tokenizer for every repeat rule. It:

1. applies Unicode NFKC normalization;
2. folds zero-width, bidi, and word-joiner characters;
3. lowercases;
4. strips punctuation, including Devanagari danda;
5. splits on Unicode whitespace and on a set of intra-token delimiters
   (`—`, `–`, `/`, `_`, bracket pairs) while **keeping** hyphens inside a token
   (`quality-gate` stays one token, `kind_direction` becomes two).

Because every gate and repair shares this function, a gate and its repair can
never disagree about what "the same words" means.

Scripts without whitespace token boundaries (Han, Kana, Hangul, fullwidth) are
detected and surfaced with a one-time warning, because trigram windows over such
text are degenerate by construction.

### 7.2 N-gram keying

- `_nar_3grams_t(tokens)` — ordered 3-gram set.
- `_nar_gram_key(phrase)` — normalized key used for cross-scene sentence
  identity (leading tokens, punctuation-insensitive).

A phrase is **banned** when its ordered 3-grams appear in two or more scenes.

### 7.3 Protected allowlist

`_protected_terms(plan)` collects tokens that must never be auto-removed:

- plan takeaways and scene titles;
- `design_decision` text;
- `protected_trigrams` persisted from the source document;
- entity tokens: acronyms, `CamelCase`, numerics, paths, identifiers.

Protected repeats are reported separately and never fail a gate. The enforcer
removes unprotected repetition only.

### 7.4 Detection

| Function | Question answered |
|---|---|
| `_narration_repeat_report(plan, protected)` | Which phrases repeat across scenes (banned vs protected)? |
| `_narration_tail_repeat(plan, protected)` | Soft warning form used by the planner |
| `_unsafe_repeat_scenes(plan, protected)` | Which scenes are beyond automatic repair? |
| `_narration_integrity_problems(plan)` | Is any narration empty, too short, or a fragment artifact? |

`_unsafe_repeat_scenes` classifies a scene as unsafe when it is a full duplicate
of another scene, or when it is both too short and repeating — the two cases where
trimming would destroy the content.

### 7.5 Repair chain (ordered)

The chain is deterministic and idempotent. Order matters: repairs that change
narration run before the enforcer's final pass.

```
_dedupe_plan_bullets            exact duplicate bullets across scenes
_prune_bullet_takeaway_echo     bullets near-copying a takeaway
_sanitize_design_decisions      drop degenerate / duplicated decision text
_source_design_decisions        fill empties from real source contrast sentences
_dedupe_narration_templates
  ├ _drop_repeated_filler       within-narration sentence dedup
  ├ cleanup_rules               collapse known stall skeletons, re-attach content
  └ _drop_shared_narration_sentences   cross-scene whole-sentence dedup
_deepen_narrations              speak each scene's decision once; append takeaways
_enforce_unique_narration_trigrams    sentence-aware enforcer (pass 1)
_repair_unsafe_narrations       deterministic rebuild for still-unsafe scenes
_repair_thin_narrations         deterministic rebuild driven by the audio gate
_trim_narration_word_count      drop trailing sentences above the spoken ceiling
```

**Narration contract.** The pipeline never reads slide bullets into narration.
The slide carries the bullets; the narration carries the explanation and the
decision. Bullet text is spoken only as verbatim teaching points when the
dedicated narration pass chooses to, and `_has_teaching_claim` accepts a scene
whose spoken track carries at least two distinct scene bullets.

### 7.6 Enforcer contract (sentence-aware, protected-aware)

- A sentence whose every token belongs to a banned phrase is dropped **whole**,
  and only when it is not the plan-first occurrence.
- A sentence that also carries real content is trimmed, and only if at least
  `_NARR_MIN_TOKENS` (12) tokens survive.
- Otherwise the scene is marked unsafe and handed to the deterministic rebuild.
- After repair the enforcer re-runs so protected terminology survives.

### 7.7 Deterministic rebuild

`_rebuild_scene_narration(scene, …)` regenerates one scene's narration from that
scene's own validated fields, using only the voice's words:

```
opener (chosen collision-free against other scenes' trigrams)
  + design_decision lead + decision text
  + distinct scene bullets
  → _clean_narration
```

Fragments whose trigrams collide with another scene, or with any takeaway, are
skipped, so a rebuild cannot introduce a new banned repeat. If the scene carries a
`source_chunk`, the rebuild hydrates from real source prose to reach the spoken
floor; without one it falls back to the legacy floor.

Two entry points share this machinery:

- `_repair_unsafe_narrations` — the scene still carries a banned repeat.
- `_repair_thin_narrations` — the audio gate reports the scene as too short,
  fragmentary, or teaching-claim-free. The repairer is driven by the **same**
  finding codes the gate emits, so repair and gate share one source of truth.

If a rebuild cannot make a scene gate-clean, the scene is left untouched and the
hard gate reports it honestly.

---

## 8. Layer B — voice profiles

```python
@dataclass(frozen=True)
class CleanupRule:
    name: str
    pattern: re.Pattern

@dataclass(frozen=True)
class NarrationLanguagePolicy:
    expected_language: str
    must_not_be_pure_english: bool
    required_markers: frozenset[str]
    min_marker_hits: int
    cleanup_rules: tuple[CleanupRule, ...]

@dataclass(frozen=True)
class NarrationVoice:
    name: str
    openers: tuple[str, ...]
    closers: tuple[str, ...]
    dd_leads: tuple[str, ...]
    points_leads: tuple[str, ...]
    narr_heads: tuple[str, ...]
    takeaways_lead: str
    takeaways_any_lead: str
    takeaways_marker: str
    bullet_leads: tuple[str, ...]
    policy: NarrationLanguagePolicy
    pronunciation_rules: tuple[PronunciationRule, ...]
    tts_voice: str | None
    rate: str
    pitch: str
    volume: str
```

Registered profiles:

| Name | Language | TTS voice | Notes |
|---|---|---|---|
| `mhe-mix` (default) | Roman-script Marathi/Hindi/English | `hi-IN-SwaraNeural` | `must_not_be_pure_english=True` |
| `english` | English | `en-IN-NeerjaNeural` | pure English is legal |

Selection and threading:

- `--narr-voice` on `build`, `render`, and `verify`; an unknown name exits 2 and
  lists the registered voices. Nothing is guessed.
- The resolved voice is threaded into the planner, the narration pass, the
  gates, and the advisory language check.
- `verify`, `review`, and `render` read the voice from the saved plan, so a stored
  plan is re-checked in the same dialect it was written in.
- `narration_matches_voice_policy` is **advisory**: a marker shortfall reports the
  soft warning `narration pure-English` and never forces a resample.

Spoken lead-ins are grammatical when the TTS punctuation repair removes their
terminator, e.g. `Iska ahem reason yeh hai ki …` rather than `Iska ahem reason: …`.

---

## 9. Speech normalization layer (`speech.py`)

`build_tts_script(plan, voice)` is the only producer of spoken text. It composes,
in order: slide-meta stripping, spoken-title preparation, narration expansion,
punctuation repair, repeated-title collapse, and fragment hydration — then audits
the result.

| Stage | Function | Purpose |
|---|---|---|
| Slide-meta strip | `strip_slide_meta` | Remove "slide pe", "exact points", and profile-specific listing phrases |
| Pronunciation expansion | `speech_expand` | Longest-match-first expansion of identifiers, symbols, digits, and file names (`active.jso` → `active dot json`, `1/(n+1)` → `one over n plus one`, `Exit 3` → `Exit three`) |
| Flattening | `_flatten_parentheses` | Remove brackets, list numbering, backticks; split em/en dashes |
| Punctuation repair | `repair_speech_punctuation` | Collapse ellipses and doubled terminators, bind orphaned punctuation, guarantee a final terminator |
| Title collapse | `collapse_repeated_title` | A scene title is spoken at most once |
| Fragment hydration | `_hydrate_spoken_fragments` | Prepend a rotating voice `bullet_lead` to any sentence that would otherwise read as a bare title/bullet fragment; the deliberate spoken-title head is exempt |
| Takeaway join | `_join_sentence_items` | Each takeaway becomes its own terminated sentence |
| Audit | `audit_tts_script` | Findings table below |

### 9.1 Pre-audio audit

| Code | Severity | Condition |
|---|---|---|
| `tts_voice_unset` | FAIL | voice profile has no concrete TTS voice |
| `tts_text_corrupted` | FAIL | mixed script, replacement character, or control character |
| `tts_slide_meta_leak` | FAIL | residual slide-listing phrase |
| `tts_symbol_heavy` | FAIL | raw arrow, slide numbering, or raw file token survived |
| `tts_punctuation_error` | FAIL | unbalanced brackets |
| `tts_sentence_fragment` | FAIL | bare title/bullet echo (spoken-title head exempt) |
| `tts_unknown_token` | FAIL | unexplained capitalized token absent from title, source, and glossary |
| `tts_required_concept_missing` | FAIL | transition/title-only narration with no teaching claim |
| `tts_too_short_critical` | FAIL | fewer than 20 spoken words |
| `tts_too_long_critical` | FAIL | more than 90 spoken words |
| `tts_too_short` | WARN | fewer than 25 spoken words |
| `tts_too_long` | WARN | more than 70 spoken words |
| `tts_long_sentence` | WARN | single sentence over 30 words |
| `tts_code_fragment` | WARN | unexpanded code or symbol token |
| `tts_title_repeated` | WARN | title spoken more than once |

Any `FAIL` blocks TTS: the CLI writes `*.rejected.tts_script.json`, prints the
findings, and exits 1 **before** the provider is called, so no audio or video is
produced from a broken script.

---

## 10. Planning pipeline (`plan.py`)

### 10.1 Concept extraction and scene frame

`_concept_headers(content)` is a stdlib AST-lite pass over `##`/`###` headings:

- junk and structural headings are filtered (container headings that only group
  subsections, overview sections, table-of-contents style headings);
- headings are deduplicated in source order and bounded to
  `AUTO_TRIM_MAX_SCENES`.

`_concept_groups(concepts)` produces `min(N, 12)` groups: one scene per concept up
to the ceiling, contiguous labelled merges beyond it. `_plan_scene_target` resolves
the required scene count. Documents with fewer than five concepts fall back to a
six-scene drill.

`_scene_count_problem` enforces the contract: a concept-driven plan must match its
target within a one-scene tolerance at the 10+ ceiling (the model reliably emits
one scene fewer on dense documents), and must match exactly below it. A plan that
exceeds the target is rejected.

### 10.2 Deterministic plan repair

Applied in `plan_lesson` after parsing, before narration:

| Repair | Effect |
|---|---|
| `_dedupe_plan_bullets` | Drop exact duplicate bullet lines within and across scenes |
| `_prune_bullet_takeaway_echo` | Drop bullets that near-copy a takeaway (token Jaccard ≥ 0.78) |
| `_harvest_takeaways` | Keep takeaways traceable to retained scenes |
| `_fix_placeholder_titles` | Replace titles that merely restate the section label |
| `_sanitize_design_decisions` | Drop generic or duplicated decision text |
| `_source_design_decisions` | Fill empty decisions from real source contrast sentences, with a uniqueness guard |
| `_annotate_source_chunks` | Attach a short, relevant source excerpt per scene |
| `_drop_ungrounded_slide_text` | Drop slide text with no source anchor, then re-paginate |
| `_sanitize_plan_source_leaks` | Strip source citations and metadata from visible text |

### 10.3 Pagination

Bullets are the unit of slide layout, so pagination is a first-class field:

- `_paginate_plan_slides` derives `bullet_pages` by splitting `bullets` into pages
  of at most four, and records `pagination {field, pages, page_size}`;
- an existing `bullet_pages` is honoured only when it is well-formed and its
  flattened content equals `bullets`;
- pagination is re-derived after **every** mutation that can change `bullets`
  (grounding drop, echo prune, dedupe), because stale pages would otherwise render
  content the plan no longer contains;
- `_scene_metadata_problems` hard-fails any scene where the flattened pages differ
  from `bullets`, or where a page exceeds four items.

### 10.4 Diagrams

`visual_diagram` is a text chain (`[A] ---> [B] ---> [C]`) parsed by
`_parse_diagram` and drawn as node boxes with chevron arrows by both renderers.
`_ensure_technical_visuals` supplies deterministic fallbacks derived from the
scene's own fields and source chunk when the model leaves the field empty —
currently precedence ordering, exit-code classes, verdict kinds, and metric
registry structure.

### 10.5 Scene metadata

`_annotate_scene_metadata` assigns `topic` (from the scene title) and
`source_refs` (extracted file references from the source, falling back to the
input filenames). Every scene is required to carry both.

---

## 11. Planner budget and reliability (`llm.py`)

The endpoint shares a bounded context window between input and output, so the
planner budgets explicitly:

- `choose_planner_budget(content, prompt_builder)` walks a window ladder
  `(12000, 8000, 6000, 4000, 2400)` and returns the widest window whose projected
  completion budget still clears `PLANNER_MIN_OUTPUT` (2048 tokens).
- `planner_output_budget` reserves output at a fixed characters-per-token ratio
  with headroom.
- `_LAST_FINISH` captures the provider's finish reason; `_looks_truncated` adds a
  structural heuristic (unbalanced delimiters, dangling fences). Either signal
  makes the parse fail fast and route to the retry path instead of yielding a
  ragged plan.
- On an output-side failure the retry drops optional framing blocks and re-probes
  the ladder, answering a cut with **output** budget rather than a smaller input.
- `_wait_before_retry` applies exponential backoff with jitter between resamples.

Targeted scene patching (`_scene_problem_map` → `_patch_scene`) rewrites only the
fields of offending scenes and keeps the rest of the plan frozen; whole-plan
resampling is the fallback, not the first response.

---

## 12. Validation gates (`validate.py`)

| Gate | Checks | Severity | Wired into |
|---|---|---|---|
| `guard_plan` | language policy, topic drift, opening drift, banned repeats, unsafe scenes, narration integrity, scene metadata, non-Latin slide text, prompt-example reuse, topic coverage, section normalization, copy-paste bullets, takeaway echo, placeholder titles, merged tokens, scene count, and grounding (when source n-grams/tokens are supplied) | hard + soft | `build` review loop, `review`, `verify` |
| `_render_blocking_problems` | banned repeats, unrepairable scenes, narration integrity, scene metadata (including pagination), non-Latin slide text, scene count | hard only | `build` before media, `render` before anything else |
| `audit_tts_script` | §9.1 | hard + soft | pre-audio gate in `build` and `render` |
| media measurement | resolution, bitrate, loudness, true peak | hard | tester L2 evidence pass |

Hard gate behaviour: the plan is persisted, a `*.rejected.audit.json` (or
`*.rejected.tts_script.json`) artifact is written, the reason is printed, and the
process exits 1 with no media produced. Soft warnings are reported and persisted
but do not block.

`_has_teaching_content` defines a usable scene: at least two bullets, or any of
steps / flow / analogy / design decision, or narration whose content-word density
clears the threshold once openers, closers, and the title echo are removed.

---

## 13. Orchestration (`cli.py`)

| Command | Behaviour |
|---|---|
| `build` | Load documents → extract concepts and topics → plan (up to 5 samples, each gated) → land every parsed sample as an artifact → deterministic repair → pre-audio gate → render slides, TTS, deck, and (unless `--skip-video`) video |
| `verify` | Load a saved plan → run the full deterministic repair chain → gate → self-check idempotency by running the chain a second time and diffing → write `*_fixed.plan.json` + `*.audit.json` |
| `review` | Deterministic gate evaluation of a saved plan, no writes |
| `render` | Load a saved plan → sanitize → pre-render gate → pre-audio gate → render media deterministically from the plan |

The expensive render is always the last step. Every failure mode above is
detectable in seconds by `verify` or `review`, and a blocked build still persists
its expensive plan so repair never requires paying for generation twice.

---

## 14. Media layer

| Concern | Design |
|---|---|
| Canvas | 1280×720, dark theme: background `(18,24,38)`, panel `(30,40,60)`, accent `#4285F4`, gold, green, white body text |
| Typography | Arial for body, Courier New for code only; slide titles 32pt bold (capped at 44 characters), body ≥ 18pt |
| Layout | Shared chrome (accent bar, section label, title, gold divider, footer with brand and version, slide counter); vertical budget guard prevents any block drawing off-canvas |
| Progressive reveal | Each scene renders an intro variant plus one variant per bullet, all sharing pixel-identical layout; audio time is split so the highlight advances with speech |
| Diagrams | Text chain parsed into node boxes with gold chevron arrows |
| Pagination | Slide count is derived from paginated scenes, so counters always match rendered content |
| Audio | edge-tts, per-scene clips synthesized concurrently, normalized with one-pass ffmpeg `loudnorm` (`I=-16`, `TP=-1.5`, `LRA=11`), original kept if ffmpeg is unavailable |
| Video | Per-scene slide shown for `max(audio.duration, 0.1)` seconds, 3 s inter-scene pause, 0.6 s cross-fade, 6 s closing hold, 24 fps CBR encode at 700 kbps |
| Cover | Title card prepended to the video (skipped for `--skip-video`) and held 4 s of real PCM silence before the first scene |

---

## 15. Configuration reference

| Constant | Value | Meaning |
|---|---|---|
| `MIN_SCENES` | 5 | Floor for a concept-driven frame |
| `TARGET_MAX_SCENES` | 8 | Scene count for non-concept-driven plans |
| `AUTO_TRIM_MAX_SCENES` | 12 | Hard ceiling; 1:1 concept ceiling |
| `_NARR_MIN_TOKENS` | 12 | Minimum tokens a trimmed sentence may retain |
| `_SECTIONS` | 5 labels | Canonical slide section vocabulary |
| `PLANNER_MIN_OUTPUT` | 2048 | Minimum planner completion budget |
| `_PLANNER_WINDOWS` | 12000→2400 | Source-window ladder |
| `LOUDNESS_TARGET` / `LOUDNESS_TP` / `LOUDNESS_LRA` | −16.0 / −1.5 / 11.0 | Loudness normalization targets |
| `LOUDNESS_WPM` | 135 | Words-per-minute model for duration reporting |
| `TTS_RATE` / `TTS_PITCH` / `TTS_VOLUME` | −8% / +0Hz / +0% | Default voice-over delivery |
| `TITLE_HOLD` | 4.0 s | Cover card hold |
| `_VIDEO_BODY_MAX` | 652 px | Vertical body budget on the 720 px canvas |
| `PACKAGE_VERSION` / `BRAND_FOOTER` | 0.1.0 | Footer identity on every slide |
| `_SCHEMA_VERSION` / `_REPEAT_POLICY_VERSION` | 2 / 2 | Plan-document provenance |

Every constant is environment-overridable where it affects media output
(loudness, voice, rate), so a deployment can retune without a code change.

---

## 16. Testing strategy

| Layer | Approach |
|---|---|
| Tokenizer / n-grams | Unit tests over separators, zero-width characters, hyphens, digits |
| Repeat gate and enforcer | Synthetic plans per language; assert banned vs protected classification, first-occurrence preservation, floor respected, unsafe classification |
| Deterministic rebuild | Assert a repaired scene clears the gate it failed, and that the rebuild introduces no new trigram collision |
| Schema | Table-driven validation cases: shape normalization, bullet ceiling, script rejection, empty generation narration |
| Speech | Expansion, meta stripping, punctuation, title collapse, fragment hydration, and every audit code with its threshold |
| Plan pipeline | Concept extraction, scene frame, pagination invariant, diagram fallback, metadata annotation |
| End-to-end | `verify` idempotency on stored plans; blocked-plan artifacts; a repaired plan rendering to media |
| Static | `ruff check src tests` and `mypy src` clean |

The fast loop is deliberate: `verify` and `review` run in seconds without an LLM
or a render, so contract regressions are caught before any media cost.

---

## 17. Acceptance criteria

1. `review` on a freshly built plan returns `VERDICT: PASS`, or `PASS (soft
   warnings)` with only advisory findings.
2. A plan with a banned narration repeat is rejected by `build` and `render` with
   exit 1 and a `*.rejected.audit.json`, and no audio, deck, or video is produced.
3. `verify` repairs that same plan deterministically to zero banned repeats, and a
   second pass changes nothing (`idempotent: true`).
4. Protected terminology survives repair and is reported separately.
5. A saved plan re-checked with `verify`/`review`/`render` uses the persisted
   voice and settings.
6. A plan whose TTS script contains any `FAIL` finding is rejected before the TTS
   provider is called.
7. Slide-visible text is Latin script in every artifact.
8. `bullet_pages` always equals `bullets` after repair, and no slide shows more
   than four bullets.
9. Every scene carries `topic` and at least one `source_ref`.
10. Scene count matches the concept frame contract.
11. `ruff`, `mypy`, and the full test suite are clean.

---

## 18. Known limitations and future work

| Item | Status | Candidate direction |
|---|---|---|
| Word-level timestamps for subtitles or per-word highlighting | Deferred | Forced alignment (`whisperx`, `stable-ts`) or a wav2vec aligner; acceptance: emit `timecodes.json` and VTT |
| Kinetic reveals, pan/zoom, focus dimming | Deferred | ffmpeg `zoompan` / `drawbox` keyframes, batched per scene rather than per word |
| Background music and auto-ducking | Deferred | ffmpeg `sidemix` / `sidechaincompress` behind an optional `--bgm` |
| Per-scene LLM resampling | Not planned | Whole-plan resample plus deterministic repair is cheaper and more stable on a free endpoint |
| External voice profiles | Not planned | Constants are sufficient for two profiles; a file format is justified only when third-party profiles are needed |
| Non-whitespace-delimited narration | Out of scope | The tokenizer, and therefore the repeat model, assumes segmentable text |
| 1080p output | Deferred | 720p is the current fidelity target |

---

## 19. Appendix

### 19.1 Artifact set

| Artifact | Contents |
|---|---|
| `<name>.plan.json` | Full plan document with provenance metadata |
| `<name>.sampleN.plan.json` | Every parsed planning sample, including rejected ones |
| `<name>_fixed.plan.json` | Plan after deterministic repair (`verify`) |
| `<name>.audit.json` | Repair audit: before/after counts, idempotency flag |
| `<name>.rejected.audit.json` | Pre-render hard-gate failure with reasons |
| `<name>.rejected.tts_script.json` | Pre-audio gate failure with the exact script |
| `<name>.tts_script.json` / `.script.txt` | Exact TTS input and a readable transcript |
| `<name>_audio/scene_*.mp3` | Per-scene voice-over for external listening |
| `<name>.pptx` | Lesson deck |
| `<name>.mp4` | Rendered video |

### 19.2 Glossary

| Term | Meaning |
|---|---|
| Layer A | Language-agnostic, token-only safety machinery |
| Layer B | The selected voice profile: what the engine writes |
| Voice | The spoken register: language, phrasing pools, pronunciation, TTS settings |
| Banned phrase | A ≥3-token sequence repeated across two or more scenes, not protected |
| Protected term | Text exempt from automatic removal (titles, takeaways, decisions, source and entity tokens) |
| Unsafe scene | A scene that repetition repair cannot fix without destroying content |
| Rebuild | Deterministic narration regeneration from a scene's own validated fields |
| Fragment | A short sentence that merely restates the slide title or a bullet |
| Grounding | Whether visible text is anchored in real source content |

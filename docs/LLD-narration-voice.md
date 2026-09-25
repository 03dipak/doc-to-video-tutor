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
| Colour semantics | One palette in `config.py`. Green means PASS, gold means REVIEW, accent means neutral, muted means non-decisive. **Red `(234,67,53)` is permitted only for FAIL semantics** (the status-badge row); it is never used for body text and never paired with green text, so no red-on-green contrast pair can occur |
| Typography | Arial for body, Courier New for code only; slide titles 32pt bold (capped at 44 characters), body ≥ 18pt |
| Layout | Shared chrome (accent bar, section label, title, gold divider, footer with brand and version, slide counter); vertical budget guard prevents any block drawing off-canvas |
| Progressive reveal | Each scene renders an intro variant plus one variant per bullet, all sharing pixel-identical layout; audio time is split so the highlight advances with speech |
| Diagrams | Text chain parsed into node boxes with gold chevron arrows |
| Source-grounded enrichment | Three optional blocks, all derived deterministically from the scene's own source chunk by `_ensure_technical_visuals` — never model-written, so they cannot invent a fact: `status_badges` (the exit-code row, colour-coded, emitted only when the source states the PASS/FAIL/REVIEW triple), `value_table` (PASS/FAIL boundary pairs, requiring a decimal or unit so exit codes are never mistaken for tolerances, and at least two rows), `json_snippet` (the literal file shape the document already gives). Each degrades rather than disappears when space is tight: the payload falls back from a pretty block to one compact line, so video and deck agree via the shared budget |
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
| `RED` | `(234, 67, 53)` | FAIL semantics only; see the colour-semantics rule in §14 |
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
| Reviewer agents | `graphic-reviewer` grades template conformance and colour semantics against §14; `tester` runs L1 gates plus L2 `ffprobe`/`ebur128` measurement. Both are read-only and report evidence; their findings are triaged into §20 rather than a session log. |

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
10. Scene count matches the concept frame contract (§10.1), which is authoritative over any fixed 5–8 range.
11. `ruff`, `mypy`, and the full test suite are clean.
12. Every open defect in §20 is either closed or explicitly waived in writing; a FAIL row there blocks a release.

---

## 18. Known limitations and future work

| Item | Status | Candidate direction |
|---|---|---|
| Word-level timestamps for reveal sync and subtitles | **Adopted — see §19** | The provider already returns them: `Communicate(..., boundary="WordBoundary")` yields `{offset, duration, text}` per word. Offsets are 100-nanosecond ticks (`TICKS_PER_SECOND = 10_000_000`), and `Communicate` applies offset compensation across chunked input so timings stay continuous. `edge_tts.SubMaker` converts the same stream to SRT at no extra cost. Forced alignment (`whisperx`, `stable-ts`) is therefore not required and stays rejected. |
| Kinetic reveals, pan/zoom, focus dimming | Deferred | ffmpeg `zoompan` / `drawbox` keyframes, batched per scene rather than per word |
| Vector/4K fidelity tier | Deferred | Replace Pillow rasterisation with SVG or an HTML/CSS stage. This is a fidelity tier, not a correctness gap: at native 1280×720 with TrueType faces the current text is already crisp. Costs a rewrite of both renderers. |
| Background music and auto-ducking | Deferred | ffmpeg `sidechaincompress` + `amix` behind an optional `--bgm` |
| Talking-head / avatar presenter | **Rejected** | Requires GPU plus model downloads, and the evidence does not support it for instruction: an on-screen instructor image is reported not to improve learning (Mayer's image principle, cited in the Bespoke lecture-generation study, arXiv 2609.26540). See §19. |
| Per-scene LLM resampling | Not planned | Whole-plan resample plus deterministic repair is cheaper and more stable on a free endpoint |
| External voice profiles | Not planned | Constants are sufficient for two profiles; a file format is justified only when third-party profiles are needed |
| Offline / air-gapped TTS | Noted, not now | edge-tts requires network. Piper (MIT) is a fully offline alternative; decide when an air-gapped or CI-without-egress requirement is real. |
| Non-whitespace-delimited narration | Out of scope | The tokenizer, and therefore the repeat model, assumes segmentable text |
| 1080p output | Deferred | 720p is the current fidelity target |

---

## 19. Enrichment direction and external cross-check

### 19.1 Tooling policy

Enrichment uses **free and open-source components only**. Paid or metered services
are out of scope regardless of quality: the pipeline's defining constraint is that
it runs against a free local model and a free TTS endpoint, and a paid dependency
would make "no manual review per document" unfundable at N.

### 19.2 Adopted

| Item | Cost | Rationale |
|---|---|---|
| Word-level reveal sync from `edge-tts` `WordBoundary` | none | The provider already returns per-word offsets; the current code discards them via `.save()` and splits a scene's audio into *equal* per-bullet shares, so bullets light up on a fixed fraction rather than when spoken. Capturing the stream is additive. |
| SRT subtitles via `edge_tts.SubMaker` | none | Same stream, different sink. Yields a standards-compliant subtitle artifact and an accessibility deliverable. |
| Pygments syntax colouring of the real command panel | BSD, pure Python | Colours only text the model already produced. No invention, no runtime service. |
| `status_badges` / `value_table` / `json_snippet` blocks | none | Shipped; see §14. All derived from the scene's own source chunk. |
| Burned-in captions from the generated SRT | none | Subs are a real accessibility deliverable, not decoration, and the SRT is already produced. Styled via an ffmpeg subtitle filter. |
| `ffmpeg silencedetect` as a pre-render audio QA check | none | The cheapest check on the list: catches truncated or dead-air TTS before the loudness pass, which directly serves G6. ffmpeg is already a dependency. |
| Minimal `zoompan` Ken Burns, scoped separately from kinetic reveals | none | A single uniform per-scene zoom is one filter, not a rewrite, and is the largest perceived-polish gain per unit of risk. Guarded: slide duration must still cover its audio clip. |
| SVG diagram backend (`cairosvg`/`resvg`), node logic unchanged | none | An incremental step toward the vector fidelity tier in §18: keep `_parse_diagram` and the node-box layout, swap only the rasteriser. A far smaller bet than replacing both renderers. |

### 19.3 Rejected

| Item | Reason |
|---|---|
| Whisper / `whisperx` / `stable-ts` forced alignment | Superseded by provider-native boundaries: a model download, CPU cost, and hardware-dependent output traded for precision we already receive for free. Retained only as the fallback if narration ever becomes prerecorded. |
| Mermaid-CLI diagram compilation | Adds a Node/npm toolchain to a Python-only offline pipeline, and renderer-class mapping was already rejected in favour of the deterministic text-chain parser. |
| Manim | Replaces the Pillow renderer wholesale and pulls in the cairo/pango stack. Existing node-box diagrams are already deterministic. |
| VHS-style simulated terminal output | The output does not exist in the source. Rendering an invented transcript would violate the grounding contract and the hard gate would (correctly) reject it. Typing animation is kinetic motion — see §18. |
| ElevenLabs / HeyGen / Synthesia / D-ID | Paid services, excluded by §19.1. |
| Wav2Lip / LivePortrait / SadTalker avatars | GPU plus model downloads, and contradicted by the learning evidence in §19.4. Decisive second argument: lip-sync and facial animation are *unverifiable model output* — the pipeline has no deterministic gate that can accept or reject them, which reopens the exact trust problem §5 exists to close. |
| Any model-based (NLI/entailment) grounding verifier | Re-adds a model where the architecture deliberately removed one. Grounding is already deterministic (§10.2); if false-negatives become a real problem, the first move is to investigate source-chunk selection in `_annotate_source_chunks`, not to add a classifier. |
| Wholesale SVG / HTML canvas rewrite | A fidelity tier, not a correctness gap; costs a rewrite of both renderers. |

### 19.4 External cross-check

Findings from independent sources, and what each one changes here:

| Source | Finding | Effect on this design |
|---|---|---|
| Bespoke, arXiv 2609.26540 (Sept 2026), 200+ generated lecture videos | "reveals slide elements as they are spoken"; "reveal timing is derived from the narration and checked programmatically"; assembly "uses no LLM calls" | Confirms the architecture: reveal derived from audio, timing verified in code, no model in assembly. |
| Same | "Programmatic checks where possible… duration arithmetic, schema checks, overflow detection run in ordinary code. The LLM is reserved for semantic judgments" | Independently validates the deterministic-gate design in §12. |
| Same | Text-to-video models "have trouble with equations and deterministic diagrams" | Confirms a programmatic renderer over a generative video model. |
| Same | Videos deliberately have **no talking-head instructor**: an on-screen instructor image "does not improve learning" (Mayer's image principle) | Decisive evidence to reject avatars (§19.3) rather than deferring them. |
| Same | The remaining human pass concentrates on "voice timing, slide–narration sync, and slide rendering" | Confirms sync is the highest-value remaining gap — the §19.2 adoption targets exactly it. |
| Same | Recurring visual complaints: "unused space", "text-heavy slides" | Matches the measured 61 % column fill (§20) — whitespace is a real quality signal, not a nitpick. |
| `edge-tts` docs / DeepWiki (v7.2.8, verified live) | `Communicate(..., boundary="WordBoundary")` yields `{offset, duration, text}`; 1 tick = 100 ns; `SubMaker` emits SRT; offsets are compensated across chunked text | Supplies the exact implementation contract for the sync adoption. |
| `telegraph/script-to-video` | "Colons create awkward pauses"; auto-generated `.srt`/`.vtt`; a 13-point pre-build QA gate | Independently corroborates the punctuation repair in §9 and the pre-audio gate in §9.1. |
| OpenCut, Remotion ecosystem | Word-level captions via Whisper, active-word highlight, React/Remotion rendering | The expensive route to what §19.2 gets free; confirms the vector/4K tier is the real reason to consider that stack, not sync. |

### 19.5 Order of work

1. Capture `WordBoundary` events in `_scene_audio` and return them per scene (additive). — **done**
2. Persist them to a `<name>.word_timings.json` sidecar, keeping the audio contract auditable. — **done**
3. Drive per-bullet reveal from the first-spoken offset, **falling back to the current equal split** whenever a bullet's words are not located in the narration. — **done**
4. Emit SRT through `SubMaker` as an additional artifact. — **not done**
5. Pygments colouring of the code panel. — **done**
6. Only then: scene-graph triggers (for example, emphasise the `3` chip while the narrator says "exit three"). — **deferred**

Step 3 must degrade exactly as the payload block and the loudness normaliser do: a missing timing degrades the animation, it never fails the build.

### 19.6 Why timings are a sidecar and not part of `tts_script.json`

The design originally put `word_timings` inside `<name>.tts_script.json`. The
implementation writes `<name>.word_timings.json` instead, for a concrete reason:
`tts_script.json` is the *exact input* to the provider, written **before**
synthesis so that a bad script can be inspected and replayed even when TTS fails
or is rejected at the gate. Word offsets only exist *after* the provider answers.
Embedding them would mean rewriting the input artifact with post-hoc data, so the
file would no longer be a faithful record of what was sent.

The sidecar keeps that invariant, and `artifacts.json` points at both, so the
pair is still one auditable unit. A consequence worth stating: a rejected script
has no timings, because synthesis never ran — which is correct, not a gap.

---

## 20. Open defects from the v020 review

Found by the graphic and pipeline reviewers on `mod03_gates_v020` (deck + 10 audio
clips, 9 scenes). These are recorded here rather than in a session log so they stay
in the design contract until closed.

### 20.1 Must fix — closed on `mod03_gates_v021`

| # | Defect | Rule | Closure evidence |
|---|---|---|---|
| 1 | `_ppt_textbox`/`_ppt_para` never set `font.name`, so paragraphs fell through to the theme font (Calibri) instead of Arial | §14 typography | **CLOSED.** Every text-bearing paragraph now declares a face: 142/142 text-bearing paragraphs, **0** without, exactly **2** typefaces (Arial + Courier New). Previously 132/171 without a face and 3 families. |
| 2 | The PAYLOAD panel was not monospace | §14 "Courier New for code only" | **CLOSED.** Payload renders Courier New 16 pt; the 3 remaining 12 pt Courier lines are the pre-existing code panel, code-only and exempt. |
| 3 | `voice.py` defined `m1`–`m6` only, so `M7`/`M8`/`M9` were spoken raw | §9.1 | **CLOSED.** Rules are now generated for `m1`–`m12` from one range instead of a hand-written list, in both voice profiles. 0 raw `M7`/`M8`/`M9` tokens across 10 clips; word-boundary matching still leaves `rasm1` intact. |
| 4 | Titles were hard-cut at 44 characters mid-word, and the broken fragment was also spoken | §14, §17 | **CLOSED.** `text.clip_title` trims on a word boundary, and `plan._normalize_scene_titles` rebuilds a title that sits at the cap while its source-derived `topic` is longer. 0 of 9 plan titles and 0 of 12 slide titles end mid-word; the spoken title is a whole word. |

Two residual warnings remain from the same re-review, neither a regression:

- "module six" is still never spoken because scene 5's title carries no `M6`
  label at all — a content gap in the plan, not a gap in the rule table.
- The repaired scene 6 title is whole-word but still semantically dangling
  ("…structure before"), because the full concept name exceeds the 44-character
  one-line cap. A shorter label chosen at plan time would close both warnings.

### 20.2 Should fix

| # | Defect | Note |
|---|---|---|
| 4 | `source_chunk` was misaligned on ~5/9 scenes: one tolerance scene received the document's "5 big ideas" overview, two pairs of scenes shared a single section | **PARTLY CLOSED** — measured on `mod03_gates_v021`, three separate defects, none of which was the scoring function: (a) the matcher compared against the section *body* only, but `_markdown_sections` returns `(heading, body)`, so the document's most discriminative text — the concept name — was never in the comparison set and "The 11-step precedence" could not match "### 6. The 11-step precedence"; (b) a per-scene argmax let scenes collapse onto one section; (c) `best_score = -1` meant a section with *zero* overlap still won, so an unrelated excerpt could be attached with nothing to show for it. Now: heading and body are both scored, assignment is a global exclusive pass, each scene field is normalised by its own token count so field *size* cannot outvote the title, and a plan-level `source_assignment` records the heading, a content digest and the score, with a confidence floor below which no excerpt is attached. Verified 8/9 correct by inspection, up from ~4/9. **Residual:** scene 4 "M4 Tolerance units" still resolves to the overview, because the title/heading coverage ties and the overview's summary body wins on bullets. A separate heading-as-weighted-field variant fixed scene 4 and regressed scene 6, so it was not adopted — tuning weights until one document reaches 9/9 is overfitting to a single lesson. The overview section is a high-overlap attractor for any query; suppressing it needs a summary-detection signal, not a weight. |
| 5 | Fused token in narration: `ki`+`deterministic` renders as `kideterministic` | **PARTLY CLOSED** — the root cause was not a text-assembly join. The token is model-authored, absent from the source, and survived four repair passes because `tts_unknown_token` only inspects *capitalised* tokens, so a lowercase fusion is invisible to it. `_fused_particle_tokens` now reports `tts_token_fusion` (WARN) with the candidate split, requiring both halves to be independently attested: whole token unknown, prefix a known Hindi particle, suffix a word the plan uses elsewhere. The third condition is load-bearing — without it `killed` and `together` split attractively. On `mod03_gates_v021` it flags 1/9 scenes, the one with the defect. Deliberately **not** auto-repaired: below the auto-repair bar the finding should surface, not mutate the script. |
| 6 | Badge chip text is 15 pt, below the 18 pt body floor | **CLOSED** — raised to 16 pt with a wider box, and the chip gutter widened 0.12 → 0.20 in so `4 CONFIG ERR` stays on one line and the red/green pair no longer vibrates |
| 8 | Column fill averages 61 %, bottom whitespace ranges 0.83–2.72 in across content slides | Matches the "unused space" complaint in §19.4 |
| 9 | Paginated scenes repeat the same header on each page with no continuation marker | Add a "continued" affordance when a scene spans pages |
| 10 | Spoken word count outside 25–65 on two clips (78 and 20) | **CLOSED** — the 25–65 band was the defect, not the clips. The code gate in §9.1 is authoritative (FAIL < 20 or > 90, WARN < 25 or > 70); the reviewer checklist now defers to it, so 78 is correctly a WARN. Recorded in §22.2 |
| 11 | Reviewer checklist still asserts a 5–8 scene band, which conflicts with the concept-driven frame (up to 12) | **CLOSED** — the checklist now requires equality with the plan's own `scene_target` and cites the 5–12 concept frame; the fixed 5–8 rule is gone. Recorded in §22.2 |
| 12 | A saved `plan.audit.json` can record a `plan` path from a different file than the one it ships beside | **CLOSED** — the audit recorded the *input* path while shipping beside the *repaired* plan, with nothing binding them, so a renamed or replaced plan inherited a clean bill of health. `plan_sha256` is now computed from the plan *after* it lands, `check_audit_binding` reports `audit_plan_digest_mismatch` as hard in `verify`, and an audit carrying no digest is reported rather than assumed good. The path is retained as display metadata only: identity comes from content, never from a name. |
| 13 | ~~Reveal sync matches on a bullet's single longest token, so a token repeated in surrounding prose can win the match ahead of the bullet's real occurrence~~ **CLOSED** | Fixed by scoring candidate positions instead of committing to the first hit. `_best_window` scores every occurrence of any bullet token by the weighted fraction of the bullet's whole token set inside a window the size of the bullet's own spoken span; tokens are weighted by inverse frequency *within the scene*, so a word the narrator repeats in setup prose cannot dominate. Verified on the real lesson: scene 6's first reveal moved 2.26 s -> 13.68 s, landing on "engine checks structure" instead of the earlier mention; scene 1's fourth bullet moved 20.9 s -> 25.4 s, off the shared `baseline` anchor. Still 9/9 located, all start times monotonic, and every scene's variant durations still sum to its clip. The search stays monotonic and still returns -1 for a bullet it cannot place, so the §19.5 degrade contract is unchanged. |

### 20.3 Verified good in the same review

Loudness −16.1…−16.5 LUFS (spread 0.4 LU, inside −16 ±2), true peak never worse
than −1.7 dBTP with no clipping, spoken duration 3.71 min against a 4.0 min target
(−7.3 %), `review` exit 0 with `VERDICT: PASS`, idempotent repair, zero banned
repeats, zero slide-meta leaks, `bullet_pages` matching `bullets` on 9/9 scenes,
all slide text Latin-only, `RED` present exactly once in the package (the `1 FAIL`
chip) so the colour rule holds, badge labels byte-exact, and nothing drawn
off-canvas.

---

## 21. Appendix

### 21.1 Artifact set

| Artifact | Contents |
|---|---|
| `<name>.plan.json` | Full plan document with provenance metadata |
| `<name>.sampleN.plan.json` | Every parsed planning sample, including rejected ones |
| `<name>_fixed.plan.json` | Plan after deterministic repair (`verify`) |
| `<name>.audit.json` | Repair audit: before/after counts, idempotency flag |
| `<name>.rejected.audit.json` | Pre-render hard-gate failure with reasons |
| `<name>.rejected.tts_script.json` | Pre-audio gate failure with the exact script |
| `<name>.tts_script.json` / `.script.txt` | Exact TTS input and a readable transcript |
| `<name>.word_timings.json` | Per-word offsets from `WordBoundary`; drives reveal sync (§19.5 step 3) |
| `<name>.vtt` | WebVTT caption track built from the same `WordBoundary` stream; cue times advance by each clip's *measured* duration, since every clip carries trailing silence after its last word |
| `<name>_audio/scene_*.mp3` | Per-scene voice-over for external listening |
| `<name>.pptx` | Lesson deck |
| `<name>.mp4` | Rendered video |

### 21.2 Glossary

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

---

## 22. External review addendum — dependency risk and the enrichment minimum

A second external review raised five concerns after §20.1 was closed. Adopted
items are recorded in §19.2; the one point rejected on evidence is recorded here
with its measurements, so the question does not get re-raised.

### 22.1 TTS provider fragility — accepted, highest priority

`edge-tts` is an unofficial wrapper around a consumer reading service, not a
published API contract, and it can change without notice. Measured exposure in
this repository:

- `pyproject.toml` pinned a **floor**, not a version: `edge-tts>=6.1`, while the
  verified working build is 7.2.8, so a minor release could change behaviour
  underneath us. — **resolved**: now pinned to `edge-tts==7.2.8`.
- `video.synth_scenes` gathers `_scene_audio` over all clips with no retry and no
  backend seam, so the first transport error aborts the whole media stage. —
  **partly resolved**: the `tts-check` probe now fails fast in CI; the backend
  seam and per-clip retry remain open (item 4 below).

Required, in order:

1. Pin the known-good version rather than a floor. — **done** (`==7.2.8`)
2. Add a `tts-check` health probe that synthesises a short fixed phrase and
   asserts a plausible duration, so a silent provider change surfaces as a
   failed probe rather than an outage. — **done**. The probe also asserts that
   `WordBoundary` events are still returned, because §19.5 reveal sync silently
   degrades to an even split if that stream disappears; a provider change that
   kept audio but dropped boundaries would otherwise pass unnoticed.
3. Run that probe in CI so the dependency is exercised continuously rather than
   discovered during a render. — **done** (`.github/workflows/render.yml`)
4. Introduce a backend seam so a second engine can be registered without touching
   the pipeline. Piper (fully offline) is the candidate; a fallback must never
   substitute silently, because the reproducibility contract in §6.1 pins the
   provider, voice, rate, pitch and volume in the plan. — **open**

### 22.1.1 Provider health gate

`studio tts-check` is a separate subcommand, not part of `build`/`render`, so it
can run on a schedule and in CI without rendering a lesson. It fails on a
transport error, on an empty clip (< 1 KiB), on zero boundary events, and on an
implausible offset (> 60 s), and it exits non-zero. Its output is deliberately
short and stable so a CI log diff shows what actually moved:

```
  provider  : edge-tts voice=hi-IN-SwaraNeural rate=-8%
  audio     : 27072 bytes in 1.8s -> /tmp/.../tts_check.mp3
  word sync : 6 boundary events
  TTS probe : PASS
```

Note that `tts-check` intentionally exercises the **live** provider. That makes
it unsuitable for an air-gapped or offline build, so CI runs it as a separate
non-blocking-for-release step; `build` itself must keep working when the network
is unavailable.

### 22.2 Policy drift between the gates and the reviewer checklist — accepted

Two checks are defined twice with different thresholds, which makes the reviewer
agent raise false positives against a stale rule. Resolution: the code gate in
§9.1 is authoritative, and the reviewer checklist in `opencode.json` has been
corrected to defer to it. One definition, one threshold.

| Check | Code gate (§9.1) — authoritative | Old reviewer rule — removed |
|---|---|---|
| Spoken word count | fail < 20 or > 90; warn < 25 or > 70 | "25–65 hard band, warn 20–75" |
| Scene count | must equal the plan's own `scene_target` (concept frame, 5–12) | fixed 5–8 range |

The word-count rule mattered in practice: a scene at 78 words is a WARN by
design, and the old checklist reported it as a FAIL, which reads as a blocking
defect on a scene that is merely verbose. The scene-count rule mattered more —
the 5–8 range predates the concept-driven frame, so every lesson with more than
8 headings was reported as non-compliant with a rule that no longer exists.

### 22.3 Font licensing and portability — rejected on evidence

The review proposed replacing Arial and Courier New with Liberation Sans/Mono on
licensing grounds. The premise does not hold for this pipeline:

- The deck embeds **no font data**: the package contains no font parts and
  `ppt/presentation.xml` declares no `embeddedFontLst`. A `.pptx` references
  fonts *by name* and the opening application resolves them, so there is no
  font-licensing exposure in the artifact and no build-time font dependency.
- Declaring Liberation would *degrade* the primary consumer: PowerPoint on
  Windows and macOS ships Arial and does not ship Liberation, so the substitution
  would get worse rather than better.
- The raster path is already on a free font. `slides.py` loads DejaVu Sans and
  DejaVu Sans Mono (Bitstream Vera license, verified present on this host) and
  bakes glyphs into PNG, so the shipped video has no font dependency either. No
  Liberation font is installed here, so the proposal would have introduced, not
  removed, a substitution.

The review's underlying observation — that fit analysis depends on which face is
actually used — is already handled conservatively: the graphic reviewer measures
widths with DejaVu, which runs 6–10 % wider than Arial, so a title that fits
under that proxy also fits in Arial.

### 22.4 The enrichment minimum

Ordered by cost and irreversibility. M0 is blocking because it protects the
pipeline rather than improving it.

| Tier | Items | Gate to the next tier |
|---|---|---|
| **M0** | pin `edge-tts`; `tts-check` probe; probe in CI; TTS backend seam; `silencedetect` pre-render audio QA; correct the two stale reviewer thresholds | probe green in CI; a deliberately truncated clip is caught by `silencedetect` |
| **M1** | WordBoundary capture → persist in a `.word_timings.json` sidecar (see §19.6) → reveal alignment with the equal-split fallback; SRT; burned-in captions | reveal timing matches the spoken word within tolerance, and falls back cleanly when timings are absent |
| **M2** | Pygments colouring; scoped uniform `zoompan`; SVG diagram backend only | deck and video stay in agreement; no slide shorter than its audio |
| **Hold** | avatars, model-based grounding, Mermaid-CLI, Manim, Whisper family, full renderer rewrite | only reopened with new evidence |

### 22.5 Milestone status

M0 and the M1/M2 items below are implemented and verified against
`mod03_gates_v021`; the remainder is open. "Verified" means exercised by a real
render, not by inspection.

| Item | State | Evidence |
|---|---|---|
| `edge-tts` pinned to `==7.2.8` | done | live probe ran against 7.2.8 |
| `tts-check` probe | done | PASS, 6 boundary events, 27 KB clip |
| probe in CI | done | `render.yml` step added |
| stale reviewer thresholds corrected | done | `opencode.json` A1.1 / A2.6 now defer to the code gate |
| `silencedetect` audio QA | done | warning-only; 10/10 clips clean on v022 |
| TTS backend seam + per-clip retry | **open** | not started |
| WordBoundary capture | done | 383 timings over 10 clips → `.word_timings.json` |
| reveal alignment | done | 9/9 scenes aligned, all monotonic, no audio lost. Matching scores candidate positions by weighted coverage in a window the size of the bullet's own spoken span, weighted by inverse frequency within the scene. Verified on scene 6: the first bullet's reveal moved 2.26 s -> 13.68 s, from a passing mention in the setup prose to the words actually spoken |
| SRT via `SubMaker` | **open** | not started |
| burned-in captions | **open** | not started |
| Pygments colouring | done | recolour-only, verified to preserve text exactly |
| WebVTT caption track | done | 53 cues over a 383-word lesson, spanning 4:09 against 223 s of audio plus nine pauses |
| digest-bound audit artifacts | done | `audit_plan_digest_mismatch` is hard in `verify`; an audit with no digest is reported |
| lowercase token-fusion detection | done | flags 1/9 scenes on the real plan; warns with a candidate split, does not rewrite |
| measured lesson duration | done | ffprobe-measured and authoritative once audio exists; pre-audio estimate retained, labelled as an estimate. Bands are symmetric (80/92/115/150 %) — the first version only checked the short side, so a fresh build at 109 % of target reported PASS and so would 300 % |
| coverage floor in CI | done | 58 % against the studio package; per-module table reported so the trend stays visible |
| scoped `zoompan` | **open** | not started |
| SVG diagram backend | **open** | not started |
| TTS backend seam + capability model | **open** | accepted in §22.6, not started |
| `SourceEvidenceBundle` for repair | **partly done** | `source_assignment` now carries heading, content digest and score per scene, and a weak match attaches nothing (§20.2 defect 4). The remaining part is making repair read that record rather than the raw `source_chunk` string. |

The two still-open M0 items (backend seam, retry) are the reason M0 is not yet
called complete; the enrichment tiers can proceed because they are independent of
provider choice.

### 22.6 External review, first pass — disposition

An external review of §19/§22 proposed a lesson-level duration budget, a
constrained source-assignment stage, a layered token-fusion detector, monotonic
weighted reveal alignment, a TTS backend seam with a capability model, a caption
sidecar, and digest-bound audits. Disposition, with the reason each way:

| Proposal | Disposition | Reason |
|---|---|---|
| Lesson-level duration budget with FAIL/WARN bands | **corrected on receipt** | The motivating number was wrong — see below. The *principle* survives: a quality target is not the same instrument as a hard safety invariant, and the per-scene word floor is not a duration control. |
| Assign source chunks after scene titles exist; give repair a resolved `SourceEvidenceBundle` instead of a raw string | **accepted, open** | Highest-severity structural item, and the only one that changes a gate rather than a metric. `source_chunk` is dual-use: it supplies provenance evidence, hydration material and enrichment, so a misplaced string is hidden authority. |
| Layered fusion detector, no auto-repair below a high-confidence bar | **adopted, shipped** | See §20.2 defect 5. Detection plus a candidate split; no silent rewrite. |
| Monotonic weighted alignment for reveal sync, instrument before choosing | **already shipped** | Implemented in `1e70bb7` as candidate scoring by weighted coverage with a scene-local inverse-frequency weight; the review was working from the pre-fix state. |
| TTS backend seam with an explicit capability model | **accepted, open** | Agreed with the emphasis: the abstraction must not be shaped around Edge's `WordBoundary`, or a backend without native timings silently degrades reveal sync. Capability differences are the contract, not the implementation. |
| WebVTT caption sidecar from existing word timings | **adopted, shipped** | Cues advance by measured clip duration, not by last-word offset, because every clip carries trailing silence. |
| Digest-bound audit artifacts | **adopted, shipped** | See §20.2 defect 12. |
| 30–50 scene human-annotated reveal-sync ground truth | **rejected as specified** | The cost is wrong, not the principle. `_rebuild_scene_narration` speaks bullets verbatim, so those scenes have *exact* ground truth for free; only the non-verbatim minority needs annotation. Instrument the free set first, then decide what still needs a human. |
| Insert deliberate spoken anchors so each bullet has a stable phrase | **deferred** | It makes the renderer dictate narration content, inverting the layering. Acceptable as an optional source-grounded concept phrase; not as a default requirement. |
| Lengthen narration to hit the duration target | **rejected** | Padding satisfies a gate without improving teaching. A real under-run is repaired only from source-grounded material — which is why the target was wrong in the first place. |

#### The two premises the review inherited were both wrong

Worth recording, because it is the failure mode this document already has a name
for (§22.2, policy drift): an external reviewer can only reason from the numbers
we hand it, and two of ours were wrong.

- It quoted a "7/9 scenes aligned" figure that was an artifact of a measurement
  harness that passed a hardcoded 20 s scene duration. Measured against real clip
  durations it is 9/9.
- It built an entire duration-budget design on a "2.8 min vs 4.0 min, −30 %"
  figure that was an *estimate* from a hardcoded 135 wpm. ffprobe on the ten
  rendered clips measures 223.0 s = 3.72 min = **93 %** of target, which passes
  under the review's own bands. `LOUDNESS_WPM` is recalibrated to the measured
  103.1, and `_render_media` now treats the measured duration as authoritative.

Neither was the reviewer's error. The lesson for this pipeline: a metric that no
one has verified against a rendered artifact is a claim, not a measurement, and
it will be repeated confidently by the next reader — including a model.

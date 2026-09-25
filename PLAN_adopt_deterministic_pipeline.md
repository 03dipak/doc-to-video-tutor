# Adopt a Deterministic Generation and Evaluation Pipeline

**Goal:** Eliminate manual slide/script/audio auditing for every document by enforcing layout, narration, and quality rules at generation time, then validating with automated CI gates.

**Status:** Phases 0 and 1 are **shipped**. Phases 2 and 3 are **substantially implemented** as a side effect of the narration-safety work; Phases 4 and 5 remain open.

| Phase | Scope | Status |
|---|---|---|
| 0 | Pydantic dependency | **DONE** |
| 1 | Strict schema enforcement at generation time | **DONE** |
| 2 | Content / presentation separation | PARTIAL — pagination + invariant shipped; AST formalization open |
| 3 | Dedicated TTS normalization layer | PARTIAL — `speech.py` is already the single normalization point; SSML + token coverage open |
| 4 | Automated post-generation evals (`evals.py`) | TODO |
| 5 | Pipeline wrapper + CI fail-fast | PARTIAL — `build` wrapper and Docs-as-Code workflow exist; eval-gated exit codes open |

---

## Why this adoption

- Manual inspection of 14 slides, 13 TTS clips, and audio peaks does not scale to $N$ documents.
- Hardcoded strings, ad-hoc pagination, and scattered validation allow regressions to reach rendered assets.
- The codebase already had fragments (pagination, pronunciation maps, `validate.py` gates), but they were not unified under a strict schema and automated evals.

---

## Current repo state (baseline, post-Phase-1)

- `plan_lesson()` emits a loose `dict` that is now schema-validated before it can be repaired or gated.
- `schema.py` owns the structural contract; `validate.py` owns the semantic contract.
- `slides.py` / `pptx.py` paginate bullets (4 per page) and render diagrams/code context.
- `voice.py` has `PronunciationRule`; `speech.py` expands identifiers, repairs punctuation, and audits the result.
- `validate.py` has hard gates (`_render_blocking_problems`, `guard_plan`).
- `config.py` holds `BRAND_FOOTER`.
- `pyproject.toml` includes `pydantic>=2.0`.

---

## Phase 0 — Add Pydantic dependency — **DONE**

- [x] `pydantic>=2.0` added to `pyproject.toml`.
- [x] `instructor` deliberately **not** added; Pydantic validation is sufficient and keeps the dependency surface minimal.
- [x] Tests import and exercise `schema.py`.

---

## Phase 1 — Strict schema enforcement at generation time — **DONE**

### Shipped

1. **`src/doc_to_video_tutor/studio/schema.py`**
   - `SlideScene`: `title`, `narration`, `bullets` (max 4), `steps`, `flow`, `visual_diagram`, `code_snippet`, `code_context`, `analogy`, `design_decision`, `section`, `topic`, `source_refs`.
   - `LessonPlan`: `title`, `opening`, `scenes` (min 1), `takeaways` (capped at 8).
   - `TTSClip`: `role`, `index`, `title`, `spoken`, `word_count`, `narration`.
   - `model_config = ConfigDict(extra="ignore")` so richer model output stays forward-compatible.

2. **Validators**
   | Validator | Rule |
   |---|---|
   | `normalize_sequence_fields` (`mode="before"`) | `None`, `""`, and tuples normalize to lists for `bullets` / `steps` / `flow` — the model emits `""` as often as `[]` |
   | `validate_bullets_max` | raise above 4 bullets; reject non-Latin bullet text |
   | `reject_non_latin_slide_text` | every slide-visible string **and every list item** must be Latin script |
   | `require_non_empty_title` | a scene title may not be blank |
   | `validate_source_refs` | strip empties |
   | `normalize_narration` | insert a space after unspaced colons (`word:word` → `word: word`); fix `.jso` → `.json` |
   | `normalize_code_context` | strip raw internal rationale prefixes (`Design choice dekho:`, `Faisla hua ki:`, …) |
   | `reject_non_latin_plan_text` | plan `title` and `opening` must be Latin script |
   | `validate_takeaways` | strip and cap |

3. **Integration** — `_validate_and_normalize_plan()` runs `LessonPlan.model_validate` on both the primary parse and the post-patch fallback parse inside `plan_lesson()`. A `ValidationError` becomes a `RuntimeError`, which the CLI already treats as a resample trigger, so a malformed candidate is never patched into a "valid" plan.

4. **Post-schema cleanup retained** — the deterministic repair chain still runs after validation; it is a cleanup pass, not the gate.

### Deviations from the original plan (deliberate)

| Original assumption | Shipped behaviour | Why |
|---|---|---|
| `narration` required non-empty at plan time | `narration` may be `""` | The planner is instructed to leave narration blank; a dedicated narration pass writes it afterwards. Requiring it blocked every candidate. |
| `LessonPlan.scenes` fixed length `scene_target` | schema enforces only `min_length=1` | Exact frame equality is a **semantic** contract owned by `_scene_count_problem`, which understands the concept-driven target and its ±1 tolerance at the 10+ ceiling. Encoding it in the schema would reject every dense-document plan. |
| Schema failure = fail fast | schema failure = resample trigger | Failing the whole build on one malformed field wastes the remaining samples; the CLI resamples and persists every parsed sample. |

### Acceptance criteria

- [x] A plan with more than 4 bullets per scene fails validation before rendering.
- [x] Unspaced colons and `.jso` typos are normalized by the validator.
- [x] `plan_lesson` retries on schema errors instead of shipping the plan.
- [x] `flow: ""` / `steps: ""` / `bullets: ""` normalize to `[]` (regression test added after the real `v011_016` failure).
- [x] Non-Latin slide text is rejected at generation time, not only at render time.

### Evidence

- Regression tests: `tests/test_non_latin_slide_text.py` (schema validation cases), plus `tests/test_repeat_safety.py` and `tests/test_speech.py` for the deterministic repair paths.
- `uv run ruff check src tests`, `uv run mypy src`, and `uv run pytest -q` are green.
- Real-plan evidence: the `v011_016` `flow: ""` class and the `v011_017` non-Latin opening/bullet classes are now caught or normalized before the review loop.

---

## Phase 2 — Total separation of content and presentation — PARTIAL

### Already implemented
- [x] `LessonPlan` is the content contract; renderers consume plan fields only.
- [x] `_scene_bullet_pages` / `_paginate_plan_slides` derive pages of at most 4 bullets.
- [x] `slides._scene_pages` expands a scene into its page variants; `pptx.build_pptx` and `slides.render_scenes` both use it, so slide counts and counters derive from the same pagination.
- [x] Bullet overflow truncates silently (`_with_overflow`) with no pipeline metadata on the slide.
- [x] Hard invariant: `_scene_metadata_problems` fails the plan when `flatten(bullet_pages) != bullets` or a page exceeds 4 items, and pagination is re-derived after every bullet mutation.

### Open
- [ ] Expose a single `paginate(scene, max_bullets=4)` layout helper and retire the two current implementations (`plan._scene_bullet_pages`, `slides._scene_pages`).
- [ ] Assert in one place that no renderer reads a layout hint the model produced.

---

## Phase 3 — Dedicated TTS normalization layer — PARTIAL

### Already implemented
- [x] `speech.build_tts_script` is the single producer of spoken text; `audit_tts_script` is the single validator.
- [x] Identifier/symbol/digit expansion via `PronunciationRule` (longest match first): `active.jso` → `active dot json`, `1/(n+1)` → `one over n plus one`, `Exit 3` → `Exit three`, `.py` → `dot py`.
- [x] Slide-listing phrases stripped before synthesis (`strip_slide_meta`).
- [x] Hinglish rationale prefixes and raw list numbering removed before synthesis.
- [x] Audit gates every clip before the provider is called; a `FAIL` writes `*.rejected.tts_script.json` and exits 1.

### Open
- [ ] `CI/CD` → `C I C D` and `v011` → `version 0 1 1` (currently unexpanded).
- [ ] Optional SSML (`<prosody>`, `<say-as>`) for numbers and code.
- [ ] Promote pronunciation rules to a named `TTS_NORM` config layer for data-only edits.

---

## Phase 4 — Automated post-generation evals (CI regression gates) — TODO

### Tasks
1. Create `src/doc_to_video_tutor/studio/evals.py` with deterministic checks:
   - **Concept Coverage**: compare source concepts against scene titles/bullets (token overlap); return `Exit 4` if critical steps omitted.
   - **Visual Bounding Check**: headless render the deck to images; check text bounds do not exceed slide height; return `Exit 3` on overflow.
   - **TTS Warning Parse**: parse `tts_script.json` audit; return `Exit 3` on `FAIL`, and surface `WARN` counts.
   - **Source Leak Check**: scan rendered text and audio-script text for raw source metadata markers; return `Exit 3` on leaks.
2. Add a CLI command:
   ```bash
   python -m doc_to_video_tutor.studio evals <plan.json> <tts_script.json>
   ```
   returning `0` (pass), `3` (structural/system error), or `4` (coverage/data error).

### Acceptance criteria
- Evals fail fast and name the offending artifact path.
- Concept coverage misses return exit code `4`.
- Visual overflow and TTS failures return exit code `3`.

### Note
Most of these checks already exist inside `validate.py` and `speech.audit_tts_script`; Phase 4 is largely a packaging and exit-code contract, not new logic.

---

## Phase 5 — Pipeline wrapper and CI fail-fast — PARTIAL

### Already implemented
- [x] `python -m doc_to_video_tutor.studio build <doc>` is the single-command wrapper (ingest → schema-enforced plan → gates → media).
- [x] `.github/workflows/render.yml` regenerates the lesson from source markdown on push and uploads plan, script, and media artifacts.

### Open
- [ ] Insert the Phase 4 eval gate into `build` and the workflow so CI fails on an eval violation rather than uploading a failing lesson.
- [ ] Make CI assert the deterministic fast loop first (`verify` → `review`) before spending TTS/render time.

---

## Suggested execution order

1. **Phase 0 + Phase 1** — shipped.
2. **Phase 4 packaging** (next) — highest leverage per unit of work, because the checks already exist and only need a stable exit-code contract.
3. **Phase 5 CI gate** — wire the packaged evals into the workflow.
4. **Phase 2 consolidation** — one pagination helper, one layout contract.
5. **Phase 3 remainder** — token coverage and optional SSML.

---

## Open decision

- Close out Phases 2 and 3 as "sufficient for the free-7B ceiling" and invest in Phase 4/5, or finish the AST and SSML work first?

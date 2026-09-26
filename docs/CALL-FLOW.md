# Call flow — `doc-to-video-tutor` studio pipeline

A map of how a lesson gets from Markdown to a deck, an MP4, and a set of
artifacts. This is a **map, not a design document** — it records what calls what,
so that a change to ordering is visible rather than inferred.

Every reference carries a `file:line` anchor so it can be checked and so this
document goes stale *visibly* rather than silently. Line numbers are a snapshot
of commit `3f99ee1`; treat them as a starting point for a search, not as truth.

For why the pipeline is shaped this way, see `LLD-narration-voice.md`. For what
"good" means, see that document's acceptance criteria (§17).

---

## 1. Entry and dispatch

```
python -m doc_to_video_tutor.studio <command> [options]
  └─ studio/__main__.py
     └─ cli.py:192  main(argv)
        ├─ argv normaliser (cli.py:199): an unrecognised first argument is
        │  rewritten to ["build", *argv], so a bare document path builds
        └─ argparse dispatch
```

| Command | Purpose | Calls a model? |
|---|---|---|
| `build` | full pipeline from source documents | **yes** — 4 call sites (§4) |
| `render` | media + deck from a saved `.plan.json` | no |
| `verify` | deterministic repair of a saved plan | no |
| `review` | audit a saved plan, print a verdict | no |
| `tts-check` | provider health probe | no |

Only `build` calls a language model. The other four are offline by construction,
which is what makes them the right tools for iterating.

---

## 2. `build` end to end

```
cli.py:492  (args.cmd == "build")
│
├─ SAMPLE LOOP  (cli.py:560, max_samples)
│    └─ per sample:
│       ├─ plan_lesson(...)                          plan.py:1524
│       ├─ build_tts_script(plan, voice)              speech.py:515
│       ├─ atomic_json_write("<out>.sampleN.plan.json")
│       └─ guard_plan(...) minus _SOFT_PREFIXES       validate.py:327
│          └─ hard problems → resample; all samples exhausted → exit 1
│
├─ atomic_json_write("<out>.plan.json")   ← plan persisted BEFORE any media
├─ _tts_blocking_findings(build_tts_script(...))
│    └─ FAIL → write .rejected.tts_script.json, exit 1 (provider never called)
├─ _render_blocking_problems(plan)                  validate.py:297
│    └─ non-empty → .rejected.audit.json, exit 1
├─ _write_tts_artifacts(out_base, script, args)    cli.py:134
│    └─ <out>.tts_script.json + <out>.script.txt
└─ _render_media(...)                              video.py:591
     ├─ [2/5] render_scenes(plan, work)             slides.py:511
     ├─ [3/5] synth_scenes(script, work, voice)     video.py:260
     ├─ [4/5] build_pptx(plan, out)                 pptx.py:479
     └─ [5/5] assemble_video(...)                   video.py:504
```

The plan is written before media on purpose: a blocked build still leaves the
expensive artifact behind so `verify` can repair it without paying for another
model call.

---

## 3. `plan_lesson` — the deterministic core

This is the longest function in the pipeline (`plan.py:1524`) and the one whose
ordering matters most. Steps are listed in execution order.

### 3.1 Source preparation

| Line | Step | Note |
|---|---|---|
| 1553 | `_strip_source_metadata_blocks` | citation blocks are removed **before** the model sees them, so it cannot quote a value that only exists in a reference footer |
| 1556 | `_plan_scene_target` / `_concept_groups` | the scene frame comes from source headings, not from the model |
| 1582 | `_contrast_sentences` / `_repeated_terms` | real source prose is harvested for design-decision grounding |

### 3.2 Model call 1 of 3 — the plan

| Line | Step | Note |
|---|---|---|
| 1606 | `_ask_llm_stable` | `llm.py:107`; internal retry ladder, `tries=2` |
| — | `_parse_plan_json` (`llm.py:227`) | scans for the first *complete* JSON object, so markdown fences and trailing prose are tolerated |
| — | `_was_truncated` (`llm.py:103`) | a **projection**: naive brace/bracket counting, so it can misjudge a plan whose string values contain braces |
| — | on `RuntimeError` | `_dump_parse_failure` + re-probe with a larger completion budget |

### 3.3 Normalise, then patch

| Line | Step |
|---|---|
| 1661 | `_normalize_sections` |
| 1662 | `_sanitize_opening` |
| 1663 | `_trim_scene_overflow` |
| 1692 | `_scene_problem_map` → targeted patch round, other scenes frozen |

### 3.4 Content repair

| Line | Step | Guards against |
|---|---|---|
| 1762 | `_dedupe_plan_bullets` | exact duplicate bullets across scenes |
| 1763 | `_drop_repeated_bullet_clauses` | a clause repeated from an earlier bullet in the same scene |
| 1768 | `_prune_bullet_takeaway_echo` | bullets near-copying a takeaway |
| 1772 | `_harvest_takeaways` | |
| 1773 | `_fix_placeholder_titles` | a title that just restates its section |
| 1777 | `_sanitize_design_decisions` | degenerate or repeated decision text |
| 1781 | `_source_design_decisions` | fills empties from real source contrast sentences |

### 3.5 Lineage

| Line | Step | Note |
|---|---|---|
| 1791 | `_annotate_source_chunks` (`plan.py:1174`) | scores heading **and** body, assigns globally and exclusively, and writes `plan["source_assignment"]` with a heading, a content digest, a score, and a confidence floor |
| 1795 | `_dedupe_visual_diagrams` | drops a diagram an earlier scene already showed — the visual-channel twin of the cross-scene narration dedup |

### 3.6 Narration

| Line | Step | Note |
|---|---|---|
| 1803 | `_narrate_plan` | **model call 2 of 3** — the dedicated narration pass |
| 1812 | `_sanitize_plan_source_leaks` | |
| 1814 | `_enforce_unique_narration_trigrams` | the sentence-aware enforcer |
| 1815 | `_repair_unsafe_narrations` | → `_rebuild_scene_narration` (`narration.py:473`) |
| 1816 | `_repair_thin_narrations` | same rebuild, driven by the audio gate |
| 1817 | `_trim_narration_word_count` (`narration.py:786`) | ceiling 90, which is also the hard-fail threshold |

### 3.7 Final gate consultation

| Line | Step |
|---|---|
| 1821 | `_drop_ungrounded_slide_text` |
| 1883 | `_render_blocking_problems` — consulted so the closing message cannot claim "proceeding anyway" when the gate will refuse |

---

## 4. The three model calls, and nothing else

| # | Call site | Produces | Failure handling |
|---|---|---|---|
| 1 | `_extract_topics` (llm.py:272), invoked at cli.py:524 | 1–3 central topics used as an on-topic constraint | `RuntimeError` is caught and the build continues with no topic constraint |
| 2 | `plan_lesson` (plan.py:1584), retried at 1607 | the lesson plan | re-probe with a larger completion budget, then the sample loop resamples |
| 3 | patch round — `_patch_scene` (plan.py:1411) and `_patch_opening` (plan.py:1457), driven by `_scene_problem_map` (plan.py:1670) | repairs for specific offending scenes | a scene whose patch fails to parse keeps its original |
| 4 | `_narrate_plan` (narration.py:915) | the whole spoken track | falls back to the deterministic rebuild, so a bad narrator can never block a build |

Call 1 is **conditional and cached**: `cli.py:524` runs it once and reuses the
result across the read-verify-resample loop, saving one call per sample. It is
listed here because it is a real model call on the first build, not because every
build pays for it. Calls 2 and 3 are retried internally by `_ask_llm_stable`
(llm.py:186) when the model degenerates into a repetition loop, so the *number* of
HTTP requests exceeds the number of rows here.

Everything else — tokenizer, gates, repair, pagination, layout, caption export —
is deterministic. That is the property that makes `verify` and `review` useful:
they re-derive every guarantee in this table without a model.

---

## 5. Layer boundaries

### 5.1 `speech.py` owns the written → spoken boundary

```
speech_expand(text, rules)              speech.py:153
  └─ expand_snake_case                 speech.py:139
spoken_token_set(text, rules)           speech.py:90
```

Two consumers depend on comparing text *in the spoken alphabet*, and both were
written after getting it wrong:

- `_spoken_tokens` (`video.py:109`) — reveal matching. A badge says `3`, the
  narrator says "exit three"; comparing the raw forms never matches.
- `spoken_token_set` (`speech.py:90`) — the unspoken-claim gate, same asymmetry.

### 5.2 `validate.py` owns severity

| Function | Line | Produces |
|---|---|---|
| `guard_plan` | 327 | plan-level problems, hard and soft |
| `_render_blocking_problems` | 297 | recomputed from the plan's own content; refuses the build |
| `check_audit_binding` | 179 | audit digest vs the plan beside it |
| `_unspoken_visual_claims` | 227 | structured visual facts the narration never names |
| `review_plan` | 381 | the `VERDICT:` line |

### 5.3 `narration.py` owns Layer A

`_nar_tokens` (`text.py:129`) is the single tokenizer; `_protected_terms`
(`narration.py:57`) is the shared allowlist. Every repeat rule, the enforcer and
the rebuild are built on those two.

### 5.4 The measured-reveal path

```
_scene_audio(text, ...)                  video.py:30   streams WordBoundary
_bullet_start_times(bullets, timings)   video.py:179
  └─ _spoken_tokens                      video.py:109
  └─ _stream_weights                     video.py:130   inverse frequency, scene-local
  └─ _best_window                        video.py:148   weighted coverage, monotonic
_variant_durations(...)                  video.py:223   even-split fallback
```

---

## 6. Media and deck

### 6.1 `_render_media` (video.py:591) — four concerns in one function

| Stage | Call | Produces |
|---|---|---|
| [2/5] | `render_scenes` (slides.py:511) → `_scene_pages` (463) → `_slide_variants` (497) → `render_slide` (183) | PNG per reveal variant |
| [3/5] | `synth_scenes` (video.py:260) → `_scene_audio` (30) | MP3 per clip + per-word timings |
| | `_audit_clip_health` (383) | ffmpeg `silencedetect`, warning only |
| | `_normalize_loudness` (62) | ffmpeg `loudnorm`, per clip |
| | `_report_measured_duration` (354) | ffprobe duration, banded against the target |
| | `_write_webvtt` (419) | `<out>.vtt` via `SubMaker` |
| [4/5] | `build_pptx` (pptx.py:479) | `<out>.pptx` |
| [5/5] | `assemble_video` (video.py:504) | `<out>.mp4` |

Because these four concerns share one function, `--skip-video` still runs the
Pillow and PPTX stages. There is no pptx-only flag; that path is a one-liner
against `build_pptx` directly.

### 6.2 `build_pptx` (pptx.py:479) — content-driven layout

```
_paginate_by_height(page, budget)       pptx.py:173   paginate on measured height
_takeaway_pages(takes, budget)          pptx.py:219   counted before rendering
  └─ _shorter_column                    balances the two takeaway columns
_RowStack                               pptx.py:123   one vertical budget per slide
  └─ reserve(height, priority, label)   REQUIRED / SUPPORTING / OPTIONAL
_est_text_height(text, width, size)     pptx.py:116   every box sized from content
_audit_layout(out_path)                 pptx.py:787   overflow, overlap, safe area
```

Two rules the layout rests on:

- **Measure, then paginate, never shrink below the font floor.** Overflow becomes
  another slide, not smaller text.
- **One shared budget.** A block that advances the local cursor instead of
  reserving leaves the stack's cursor stale, and the stack then admits a block
  into space that does not exist.

---

## 7. Artifact set

| Artifact | Written by | Read by |
|---|---|---|
| `<out>.plan.json` | `build` after the sample loop | `render`, `verify`, `review` |
| `<out>.sampleN.plan.json` | every parsed sample, accepted or not | manual inspection |
| `<out>.audit.json` | `verify` | `check_audit_binding` |
| `<out>.rejected.audit.json` | `build` at the pre-render gate | manual |
| `<out>.rejected.tts_script.json` | `build` at the pre-audio gate | manual |
| `<out>.tts_script.json` / `.script.txt` | `_write_tts_artifacts` | `render`, `_render_media` |
| `<out>.word_timings.json` | `_render_media` | reveal sync, caption export |
| `<out>.vtt` | `_write_webvtt` | players, editors |
| `<out>_audio/scene_*.mp3` | `synth_scenes` | external listening |
| `<out>.pptx` | `build_pptx` | the learner |
| `<out>.mp4` | `assemble_video` | the learner |

---

## 8. Known ordering gaps

Both were found by tracing this flow, not by a failing test. Neither is claimed
to be a live defect; both are recorded because the ordering is what makes them
possible.

**8.1 The chunk assignment feeds the rebuild through an implicit dependency.**
`_annotate_source_chunks` runs at plan.py:1769. `_rebuild_scene_narration`
(narration.py:473), invoked 24 lines later at 1793 and 1794, reads
`scene["source_chunk"]`. `_rebuild_scene_narration` reads it at narration.py:439 and 532. Nothing in
either signature records the dependency, so a future reordering would fail
silently: the rebuild would simply find no hydration material. This is the mechanism behind the `source_chunk` misalignment
defect.

**8.2 CONFIRMED — narration speaks about slide text the grounding gate removed.**
`_drop_ungrounded_slide_text` runs at plan.py:1799, *after* the narration pass
(1781) and all narration repair (1792–1795), so it removes bullets the narrator
was given.

This was unmeasurable until `mod03_gates_v012_013`: the log recorded only a
*count* and the plan was written after the drop, so the removed text survived
nowhere. The drop now records what it removed into `plan["dropped_slide_text"]`
(scene, field, text), and `_narration_references_dropped_text`
(validate.py) compares the scene's narration against it on canonical 4-grams.

That build proved it, twice:

| scene | dropped bullet | still narrated |
|---|---|---|
| 5 | "Ensures that a single outlier cannot bypass the gate." | "ensures that a single" |
| 7 | "Ensures that structural integrity is maintained." | "ensures that structural integrity" |

So the learner hears a justification for a claim the slide no longer shows. The
finding is soft, because the remedy is a narration decision and silently
rewriting the script to match a pruned slide is content injection. The
**unresolved** question is the repair: re-running the narration pass after the
drop costs another model call, and dropping the narration sentence risks the
teaching-claim floor. That is a design decision, not a bug fix.

**8.3 The repair chain is order-dependent and the order is only in comments.**
Fifteen-plus steps, several of which rewrite the fields later steps read. The
order is documented in LLD §7.5, but nothing enforces it, and `_RowStack` in
`build_pptx` is the one place in the pipeline where a similar ordering mistake
has already been made and had to be fixed.

---

## 9. Where severity is decided — and the known drift

Severity is currently derived in **two** places:

- `validate.py` computes hard and soft problems from the plan's content.
- `_SOFT_PREFIXES` (`config.py`) is a tuple of **string prefixes**. The
  review-before-build loop splits hard from soft with
  `problem.startswith(prefix)`.

A finding whose message does not *begin* with a registered prefix is counted as
hard. That has bitten twice: a soft `unspoken visual claim` prefix that buried
its own name and burned three model samples, and a repeating-phrase finding that
announced "proceeding anyway" and then refused the build.

`soft_finding()` (`validate.py`) now constructs advisory messages so they lead
with their prefix, and a test asserts the convention. The structural fix — one
severity per condition, decided in one place, with the finding carrying a
severity field instead of matching on message text — is **open**. It is the same
policy-drift shape as the stale scene band and the stale word-count band, and it
has now produced three separate defects.

---

## 10. Quick reference

| Question | Where to look |
|---|---|
| Why is my narration repeating? | `narration.py:473` rebuild, `plan.py:1792` enforcer |
| Why did a scene lose its diagram? | `plan.py:1262` `_dedupe_visual_diagrams` |
| Why did a scene lose its source excerpt? | `plan.py:1174` + `plan["source_assignment"]` |
| Why is a slide's box the wrong height? | `pptx.py:116` `_est_text_height` |
| Why is a card off the canvas? | `pptx.py:123` `_RowStack`, and §8 above |
| Why does the build refuse at the end? | `validate.py:297` `_render_blocking_problems` |
| Why is the duration figure wrong? | `video.py:354` measured vs `config.LOUDNESS_WPM` projected |
| Why did the caption track look wrong? | `video.py:419` `_write_webvtt` |
| Which parts call a model? | §4 — three call sites, all in `plan.py` |

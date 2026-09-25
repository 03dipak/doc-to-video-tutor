"""plan_lesson() planning pipeline and plan-analysis helpers."""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path

from .config import (
    _CONTRAST_RE,
    _DD_GENERIC,
    _DEVANAGARI,
    _ENUM_HINTS,
    _SECTIONS,
    AUTO_TRIM_MAX_SCENES,
    LLM_MODEL,
    MIN_SCENES,
    STUDIO_PROMPT,
    TARGET_MAX_SCENES,
)
from .llm import (
    _LAST_FINISH,
    _ask_llm_stable,
    _bounded_content,
    _extract_topics,
    _parse_plan_json,
    _parse_scene_json,
    _was_truncated,
    choose_planner_budget,
)
from .narration import (
    _dedupe_narration_templates,
    _deepen_narrations,
    _enforce_unique_narration_trigrams,
    _narrate_plan,
    _narration_is_pure_english,
    _narration_tail_repeat,
    _persist_protected_trigrams,
    _protected_terms,
    _repair_thin_narrations,
    _repair_unsafe_narrations,
    _trim_narration_word_count,
    _unsafe_repeat_scenes,
)
from .text import (
    _STOP,
    _WORD,
    _content_ngrams,
    _content_tokens,
    _has_non_latin_script,
    _has_source_citation,
    _jaccard,
    _merged_token_issues,
    _nar_tokens,
    _near_dupe_bullets,
    _strip_source_citations,
    _strip_source_metadata_blocks,
    _text_ngrams,
    _token_set,
    _tokens_of,
    _top_source_terms,
    clip_title,
)
from .topics import (
    _force_opening_on_topic,
    _opening_is_on_topic,
    _opening_template_hit,
    _plan_is_on_topic,
    _topic_coverage_problem,
)
from .voice import _MHE_VOICE, NarrationVoice, _assign_closers, _assign_openers

_MAX_PATCH_SCENES = 4
_SOURCE_CHUNK_LIMIT = 420


def _section_problems(plan: dict) -> list[str]:
    """Flag scenes whose section label is not a single schema value (e.g. the echoed enum)."""
    problems: list[str] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        s = str(sc.get("section", "")).strip().lower()
        if "|" in s:
            problems.append(
                f"scene {i} section label is the full enum: {str(sc.get('section',''))!r}"
            )
        elif s and not any(s.startswith(c) for c in _SECTIONS):
            problems.append(f"scene {i} section label not a schema section: {s!r}")
    return problems
def _harvest_takeaways(plan: dict) -> int:
    """Promote scene-level takeaways when the top-level list is missing.

    The 7B sometimes emits 5 solid takeaways inside the last Takeaway scene but
    omits the mandatory top-level ``takeaways`` array; the deck's final 'Key
    Takeaways' slide and the plan-level QA gates read only the top-level key.
    Deterministically promote them in place; return the count gathered.
    """
    if plan.get("takeaways"):
        return 0
    got: list[str] = []
    for sc in plan.get("scenes", []):
        for t in (sc.get("takeaways") or []):
            t = str(t).strip()
            if t and t not in got:
                got.append(t)
    if got:
        plan["takeaways"] = got[:8]
    return len(got)
def _sanitize_design_decisions(plan: dict) -> int:
    """Empty degenerate 'design_decision' fields in place; return count removed.

    The free 7B loves to copy ONE plausible decision sentence into every scene
    ("...not X because it must be consistent across modules", or Devanagari
    script). Rendering that as a 'WHY THIS' card is worse than no card, so
    degenerate entries are deterministically dropped.
    """
    dds = [str(s.get("design_decision", "")) for s in plan["scenes"]]
    removed = 0
    for i, dd in enumerate(dds):
        low, toks = dd.lower(), set(_tokens_of(dd))
        degenerate = (
            not dd or len(toks) < 4
            or any(g in low for g in _DD_GENERIC)
            or bool(_DEVANAGARI.search(dd))
            or any(toks and len(toks & set(_tokens_of(o))) / len(toks) > 0.6
                    for j, o in enumerate(dds) if j != i and o)
        )
        if degenerate and dd.strip():
            plan["scenes"][i]["design_decision"] = ""
            removed += 1
    return removed
def _sanitize_plan_source_leaks(plan: dict,
                                voice: NarrationVoice | None = None,
                                source_content: str = "",
                                default_refs: list[str] | None = None) -> int:
    voice = voice or _MHE_VOICE
    changed = 0
    opening_before = str(plan.get("opening", ""))
    _sanitize_opening(plan)
    if str(plan.get("opening", "")) != opening_before:
        changed += 1

    def _clean_visible(value: object) -> object:
        nonlocal changed
        if not isinstance(value, str) or not _has_source_citation(value):
            return value
        cleaned = _strip_source_citations(value)
        if cleaned != value:
            changed += 1
        return cleaned

    for sc in plan.get("scenes", []):
        for field in ("title", "analogy", "visual_diagram"):
            if field in sc:
                sc[field] = _clean_visible(sc.get(field, ""))
        for field in ("bullets", "steps", "flow"):
            values = sc.get(field)
            if isinstance(values, list):
                cleaned_values = [_clean_visible(value) for value in values]
                if any(original is not value
                       for original, value in zip(values, cleaned_values, strict=True)):
                    sc[field] = [value for value in cleaned_values if value]
        source_chunk = sc.get("source_chunk")
        if isinstance(source_chunk, str):
            cleaned_chunk = _strip_source_metadata_blocks(source_chunk)
            if cleaned_chunk != source_chunk:
                sc["source_chunk"] = cleaned_chunk
                changed += 1
        design_decision = sc.get("design_decision", "")
        if isinstance(design_decision, str) and _has_source_citation(design_decision):
            sc["design_decision"] = ""
            changed += 1
        narration = sc.get("narration", "")
        if isinstance(narration, str) and _has_source_citation(narration):
            cleaned_narration = ""
            lower_narration = narration.casefold()
            for lead in voice.dd_leads:
                marker = lead.strip().casefold()
                position = lower_narration.find(marker)
                if position < 0:
                    continue
                prefix = narration[:position].rstrip()
                if len(_nar_tokens(prefix)) >= 6:
                    cleaned_narration = prefix.rstrip(" .:") + "."
                    break
            if not cleaned_narration:
                cleaned_narration = _strip_source_citations(narration)
            if cleaned_narration != narration:
                sc["narration"] = cleaned_narration
                changed += 1
    _paginate_plan_slides(plan)
    _ensure_technical_visuals(plan)
    _annotate_scene_metadata(plan, source_content, default_refs)
    _normalize_scene_titles(plan)
    return changed


_TITLE_CAP = 44


def _normalize_scene_titles(plan: dict) -> int:
    """Repair titles that were hard-cut mid-word, then trim on a word boundary.

    A title truncated at the character limit is not only ugly on the slide: the
    stored title is also what the voice speaks, so a title ending in "...before
    va" is heard as a broken word. The repair is deliberately narrow — it fires
    only for a title sitting at the cap while its ``topic`` (derived from the
    source, never model-invented) is still longer, which is exactly the shape a
    hard cut leaves behind. Other titles are left alone, so the module label and
    the model's wording survive.
    """
    changed = 0
    for scene in plan.get("scenes", []):
        title = str(scene.get("title", "")).strip()
        if not title:
            continue
        topic = str(scene.get("topic", "")).strip()
        at_cap = len(title) >= _TITLE_CAP
        if topic and at_cap and len(topic) > len(title):
            rebuilt = clip_title(re.sub(r"^\s*m\d+\s*[-.:]?\s*", "", topic,
                                        flags=re.IGNORECASE))
            if rebuilt and len(rebuilt) >= _TITLE_CAP - 4:
                if rebuilt != title:
                    scene["title"] = rebuilt
                    changed += 1
                continue
        clipped = clip_title(title)
        if clipped != title:
            scene["title"] = clipped
            changed += 1
    return changed
def _scene_bullet_pages(scene: dict) -> list[list[str]]:
    bullets = [str(value) for value in (scene.get("bullets") or [])]
    if not bullets:
        return []
    existing = scene.get("bullet_pages")
    if isinstance(existing, list) and existing:
        pages = [[str(value) for value in page]
                 for page in existing if isinstance(page, list) and page]
        if (pages and all(len(page) <= 4 for page in pages)
                and [value for page in pages for value in page] == bullets):
            return pages
    return [bullets[i:i + 4] for i in range(0, len(bullets), 4)]


def _paginate_plan_slides(plan: dict) -> int:
    changed = 0
    for scene in plan.get("scenes", []):
        pages = _scene_bullet_pages(scene)
        if not pages:
            if scene.get("bullet_pages") or scene.get("pagination"):
                scene.pop("bullet_pages", None)
                scene.pop("pagination", None)
                changed += 1
            continue
        pagination = {"field": "bullets", "pages": len(pages), "page_size": 4}
        if scene.get("bullet_pages") != pages or scene.get("pagination") != pagination:
            scene["bullet_pages"] = pages
            scene["pagination"] = pagination
            changed += 1
    return changed


def _ensure_technical_visuals(plan: dict) -> int:
    changed = 0
    for scene in plan.get("scenes", []):
        text = " ".join([
            str(scene.get("title", "")), str(scene.get("design_decision", "")),
            str(scene.get("source_chunk", "")),
            *[str(value) for value in (scene.get("bullets") or [])]]).casefold()
        if not str(scene.get("visual_diagram", "")).strip():
            if "precedence" in text or ("fail" in text and "review" in text
                                        and "structure" in text):
                scene["visual_diagram"] = (
                    "[Structure checks] ---> [Value checks] ---> "
                    "[FAIL > REVIEW > PASS]")
                changed += 1
            elif "exit 3" in text or "exit 4" in text or "structural error" in text:
                scene["visual_diagram"] = (
                    "[Exit 3: evaluation/input error] ---> "
                    "[Exit 4: config/baseline error]")
                changed += 1
            elif "guardrail" in text and "info" in text and "gate" in text:
                scene["visual_diagram"] = (
                    "[Gate: hard FAIL] ---> [Guardrail: soft REVIEW] ---> "
                    "[Info: recorded only]")
                changed += 1
            elif all(term in text for term in ("kind", "direction", "tolerance")):
                scene["visual_diagram"] = (
                    "[Metric] ---> [Kind + direction] ---> "
                    "[Tolerance + unit]")
                changed += 1
        code = str(scene.get("code_snippet", "")).strip()
        if code and not str(scene.get("code_context", "")).strip():
            if code == "uv sync":
                scene["code_context"] = (
                    "Prepare the locked environment before running the suite.")
            elif "llm_eval_gate" in code:
                scene["code_context"] = (
                    "Use this workflow for the deterministic offline gate.")
            else:
                scene["code_context"] = "Use this documented command in the lesson workflow."
            changed += 1
        changed += _populate_concrete_values(scene, text)
        changed += _populate_status_badges(scene, text)
    return changed


_NUM = r"[-+]?\d+(?:\.\d+)?\s*(?:ms|s|%|x)?"
_STATE = r"PASS|FAIL|REVIEW"
# The document states boundaries in both orders ("0.97 PASS / 0.96 FAIL" and
# "boundary PASS 0.97, FAIL 0.96"), so both shapes are matched and the row keeps
# the order the text used. No inequality is invented either way.
_BOUNDARY_PAIRS_RE = (
    re.compile(rf"(?P<a>{_NUM})\s*(?P<sa>{_STATE})\b[^0-9+\-]{{0,12}}"
               rf"(?P<b>{_NUM})\s*(?P<sb>{_STATE})\b", re.IGNORECASE),
    re.compile(rf"\b(?P<sa>{_STATE})\s*(?P<a>{_NUM})\b[^0-9]{{0,18}}?"
               rf"\b(?P<sb>{_STATE})\s*(?P<b>{_NUM})\b", re.IGNORECASE),
)
_EXIT_STATE_RE = re.compile(
    r"\b(?P<code>[0-4])\s*[:=]?\s*(?P<state>PASS|FAIL|REVIEW)\b",
    re.IGNORECASE)
_EXIT_ERROR_RE = re.compile(
    r"\b(?P<code>[0-4])\s*=\s*(?P<scope>[A-Za-z][A-Za-z/\s]{2,60}?)"
    r"(?=\s*[.,;()]|$|\d\s*=)", re.IGNORECASE)
_POINTER_KEYS_RE = re.compile(
    r"\{\s*schema_version\s*,\s*baseline_id\s*,\s*path\s*\}", re.IGNORECASE)


def _boundary_rows(text: str) -> list[str]:
    """PASS/FAIL boundary pairs that carry a real magnitude."""
    rows: list[str] = []
    seen: set[str] = set()
    for pattern in _BOUNDARY_PAIRS_RE:
        for match in pattern.finditer(text):
            first = f"{match.group('a').strip()} {match.group('sa').upper()}"
            second = f"{match.group('b').strip()} {match.group('sb').upper()}"
            if match.group("sa").upper() == match.group("sb").upper():
                continue
            # A threshold boundary carries a magnitude: a decimal point or a
            # unit. Without one the integers in this document are exit codes
            # ("exit code 0 PASS, 1 FAIL, 2 REVIEW"), never tolerances, and
            # rendering them as a NUMBERS table would be actively misleading.
            if not re.search(r"\.", first + second) and not re.search(
                    r"(?:ms|s|%|x)\b", first + second, re.IGNORECASE):
                continue
            row = f"{first} | {second}"
            if row not in seen:
                seen.add(row)
                rows.append(row)
    return rows


def _populate_status_badges(scene: dict, text: str) -> int:
    """Derive the colour-coded exit-code row the source actually states.

    Only emitted when the text carries the verdict triple ("0 PASS, 1 FAIL,
    2 REVIEW"), so a document that never discusses exit codes never gets a badge
    row. The two infrastructure codes are labelled by keyword, with a neutral
    ``ERROR`` fallback: guessing a label the source never used would be exactly
    the invented content the grounding rules exist to prevent.
    """
    if scene.get("status_badges"):
        return 0
    verdicts = {m.group("code"): m.group("state").upper()
                for m in _EXIT_STATE_RE.finditer(text)}
    if len({"PASS", "FAIL", "REVIEW"} & set(verdicts.values())) < 3:
        return 0
    badges: list[dict] = [{"code": code, "label": verdicts[code],
                           "state": verdicts[code]}
                          for code in sorted(verdicts, key=int)
                          if verdicts[code] in {"PASS", "FAIL", "REVIEW"}]
    for match in _EXIT_ERROR_RE.finditer(text):
        code = match.group("code")
        if code in verdicts:
            continue
        scope = match.group("scope").lower()
        if "eval" in scope:
            label = "EVAL ERR"
        elif "config" in scope or "baseline" in scope:
            label = "CONFIG ERR"
        else:
            label = "ERROR"
        badges.append({"code": code, "label": label, "state": "ERROR"})
    if len(badges) < 3:
        return 0
    badges.sort(key=lambda item: int(item["code"]))
    scene["status_badges"] = badges[:6]
    return 1


def _populate_concrete_values(scene: dict, text: str) -> int:
    """Surface the lesson's real numbers and real payload instead of prose.

    Two enrichment blocks are derived deterministically from the scene's own
    source chunk, so they can never invent a fact the way a model-written
    "worked example" would:

    * ``value_table`` - PASS/FAIL boundary pairs the document already states
      ("0.97 PASS / 0.96 FAIL", "120ms PASS / 121ms REVIEW"). Abstract
      tolerance prose becomes a concrete table the viewer can read at a glance.
    * ``json_snippet`` - the literal file shape the document already gives,
      such as the ``{schema_version, baseline_id, path}`` pointer payload.

    Both are additive: a scene that already carries the field is untouched.
    """
    changed = 0
    if not scene.get("value_table"):
        rows = _boundary_rows(text)
        # Two rows minimum: a single boundary pair is a curiosity, not a table.
        if len(rows) >= 2:
            scene["value_table"] = rows[:4]
            changed += 1
    if not str(scene.get("json_snippet", "")).strip():
        payload = _POINTER_KEYS_RE.search(text)
        if payload:
            scene["json_snippet"] = "\n".join([
                "{", '  "schema_version": 1,', '  "baseline_id": "v1.2.0",',
                '  "path": "eval/baselines/v1.2.0.json"', "}",
            ])
            changed += 1
    return changed


def _extract_source_refs(content: str) -> list[str]:
    return list(dict.fromkeys(re.findall(
        r"(?<![\w/])([\w.-]+(?:/[\w.-]+)*\.(?:md|txt|yaml|yml|json|py)"
        r"(?::\d+(?:-\d+)?)?)",
        str(content),
        flags=re.IGNORECASE,
    )))


def _annotate_scene_metadata(plan: dict, content: str = "",
                             default_refs: list[str] | None = None) -> int:
    refs = _extract_source_refs(content)
    fallback = [str(value) for value in (default_refs or []) if str(value).strip()]
    if not fallback:
        fallback = ["source document"]
    changed = 0
    for scene in plan.get("scenes", []):
        title = str(scene.get("title", "")).strip()
        if not str(scene.get("topic", "")).strip() and title:
            scene["topic"] = title
            changed += 1
        existing = scene.get("source_refs")
        if isinstance(existing, list) and any(str(value).strip() for value in existing):
            continue
        anchor = set(_nar_tokens(" ".join([
            title, *[str(value) for value in (scene.get("bullets") or [])]
        ])))
        selected = [ref for ref in refs if any(
            len(token) > 3 and token in ref.casefold()
            for token in anchor)]
        scene["source_refs"] = selected[:3] or fallback[:3]
        changed += 1
    return changed


def _source_design_decisions(plan: dict, content: str) -> int:
    """Deterministically fill empty design_decision fields from the DOCS.

    After sanitizing, any scene still missing a design_decision gets one
    sourced straight from the mined contrast sentences (attributed to the
    nearest heading). This guarantees a grounded, non-repeating 'WHY THIS'
    card without another LLM round-trip. Returns how many were filled.
    """
    contrasts = _contrast_sentences(content, limit=12)
    if not contrasts:
        return 0
    items: list[tuple[str, str]] = []
    for c in contrasts:
        m = re.match(r"\((.*?)\):\s*(.*)", c, re.DOTALL)
        items.append(((m.group(1).strip() if m else ""),
                      (m.group(2).strip() if m else c)))
    filled = 0
    used: list[str] = []
    for sc in plan["scenes"]:
        if str(sc.get("design_decision", "")).strip():
            continue
        title_toks = set(_token_set(str(sc["title"])))
        title_toks |= set(_token_set(" ".join(sc.get("bullets") or [])))
        if not title_toks:
            continue
        best, best_score = "", 0
        for head, sent in items:
            ht = set(_token_set(head))
            if not ht:
                continue
            # An exact heading match (subset either way) always wins.
            score = 5 if (ht <= title_toks or title_toks <= ht) else 0
            score = max(score, len(title_toks & ht))
            if score > best_score:
                best, best_score = sent, score
        if best_score >= 2:
            bt = set(_tokens_of(best))
            if bt and any(_tokens_of(u) and len(bt & set(_tokens_of(u)))
                          / len(bt) > 0.6 for u in used):
                continue
            sc["design_decision"] = best[:180]
            used.append(best)
            filled += 1
    return filled
def _infer_section(sc: dict, idx: int) -> str:
    """Deterministic last-resort section label (strong keyword hints, else by
    scene position). Free-7B plans occasionally echo table-row ids into
    'section'; force a valid schema enum so the review gate cannot fail on it."""
    text = (" ".join([str(sc.get("title", "")), str(sc.get("section", "")),
                      *[str(b) for b in (sc.get("bullets") or [])],
                      str(sc.get("narration", ""))])).lower()
    for cand, hints in _ENUM_HINTS:
        if any(h in text for h in hints):
            return cand.title()
    if idx == 1:
        return "What Is This?"
    spread = ("Why Do We Need It?", "How Does It Work?", "Example", "Takeaway")
    return spread[(idx - 2) % len(spread)]
def _normalize_sections(plan: dict) -> None:
    """Force every scene section to a single canonical schema value, in place."""
    for idx, sc in enumerate(plan.get("scenes", []), 1):
        sc["design_decision"] = str(sc.get("design_decision") or "").strip()
        raw = str(sc.get("section", "")).strip().lower()
        if "|" in raw:
            raw = raw.split("|")[0]
        raw = raw.strip(" .,:;-")
        section = ""
        for cand in _SECTIONS:
            if raw.startswith(cand):
                section = cand
                break
        sc["section"] = (section.title() if section
                         else _infer_section(sc, idx))


def _sanitize_opening(plan: dict) -> None:
    """Strip LLM field-name leakage from the plan-level opening text.

    The 7B occasionally appends a stray field name after a period
    (e.g. '...shuruwat karte hai. opening') or bare at the end
    ('...karte hai opening'), leaking the JSON key into the spoken
    string. Deterministic cleanup keeps the spoken line clean without
    a fresh LLM call.
    """
    opening = plan.get("opening", "")
    if not opening:
        return
    text = str(opening)
    cleaned = re.sub(
        r"\s*\.\s*(?:opening|closing|prompt_end|text|line|value|content|0)\s*$",
        "", text, flags=re.IGNORECASE).strip()
    cleaned = re.sub(
        r"\s+(?:opening|closing|prompt_end)\s*$",
        "", cleaned, flags=re.IGNORECASE).strip()
    if cleaned != text:
        plan["opening"] = cleaned
def _grounding_issues(plan: dict, source_bigrams: set[tuple[str, str]],
                      source_tokens: set[str]) -> list[str]:
    """Flag slide/takeaway text with no source anchor.

    Deterministic drift heuristic. Text counts as anchored when it reuses a
    source word-pair, or >=2 shared content words with one specific (len>6),
    or >=3 shared content words. Invented or generic filler reuses neither;
    very short text (<2 content words) is never flagged.
    """
    if not source_bigrams or not source_tokens:
        return []

    def _anchored(text: str) -> bool:
        ngrams = _text_ngrams(text)
        tokens = {w.lower() for w in _WORD.findall(text)
                  if len(w) > 2 and w.lower() not in _STOP}
        if len(tokens) < 2:
            # Non-Latin text has no Latin content tokens at all: treat it as
            # ungrounded, otherwise pure Devanagari invention would be
            # "anchored by default" (v011_006 shipped 4 such slide items).
            return not _has_non_latin_script(text)
        if ngrams & source_bigrams:
            return True
        shared = tokens & source_tokens
        return len(shared) >= 3 or (len(shared) >= 2 and any(len(t) > 6 for t in shared))

    issues: list[str] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        texts = [str(sc.get("title", ""))]
        texts += [str(b) for b in (sc.get("bullets") or [])]
        texts += [str(t) for t in (sc.get("takeaways") or [])]
        texts.append(str(sc.get("visual_diagram", "")))
        texts.append(str(sc.get("code_snippet", "")))
        for text in texts:
            if text and not _anchored(text):
                issues.append(f"scene {i} ungrounded: {text[:64]!r}")
    for j, t in enumerate(plan.get("takeaways") or [], 1):
        if t and not _anchored(str(t)):
            issues.append(f"takeaway {j} ungrounded: {str(t)[:64]!r}")
    return issues
def _drop_repeated_bullet_clauses(plan: dict) -> int:
    """Drop a later bullet that repeats a clause already spoken by an earlier
    bullet in the SAME scene.

    Exact-duplicate removal (``_dedupe_plan_bullets``) and near-duplicate
    detection against takeaways (``_near_dupe_bullets``) both miss the case the
    free model actually produces: two bullets that are individually distinct but
    share the same trailing clause, e.g.

        "Info: recorded for provenance, CI gate ney pass nahi karne ka."
        "Info: never breaks a build, kyunki CI gate ney pass nahi karne ka."

    Whole-bullet similarity stays low, but the shared run is long enough to become
    a banned repeated trigram as soon as the deterministic narration rebuild
    speaks both bullets. Removing the later bullet at the source fixes the slide
    and the narration in one deterministic pass. The rule is deliberately
    conservative: a run of four or more consecutive canonical tokens must be
    shared, so "gate: hard fail" and "gate: soft review" are untouched.
    """
    run = 4
    dropped = 0
    for sc in plan.get("scenes", []):
        original = [b for b in (sc.get("bullets") or [])]
        kept: list[str] = []
        kept_windows: set[tuple[str, ...]] = set()
        scene_dropped = 0
        for bullet in original:
            tokens = tuple(_nar_tokens(str(bullet)))
            if not tokens:
                scene_dropped += 1
                continue
            windows = {tokens[i:i + run] for i in range(len(tokens) - run + 1)}
            if windows & kept_windows:
                scene_dropped += 1
                continue
            kept.append(bullet)
            kept_windows |= windows
        if scene_dropped:
            sc["bullets"] = kept
            dropped += scene_dropped
    return dropped


def _drop_ungrounded_slide_text(plan: dict,
                                source_bigrams: set[tuple[str, str]],
                                source_tokens: set[str]) -> int:
    """Deterministic last resort for grounding: drop unanchored slide text.

    The free 7B keeps inventing short filler bullets that no source bigram or
    content token anchors. LLM targeted patches sometimes can't kill them (the
    model rewrites to a new invented phrase), and review-before-build then burns
    a fresh whole-plan sample for one surviving item. Like _dedupe_plan_bullets
    and _sanitize_design_decisions, this prunes that degenerate output in place
    so the hard grounding gate converges without spending another LLM call.

    Only *drop*, except for titles, which cannot be deleted. An ungrounded title
    is therefore re-derived from the scene's own grounded material (its
    ``topic`` first, then its first grounded bullet), so the hard grounding gate
    converges without spending another LLM call.
    """
    if not source_bigrams or not source_tokens:
        return 0

    def _anchored(text: str) -> bool:
        ngrams = _text_ngrams(text)
        tokens = {w.lower() for w in _WORD.findall(text)
                  if len(w) > 2 and w.lower() not in _STOP}
        if len(tokens) < 2:
            # Mirror _grounding_issues: pure non-Latin text is ungrounded, not
            # "anchored by default" (v011_006 leaked 4 Devanagari slide items).
            return not _has_non_latin_script(text)
        if ngrams & source_bigrams:
            return True
        shared = tokens & source_tokens
        return len(shared) >= 3 or (len(shared) >= 2 and any(len(t) > 6 for t in shared))

    def _clean_title(text: str) -> str:
        value = re.sub(r"^\s*m\d+\s*[-.:]?\s*", "", str(text), flags=re.IGNORECASE)
        return clip_title(value)

    dropped = 0
    for sc in plan.get("scenes", []):
        for field in ("bullets", "takeaways"):
            items = [str(x) for x in (sc.get(field) or [])]
            kept = [x for x in items if _anchored(x)]
            dropped += len(items) - len(kept)
            sc[field] = kept
        for field in ("visual_diagram", "code_snippet"):
            val = str(sc.get(field, "")).strip()
            if val and not _anchored(val):
                sc[field] = ""
                dropped += 1
    plan_tk = [str(t) for t in (plan.get("takeaways") or [])]
    kept_tk = [t for t in plan_tk if _anchored(t)]
    dropped += len(plan_tk) - len(kept_tk)
    plan["takeaways"] = kept_tk
    used_titles = {str(sc.get("title", "")).strip().casefold()
                   for sc in plan.get("scenes", [])}
    for sc in plan.get("scenes", []):
        title = str(sc.get("title", "")).strip()
        if not title or _anchored(title):
            continue
        candidates = [sc.get("topic"),
                      *(sc.get("bullets") or []), sc.get("design_decision", "")]
        for candidate in candidates:
            value = _clean_title(str(candidate or ""))
            if (len(value) < 8 or not _anchored(value)
                    or value.casefold() in used_titles):
                continue
            sc["title"] = value
            used_titles.discard(title.casefold())
            used_titles.add(value.casefold())
            dropped += 1
            break
    _paginate_plan_slides(plan)
    return dropped
def _repeated_bullets(plan: dict) -> list[str]:
    """Bullet lines reused on >=2 scenes (copy-paste degeneration)."""
    counts: dict[str, int] = {}
    for sc in plan.get("scenes", []):
        seen: set[str] = set()
        for b in (sc.get("bullets") or []):
            key = str(b).strip().lower()
            if key and key not in seen:
                counts[key] = counts.get(key, 0) + 1
                seen.add(key)
    return sorted(k for k, c in counts.items() if c >= 2)
def _dedupe_plan_bullets(plan: dict) -> None:
    """Deterministic cleanup: drop exact bullet lines reused within/across scenes.

    Copy-paste degeneration is a model-mood failure; this clears it without
    spending another LLM call. Grounding etc. are unaffected (dedupe drops the
    later occurrences in place).
    """
    global_seen: set[str] = set()
    for sc in plan.get("scenes", []):
        keep: list[str] = []
        scene_seen: set[str] = set()
        for b in (sc.get("bullets") or []):
            key = str(b).strip().lower()
            if not key or key in global_seen or key in scene_seen:
                continue
            global_seen.add(key)
            scene_seen.add(key)
            keep.append(str(b).strip())
        sc["bullets"] = keep
def _prune_bullet_takeaway_echo(plan: dict) -> int:
    """Drop scene bullets that are near-copies of a takeaway card.

    Takeaways are the lesson's summary, so a bullet restating one word-for-word
    is redundant with the takeaway panel (and trips the near-dupe gate). The
    takeaway keeps the information, so removing the bullet line is lossless.
    """
    takeaways = [str(t).strip().lower() for t in (plan.get("takeaways") or [])]
    pruned = 0
    for sc in plan.get("scenes", []):
        keep: list[str] = []
        for b in (sc.get("bullets") or []):
            tb = _token_set(str(b))
            if not b or any(_jaccard(tb, _token_set(t)) >= 0.78 for t in takeaways):
                pruned += 1
                continue
            keep.append(str(b).strip())
        if keep != sc.get("bullets"):
            sc["bullets"] = keep
    return pruned
def _placeholder_titles(plan: dict) -> list[tuple[int, str]]:
    """Scene titles that just restate the section label (no information).

    E.g. section 'Example' + title 'm4 example' — the title carries zero extra
    information and the schema explicitly rejects it.
    """

    def norm(text: str) -> str:
        s = re.sub(r"[^a-z0-9 ]", "", str(text).lower())
        s = re.sub(r"^m?\d+\s*", "", s)  # drop a leading 'm4 ' module prefix
        s = re.sub(r"\b(the|a|an)\b", " ", s)
        return " ".join(s.split())

    out: list[tuple[int, str]] = []
    for idx, sc in enumerate(plan.get("scenes", []), 1):
        label = norm(sc.get("section", ""))
        title = sc.get("title", "")
        t = norm(title)
        if label and t and (t == label or t.rstrip("s") == label.rstrip("s")):
            out.append((idx, str(title)))
    return out
def _fix_placeholder_titles(plan: dict) -> int:
    """Retitle scenes whose title just restates the section label.

    The 7B frequently titles its closing scene "takeaways" (== section
    'Takeaway'), which the QA gate flags. Deterministically reuse the scene's
    own first takeaway/bullet as the title so it carries real information.
    """
    fixed = 0
    for idx, _ in _placeholder_titles(plan):
        sc = plan["scenes"][idx - 1]
        src = sc.get("takeaways") or sc.get("bullets") or []
        if not src:
            continue
        new = str(src[0]).strip()[:72]
        if new and new.lower() != str(sc.get("title", "")).lower():
            sc["title"] = new
            fixed += 1
    return fixed
def _scene_count_problem(plan: dict) -> str:
    """Schema slot guard, with the 1:1 concept contract honored when the plan
    was concept-driven (plan['scene_target'] set).

    A >TARGET_MAX-scene plan is recoverable by deterministic overflow trim only
    inside plan_lesson; a raw candidate that reaches guard/review is illegal
    unless it exactly matches the extracted source concept list.
    """

    n = len(plan.get("scenes", []))
    scene_target = plan.get("scene_target")
    if isinstance(scene_target, int) and MIN_SCENES <= scene_target <= AUTO_TRIM_MAX_SCENES:
        if n == scene_target or (scene_target >= 10 and n >= scene_target - 1):
            return ""
        return (f"scene count {n} != source concept frame {scene_target} "
                f"(1:1 concept contract - every source concept must be covered)")
    if not MIN_SCENES <= n <= TARGET_MAX_SCENES:
        return f"scene count {n} (schema wants {MIN_SCENES}-{TARGET_MAX_SCENES})"
    return ""
def _concept_headers(content: str) -> list[str]:
    """Docs-as-Code concept skeleton: every real source `##`/`###` heading, in
    order, deduplicated and junk-filtered (rule 1:1 concept partitioning).

    This is the deterministic upstream AST-lite pass (stdlib regex; no new
    dependency where the reviewer suggested mistune/marko). Returns the raw
    headers bounded to the schema's scene range [MIN_SCENES, AUTO_TRIM_MAX_SCENES]
    so plan_lesson can demand exactly one scene per concept.
    """
    junk = ("out of scope", "none yet", "scaffold only", "n/a", "to be done",
            "todo ", "todo:", "table of contents", "contents", "introduction",
            "the one sentence to memorize", "the 5 big ideas", "checkpoints",
            "now read the lld", "concept by concept")
    heads: list[str] = []
    lines = content.splitlines()
    for i, ln in enumerate(lines):
        t = ln.strip()
        if not (t.startswith("## ") or t.startswith("### ")):
            continue
        # Skip ## section dividers that are immediately followed by ###
        # sub-headers (e.g. "Concept by concept" is a container, not a
        # concept).
        if t.startswith("## "):
            follow = False
            for j in range(i + 1, min(i + 3, len(lines))):
                if lines[j].strip().startswith("### "):
                    follow = True
                    break
            if follow:
                continue
        head = " ".join(t.strip("# ").split())
        low = head.lower()
        if len(head) <= 3 or any(x in low for x in junk):
            continue
        if head not in heads:
            heads.append(head)
        if len(heads) >= AUTO_TRIM_MAX_SCENES:
            break
    return heads
def _plan_scene_target(concepts: list[str] | None) -> int:
    """Resolve the exact scene count a plan must hit. Concept-driven builds get
    one scene per source concept (1:1, 5-12 concepts) or per contiguous
    concept GROUP when the doc has more than 12 concepts - the frame is sized
    to the free 7B's reliable scene budget while still covering EVERY source
    concept (no silent truncation). Plain builds keep the classic 6-scene
    drill."""
    groups = _concept_groups(concepts or [])
    return len(groups) if groups else 6
def _concept_groups(concepts: list[str]) -> list[str]:
    """Deterministic, source-ordered grouping of concept headers into a scene
    frame the 7B can actually emit (5-12 scenes).

    5-12 concepts -> one scene per concept (1:1). 13+ concepts -> 12 contiguous
    groups (labels keep EVERY concept name, so nothing is dropped/invented),
    mirroring the deterministic `_trim_scene_overflow` philosophy: a loud,
    audited merge ordered by the source, never a silent loss.
    """
    if not concepts:
        return []
    n = len(concepts)
    if n <= AUTO_TRIM_MAX_SCENES:
        return [c for c in concepts]
    k = AUTO_TRIM_MAX_SCENES
    base, rem = divmod(n, k)
    sizes = [base + (1 if i < rem else 0) for i in range(k)]
    groups: list[str] = []
    idx = 0
    for size in sizes:
        chunk = concepts[idx:idx + size]
        idx += size
        groups.append(" & ".join(chunk) if len(chunk) > 1 else chunk[0])
    return groups
def _overflow_audit(scenes_kept: list[dict], scenes_dropped: list[dict],
                    max_scenes: int = TARGET_MAX_SCENES) -> dict:
    """Audit record for a deterministic overflow trim (never silent loss)."""
    return {
        "scene_count_original": len(scenes_kept) + len(scenes_dropped),
        "scene_count_final": len(scenes_kept),
        "scene_overflow_policy": {
            "action": "trim_prefix",
            "max_scenes": max_scenes,
            "auto_trim_max_scenes": AUTO_TRIM_MAX_SCENES,
            "reason": "model_overproduced_scenes",
        },
        "dropped_scenes": [
            {
                "original_index": i,
                "title": str(s.get("title", ""))[:120],
                "section": str(s.get("section", "")),
            }
            for i, s in enumerate(scenes_dropped, start=len(scenes_kept) + 1)
        ],
    }
def _trim_scene_overflow(plan: dict, *,
                         max_scenes: int = TARGET_MAX_SCENES,
                         auto_trim_max_scenes: int = AUTO_TRIM_MAX_SCENES) -> int:
    """Deterministic overflow repair: a 13+ scene plan keeps its first 12
    source-ordered scenes, drops the invented tail, and re-runs all gates.

    This is a LOUD, audited trim (never a silent clamp): original count,
    dropped titles/sections and the policy are recorded in
    plan["_overflow_report"], and the caller must re-run every plan gate on
    the trimmed result. Returns the number of scenes dropped (0 = no trim).
    Counts > auto_trim_max_scenes or < MIN_SCENES are rejected, not trimmed.
    """
    scenes = plan.get("scenes") or []
    n = len(scenes)
    if not (max_scenes < n <= auto_trim_max_scenes):
        return 0
    kept = scenes[:max_scenes]
    dropped = scenes[max_scenes:]
    plan["scenes"] = kept
    plan["_overflow_report"] = _overflow_audit(kept, dropped, max_scenes)
    _filter_takeaways_to_retained(plan)
    print(f"\n  PLAN OVERFLOW REPAIRED: generated {n} scenes; retained "
          f"source-order scenes 1-{max_scenes}; dropped scenes "
          f"{max_scenes + 1}-{n}; rerunning full gates.", flush=True)
    return len(dropped)
def _filter_takeaways_to_retained(plan: dict) -> None:
    """Drop plan-level takeaways that only make sense against a dropped scene.

    Reuses the retained scenes' own token sets (titles/narration/bullets/
    design_decision) as the anchor: a takeaway that shares no content token
    with any surviving scene came from the dropped tail. Only takeaways that
    share at least one content token with a retained scene survive (capped at
    8), so the final Key Takeaways slide only reflects surviving content.
    """
    scenes = plan.get("scenes") or []
    if not scenes:
        return
    union: set[str] = set()
    for sc in scenes:
        union |= _token_set(" ".join([
            str(sc.get("title", "")),
            str(sc.get("narration", "")),
            " ".join(str(b) for b in (sc.get("bullets") or [])),
            " ".join(str(s) for s in (sc.get("steps") or [])),
            str(sc.get("design_decision", "")),
        ]))
    takes = [str(t) for t in plan.get("takeaways") or [] if str(t).strip()]
    if not takes or not union:
        return
    kept = [t for t in takes if _token_set(t) & union]
    if kept:
        plan["takeaways"] = kept[:8]
def _trim_dropped_topic(plan: dict, outline: list[str]) -> list[str]:
    """Coverage guard for a trimmmed plan: which source outline elements are
    missing from the retained scenes? Run only after an overflow trim."""
    report = plan.get("_overflow_report")
    if not report:
        return []
    if not outline:
        return []
    union: set[str] = set()
    for sc in plan.get("scenes") or []:
        union |= _token_set(" ".join([
            str(sc.get("title", "")),
            str(sc.get("narration", "")),
            " ".join(str(b) for b in (sc.get("bullets") or [])),
        ]))
    missing: list[str] = []
    for item in outline:
        toks = _token_set(item)
        if toks and not (toks & union):
            missing.append(item)
    return missing
def _contrast_sentences(content: str, limit: int = 6) -> list[str]:
    """Sentences encoding a real design choice ('X not Y because Z').

    Dense LLD docs bury 'why this design' in prose near contrast words rather
    than headings. Pair each hit with the nearest heading so the LLM can
    attribute the decision to a module. Bold-lead decision bullets are kept
    (the docs write 'why' entries as ``**No build by default**: ...``), but
    headers, table rows, code snippets and line-numbered fragments are dropped.
    """
    content = _strip_source_metadata_blocks(content)
    head = ""
    out: list[str] = []
    for ln in content.splitlines():
        t = ln.strip()
        if t.startswith(("## ", "### ")) and len(t.strip("# ")) > 3:
            head = t.strip("# ").strip()
            continue
        if (not t or t.startswith(("#", "|", ">", "="))
                or re.match(r"^\d+[.):\s]", t)
                or len(t) < 40 or len(t) > 320):
            continue
        s = t.lstrip("-+* ")
        if s.startswith("**"):
            s = s[2:]
        s = s.lstrip("-+* ")
        if not s.endswith((".", "?", "!")) or not _CONTRAST_RE.search(s):
            continue
        s = re.sub(r"[`*]", "", s)
        s = re.sub(r"\s+", " ", s).rstrip(".") + "."
        out.append(f"({head or 'top'}): {s}")
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen[:limit]
def _repeated_terms(content: str, min_count: int = 3) -> list[str]:
    """Bold/backtick'd terms the doc itself defines/emphasizes (repeated)."""
    counts: dict[str, int] = {}
    for back, bold in re.findall(r"`([^`]+)`|\*\*([^*]+)\*\*", content):
        t = (back or bold).strip().strip("`*").strip()
        key = t.lower()
        if 3 <= len(key) <= 40:
            counts[key] = counts.get(key, 0) + 1
    order = sorted((c, k) for k, c in counts.items() if c >= min_count)
    return [k for _, k in order[:6]]
def _source_outline(content: str) -> list[str]:
    """Extract the docs' own skeleton: headings + module/phase table rows.

    Table rows first keep only entries with an id-like first cell (skips prose
    tables). If fewer than 2 id rows are found, fall back to any plausible
    row (first cell is a short name, second cell is non-empty) up to 4 rows.
    """
    idish = re.compile(r"`?[A-Za-z]?\d{1,3}\w*`?$|^Phase[- ]?\d+$")
    rows: list[str] = []
    heads: list[str] = []
    for ln in content.splitlines():
        t = ln.strip()
        if (t.startswith("## ") or t.startswith("### ")) and len(t.strip("# ")) > 3:
            head = t.strip("# ").strip()
            low = head.lower()
            if any(x in low for x in ("out of scope", "none yet", "scaffold only",
                                      "n/a", "to be done", "todo ")):
                continue
            heads.append(head)
        elif t.startswith("|") and t.count("|") >= 2:
            cells = [c.strip() for c in t.strip("|").split("|")]
            first = cells[0].replace("`", "")
            if len(cells) >= 2 and cells[1] and idish.fullmatch(first):
                rows.append(f"{first}: {cells[1]}")
    if len(rows) < 2 and len(heads) < 4:
        skip_first = {
            "module", "function", "name", "sr no", "sr. no", "phase",
            "description", "id", "component", "area",
        }
        for ln in content.splitlines():
            t = ln.strip()
            if not t.startswith("|") or t.count("|") < 2:
                continue
            cells = [c.strip() for c in t.strip("|").split("|")]
            first = cells[0].strip("`")
            second = cells[1].strip() if len(cells) >= 2 else ""
            if (not first or not second or len(first) > 32
                    or first.lower() in skip_first
                    or "--" in first or "—" in first):
                continue
            rows.append(f"{first}: {second}")
            if len(rows) >= 4:
                break
    out = rows + heads
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen[:8]
def _markdown_sections(content: str) -> list[tuple[str, str]]:
    """Split the docs into (heading, prose-body) pairs, tables/fences removed.

    Used to attach a real source excerpt to every scene (Phase-3 narration
    hydration): each scene keeps a small `source_chunk` the deterministic
    thin-repair can pull raw source sentences from when a scene's own bullets
    and design decision cannot reach the narration floor.
    """
    content = _strip_source_metadata_blocks(content)
    heads = [m for m in re.finditer(r"^#{1,3} .+$", content, re.M)]
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(content)
        body = content[m.end():end]
        body = re.sub(r"```.*?```", " ", body, flags=re.S)
        body = re.sub(r"\|[^\n]*\|", " ", body)
        sections.append((m.group(0), body))
    if not sections:
        return [("", content[:1200])]
    return sections
def _first_source_sentences(text: str, limit: int = _SOURCE_CHUNK_LIMIT) -> str:
    """The leading complete prose sentences of a section, capped in length."""
    text = _strip_source_metadata_blocks(text)
    text = re.sub(r"[ \t]+", " ", text)
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    total = 0
    for s in sentences:
        s = s.strip()
        if len(s) < 12:
            continue
        if total + len(s) > limit:
            break
        kept.append(s)
        total += len(s)
    return " ".join(kept).strip()
def _annotate_source_chunks(plan: dict, content: str) -> int:
    """Attach a short real source excerpt to each scene (in place)."""
    sections = _markdown_sections(content)
    annotated = 0
    for sc in plan.get("scenes", []):
        anchor = set(_nar_tokens(" ".join([
            str(sc.get("title", "")),
            *[str(b) for b in (sc.get("bullets") or [])],
            str(sc.get("design_decision", ""))])))
        best = ""
        best_score = -1
        for _head, body in sections:
            score = len(anchor & set(_nar_tokens(body)))
            if score > best_score:
                best, best_score = body, score
        chunk = _first_source_sentences(best) if best else _first_source_sentences(content)
        if chunk:
            sc["source_chunk"] = chunk
            annotated += 1
    return annotated
def _scene_problem_map(plan: dict, topics: list[str],
                       source_bigrams: set[tuple[str, str]],
                       source_tokens: set[str],
                       top_terms: list[str] | None = None,
                       voice: NarrationVoice | None = None,
                       protected: frozenset[str] | None = None) -> dict[int, list[str]]:
    """Map 1-based scene index -> concrete fix instructions for that scene."""
    fixes: dict[int, list[str]] = {}
    n = len(plan.get("scenes", []))

    for line in _grounding_issues(plan, source_bigrams, source_tokens):
        m = re.match(r"scene (\d+) ungrounded: (.+)$", line)
        if m:
            fixes.setdefault(int(m.group(1)), []).append(
                "grounding: the slide text "
                + m.group(2)
                + " does not reuse any real source wording. Rewrite EXACTLY that "
                "text (and only that text) so it repeats real names and facts "
                "verbatim from the source docs (modules, phases, engine options, "
                "registers); nothing invented")

    for dup in _repeated_bullets(plan):
        for i, sc in enumerate(plan.get("scenes", []), 1):
            if any(str(b).strip().lower() == dup for b in (sc.get("bullets") or [])):
                fixes.setdefault(i, []).append(
                    f"bullets: remove the copy-pasted bullet {dup!r} and replace "
                    f"it with a DIFFERENT fact specific to this exact scene")

    for near in _near_dupe_bullets(plan):
        for i, sc in enumerate(plan.get("scenes", []), 1):
            if any(str(b).strip().lower() == near for b in (sc.get("bullets") or [])):
                fixes.setdefault(i, []).append(
                    f"bullets: {near!r} is a near-duplicate of another scene's "
                    f"bullet or a takeaway (same facts). Rephrase it as a DIFFERENT, "
                    f"scene-specific fact")

    for idx, title in _placeholder_titles(plan):
        fixes.setdefault(idx, []).append(
            f"title: {title!r} just restates its section label, carrying no "
            f"information (schema rejects placeholder titles). Give a descriptive "
            f"title that names the ACTUAL module/topic this scene teaches")

    for issue in _merged_token_issues(plan):
        m = re.match(r"scene (\d+) (\w+): unbroken token", issue)
        if m:
            fixes.setdefault(int(m.group(1)), []).append(
                "text: a slide field has words fused together with no spaces "
                "(e.g. 'comparedifferentiatescandidatevsbaseline'). Rewrite that "
                "field with normal word spacing so it renders readably; keep "
                "real module/file names exact")

    for phrase in _narration_tail_repeat(plan, quiet=True, protected=protected):
        for i, sc in enumerate(plan.get("scenes", []), 1):
            ntok = _nar_tokens(str(sc.get("narration", "")))
            if any(" ".join(ntok[j:j + 3]) == phrase
                   for j in range(max(0, len(ntok) - 2))):
                fixes.setdefault(i, []).append(
                    f"narration: no two scenes may share the running phrase "
                    f"{phrase!r} - rewrite THIS narration so it actually explains "
                    f"the module instead of reusing filler")

    if _narration_is_pure_english(plan, voice=voice):
        for i in range(1, n + 1):
            fixes.setdefault(i, []).append(
                "narration: mix Marathi/Hindi words into English narration (e.g. "
                "'aaj hum seekhenge', 'kya hai', 'samajh sakte ho'); never pure English")

    if _opening_template_hit(plan):
        fixes.setdefault(1, []).append(
            "opening: your opening line copied the prompt's example sentence "
            "verbatim. Write a fresh opening ABOUT THIS LESSON'S TOPICS that "
            "names one of them; never reuse the example phrase from the prompt")

    if topics and not _opening_is_on_topic(plan, topics):
        fixes.setdefault(1, []).append(
            f"opening: the opening line MUST name one of these topics: "
            f"{', '.join(topics)}")

    if topics and not _plan_is_on_topic(plan, topics):
        for i in range(1, n + 1):
            fixes.setdefault(i, []).append(
                f"topic: the title and narration must relate to these exact "
                f"topics: {', '.join(topics)}; no unrelated side-topics")

    if top_terms and _topic_coverage_problem(plan, top_terms):
        for i in range(1, n + 1):
            fixes.setdefault(i, []).append(
                "coverage: this lesson barely touches the docs' core subject "
                "matter. Add real source detail to the title, bullets and "
                "narration using the dominant technical vocabulary of the "
                "DOCUMENTS (gates vs guardrails vs info tiers, tolerance "
                "policies, retriever/correctness gates, p95, precision/recall) "
                "instead of a generic module walkthrough")

    if _section_problems(plan):
        for i in range(1, n + 1):
            fixes.setdefault(i, []).append(
                "section: keep a clean, distinct section label (no duplicates)")

    return fixes
def _patch_opening(plan: dict, msgs: list[str], content: str) -> str:
    """Regenerate ONLY the plan-level spoken opening; scenes stay frozen."""
    prompt = (
        "The lesson opening below must be rewritten to fix these problems:\n- "
        + "\n- ".join(msgs)
        + "\n\nRules: write exactly ONE spoken opening (1-2 sentences) in "
        "Marathi/Hindi mixed with English technical words. It MUST name one of "
        "the lesson topics explicitly and feel like a fresh YouTube-style "
        "opening. NEVER reuse the prompt's example sentence pattern ('aaj aapne "
        "dekha ki ... samajhuyacha'); phrase it in your own words. The opening "
        "must not repeat any phrase used in a later narration.\n"
        f"SOURCE DOC (ground the opening in real module names only if natural):\n"
        f"{content[:1200]}"
        "\nReply with ONLY the opening line as a single JSON string."
    )
    raw = _ask_llm_stable(prompt, tries=2).strip()
    try:
        obj: object = json.loads(raw)
        if isinstance(obj, str) and obj.strip():
            return obj.strip()
        if isinstance(obj, dict):
            for key in ("opening", "text", "line", "value", "content", "0"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except json.JSONDecodeError:
        pass
    m = re.search(r'"((?:[^"\\]|\\.)*)"', raw)
    if m and m.group(1).strip():
        return m.group(1).strip()
    lines = [ln.strip('`" \t') for ln in raw.splitlines() if ln.strip('`" \t')]
    if len(lines) >= 2 and re.match(r"^(sure|here|ok+|of course|\*\*)", lines[0], re.I):
        lines = lines[1:]
    if lines:
        line = lines[0].rstrip(".")
        line = re.sub(r'^\s*\{?\s*"opening"\s*:\s*"?', "", line)
        line = re.sub(r'"?\s*\}?\s*$', "", line)
        return line.strip()
    raise RuntimeError("opening patch returned no usable line")
def _patch_scene(scene: dict, idx: int, msgs: list[str], frame_context: str) -> dict:
    """Rewrite only the troublesome fields of one scene; keep the rest frozen."""
    prompt = (
        "Here is ONE scene from a lesson plan as strict JSON:\n"
        f"{json.dumps(scene, ensure_ascii=False)}\n\n"
        "Fix ALL of these problems IN THIS SCENE ONLY (every other scene is "
        "already correct and must stay as-is):\n- "
        + "\n- ".join(msgs)
        + "\n\nRules: keep this scene's [title] and [section] unchanged unless "
        "an instruction above says otherwise. Change ONLY the fields you must "
        "fix. The narration must mix Marathi/Hindi words with English, open by "
        "naming its module, and start and end differently from every other "
        "scene. Every fact must reuse real names/numbers from the source docs "
        "in the scene frame below; never invent. Bullet lines must be unique "
        "across the whole lesson.\n"
        + frame_context
        + "\nDo NOT emit the plan or other scenes. Reply with ONLY the corrected "
        "scene as a single JSON object with these keys: section, title, "
        "narration, bullets, steps, flow, visual_diagram, code_snippet, analogy, "
        "design_decision (keep it when already good; empty string when this "
        "scene has no genuine X-not-Y design choice)."
    )
    raw = _ask_llm_stable(prompt, tries=2)
    patched = _parse_scene_json(raw)
    patched["section"] = patched.get("section") or scene.get("section", "")
    patched["title"] = patched.get("title") or scene.get("title", "")
    return patched
def _dump_parse_failure(raw: str, context: str) -> None:
    """Persist + echo the raw LLM completion whenever a plan JSON parse fails.

    Supporting diagnosis of the free-7B flakiness: records finish_reason, the
    truncation heuristic, output length, and the full raw response so we can
    tell truncation apart from degenerate repetition apart from schema drift.
    """
    path = Path("output") / "plan_debug_last.raw.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {context}\n"
            f"# finish_reason={_LAST_FINISH!r} "
            f"truncated_heuristic={_was_truncated(raw)!r} len(raw)={len(raw)}\n"
            + raw, encoding="utf-8")
    except OSError:
        pass
    print(f"\n    [debug] {context}")
    print(f"    [debug]   finish_reason={_LAST_FINISH!r} "
          f"truncated_heuristic={_was_truncated(raw)} len(raw)={len(raw)}")
    print(f"    [debug]   raw -> {path}")
    print(f"    [debug]   head={raw[:200]!r}")


def _validate_and_normalize_plan(plan: dict,
                                   scene_target: int | None = None) -> dict:
    """Phase-1 schema gate: normalize text and enforce bullet/scene limits.

    Raises RuntimeError when the plan cannot satisfy the strict schema
    so the caller resamples instead of shipping a structurally invalid
    lesson.
    """
    from .schema import LessonPlan

    for sc in plan.get("scenes", []):
        nar = str(sc.get("narration", "")).strip()
        nar = nar.replace(".jso ", ".json ").replace(".jso", ".json")
        nar = re.sub(r"(\w):(\w)", r"\1: \2", nar)
        sc["narration"] = nar
        for field in ("code_context",):
            val = str(sc.get(field, "")).strip()
            val = re.sub(r"Design choice \w+:", "", val)
            val = re.sub(r"Faisla hua ki:", "", val)
            sc[field] = val.strip()
        if isinstance(sc.get("bullets"), list) and len(sc["bullets"]) > 4:
            sc["bullets"] = sc["bullets"][:4]
    if scene_target:
        plan["scene_target"] = scene_target
    if isinstance(plan.get("scene_target"), int):
        n = len(plan.get("scenes", []))
        target = plan["scene_target"]
        if n > target:
            raise RuntimeError(
                f"scene count {n} > source concept frame "
                f"{target} (1:1 concept contract)")
    try:
        LessonPlan.model_validate(plan)
    except Exception as exc:
        raise RuntimeError(f"schema validation failed: {exc}") from exc
    return plan


def plan_lesson(content: str, target_minutes: float,
                voice: NarrationVoice | None = None,
                concepts: list[str] | None = None,
                topics: list[str] | None = None,
                source_files: list[str] | None = None) -> tuple[dict, list[str]]:
    voice = voice or _MHE_VOICE
    source_content = content
    content = _strip_source_metadata_blocks(content)
    t0 = time.monotonic()
    concepts = concepts or []
    scene_target = _plan_scene_target(concepts)
    scene_frame = _concept_groups(concepts) if concepts else []
    concept_driven = bool(scene_frame) and scene_target == len(scene_frame)
    print(f"  [1/5] Planning lesson ({LLM_MODEL}) ... ", end="", flush=True)
    topics = list(topics) if topics is not None else _extract_topics(content)
    if topics:
        print(f"topics: {', '.join(topics)}; ", end="", flush=True)
    if concept_driven:
        print(f"concepts: {len(concepts)} source headers -> {len(scene_frame)} "
              f"scene(s) ({scene_target} groups), all concepts covered; ",
              end="", flush=True)
    topic_block = (f"\nLESSON TOPICS (the lesson MUST cover these, in this order):\n"
                   f"{', '.join(topics)}") if topics else ""

    outline = scene_frame or _source_outline(content)[:AUTO_TRIM_MAX_SCENES]
    if outline:
        print(f"frame: {len(outline)} source items; ", end="", flush=True)
    outline_block = (
        "\n\nMANDATORY SCENE FRAME: build your scene list from THESE source elements, "
        "one scene per element, IN THIS ORDER. Lift each scene's title and detail from "
        "the DOCUMENTS for that element. Do not add a What/Why/How/Example overlay.\n- "
        + "\n- ".join(outline)
        + "\nTakeaways MUST reuse the element names above verbatim (e.g. "
        "'M1 Data & testset (goldens)'), never a paraphrase."
    ) if outline else ""

    design_ctx = _contrast_sentences(content)
    terms = _repeated_terms(content)
    design_context_block = (
        "\n\nDESIGN CONTEXT - real decision reasoning from the DOCUMENTS. Use it "
        "for each scene's design_decision field and for narration reasoning "
        "(only - never invent anything beyond the DOCUMENTS):"
        + "".join(f"\n- CONTRAST: {s}" for s in design_ctx)
        + "".join(f"\n- DEFINED CONCEPT: {t}" for t in terms)
    ) if (design_ctx or terms) else ""

    def build(grip: str = "") -> dict:
        extra = topic_block + outline_block + design_context_block + grip
        retry_extra = topic_block + outline_block + grip
        window, max_out = choose_planner_budget(
            content,
            lambda c: STUDIO_PROMPT.format(target_minutes=target_minutes,
                                           scene_count=scene_target,
                                           content=c) + extra)
        base = (STUDIO_PROMPT.format(target_minutes=target_minutes,
                                     scene_count=scene_target,
                                     content=_bounded_content(content, window))
                + extra)
        raw: str | None = None
        try:
            raw = _ask_llm_stable(base, max_tokens=max_out)
            return _validate_and_normalize_plan(_parse_plan_json(raw),
                                               scene_target=scene_target)
        except RuntimeError as exc:
            if raw is not None:
                _dump_parse_failure(
                    raw, f"plan grip={'' if not grip else 're-grip'} "
                         f"window={window} budget={max_out} -> {exc}")
            # Truncated/parse-failed plan JSON (finish_reason=length or ragged
            # JSON) is an OUTPUT-side cut: max_tokens was exhausted mid-object.
            # The old fallback re-fixed the source window to 8000 and recomputed
            # the same ~512-token budget, so the retry could never finish a
            # dense plan. Instead drop the optional DESIGN CONTEXT block (pure
            # framing - the plan stays grounded) and re-probe the full window
            # ladder, which hands this retry a real completion budget.
            window2, max_out2 = choose_planner_budget(
                content,
                lambda c: STUDIO_PROMPT.format(target_minutes=target_minutes,
                                               scene_count=scene_target,
                                               content=c) + retry_extra)
            raw2 = _ask_llm_stable(
                STUDIO_PROMPT.format(target_minutes=target_minutes,
                                     scene_count=scene_target,
                                     content=_bounded_content(content, window2))
                + retry_extra,
                max_tokens=max_out2)
            try:
                return _validate_and_normalize_plan(_parse_plan_json(raw2),
                                                    scene_target=scene_target)
            except RuntimeError as exc2:
                _dump_parse_failure(
                    raw2, f"plan fallback grip={'' if not grip else 're-grip'} "
                          f"window={window2} budget={max_out2} -> {exc2}")
                raise

    def attempt(grip: str = "") -> dict | None:
        """One gated rebuild attempt; None when the LLM returns bad JSON."""
        try:
            return build(grip)
        except RuntimeError:
            return None

    plan0 = attempt()
    if plan0 is None:
        raise RuntimeError("LLM could not produce a parseable lesson plan "
                           "after 2 attempts")
    plan: dict = plan0
    if concept_driven:
        plan["source_concepts"] = list(concepts)
        plan["concept_groups"] = list(scene_frame)
        plan["scene_target"] = scene_target
    sections_ok = not _section_problems(plan)
    _normalize_sections(plan)
    _sanitize_opening(plan)
    _trim_scene_overflow(plan, max_scenes=scene_target if concept_driven
                         else TARGET_MAX_SCENES)
    source_bigrams = _content_ngrams(content)
    source_tokens = _content_tokens(content)
    top_terms = _top_source_terms(content)
    grounded = not _grounding_issues(plan, source_bigrams, source_tokens)
    sections_ok = not _section_problems(plan)
    topic_coverage = _topic_coverage_problem(plan, top_terms)
    frame_context = (topic_block + "\n\nSOURCE DOC (lift real names and details "
                     "from here - never invent):\n" + content[:2500]
                     + ("...\n\nSCENE FRAME (source elements, in order):\n- "
                        + "\n- ".join(outline)) if outline
                     else topic_block + "\n\nSOURCE DOC:\n" + content[:2500])
    frame_context += design_context_block

    def _still_bad() -> bool:
        # Language-policy mismatch is advisory (soft) by design; it must not
        # force a whole-plan regen from the free 7B. See
        # narration_matches_voice_policy.
        return bool(_repeated_bullets(plan)
                    or _scene_count_problem(plan) or not grounded
                    or not sections_ok
                    or bool(topic_coverage)
                    or bool(_trim_dropped_topic(plan, outline))
                    or _opening_template_hit(plan)
                    or (topics and not _opening_is_on_topic(plan, topics))
                    or (topics and not _plan_is_on_topic(plan, topics)))

    if _still_bad():
        fixes = _scene_problem_map(plan, topics, source_bigrams,
                                   source_tokens, top_terms=top_terms,
                                   voice=voice, protected=None)
        if fixes:
            capped = sorted(fixes)[:_MAX_PATCH_SCENES]
            print(f"\n  [1/5] plan off-target; patching {len(capped)} scene(s) "
                  "(targeted, other scenes frozen, one round) ... ",
                  end="", flush=True)
            patched_any = False
            for idx in capped:
                msgs = fixes[idx]
                if idx == 1:
                    opening_msgs = [m for m in msgs if m.startswith("opening:")]
                    scene_msgs = [m for m in msgs if not m.startswith("opening:")]
                    if opening_msgs:
                        try:
                            plan["opening"] = _patch_opening(plan, opening_msgs,
                                                             content)
                            patched_any = True
                        except RuntimeError:
                            print("\n  [1/5] opening patch parse failed; keeping "
                                  "original ... ", end="", flush=True)
                    if scene_msgs:
                        try:
                            plan["scenes"][0] = _patch_scene(
                                plan["scenes"][0], 1, scene_msgs, frame_context)
                            patched_any = True
                        except RuntimeError:
                            print("\n  [1/5] scene 1 patch parse failed; keeping "
                                  "original ... ", end="", flush=True)
                else:
                    try:
                        plan["scenes"][idx - 1] = _patch_scene(
                            plan["scenes"][idx - 1], idx, msgs, frame_context)
                        patched_any = True
                    except RuntimeError:
                        print(f"\n  [1/5] scene {idx} patch parse failed; keeping "
                              f"original ... ", end="", flush=True)
            if patched_any:
                _normalize_sections(plan)
                _sanitize_opening(plan)
                grounded = not _grounding_issues(plan, source_bigrams, source_tokens)
                sections_ok = not _section_problems(plan)
                topic_coverage = _topic_coverage_problem(plan, top_terms)
        if _still_bad():
            # The deterministic chain below (openers, dedupe, deepening,
            # ungrounded-drop, thin-repair) resolves the narration + grounding
            # leftovers. Landing the partial plan is cheaper and more reliable
            # than 1-2 whole-plan regrips the free 7B has repeatedly failed to
            # clean up; the persisted plan is still repairable by `verify`.
            remaining = len(_scene_problem_map(
                plan, topics, source_bigrams, source_tokens,
                top_terms=top_terms, voice=voice, protected=None))
            print(f"\n  [1/5] {remaining} issue(s) remain after the patch round; "
                  "landing the partial plan - deterministic repair finishes "
                  "narration/grounding.", end="", flush=True)

    if _opening_template_hit(plan):
        with contextlib.suppress(RuntimeError):
            plan["opening"] = _patch_opening(
                plan,
                ["opening: your opening line copied the prompt's example "
                 "sentence verbatim. Name a lesson topic in your own words."],
                content)

    # Overflow trim already ran on every candidate (first parse + each whole-plan
    # retry) via _trim_scene_overflow(): a 13+ scene plan was cut to 12 with the
    # full audit in plan["_overflow_report"] and all gates re-run on the result.
    # That is a loud, recorded repair - not a silent clamp. Only counts outside
    # the recoverable range (<5 or >12) can still surface here as manual gates.
    _dedupe_plan_bullets(plan)
    clause_dropped = _drop_repeated_bullet_clauses(plan)
    if clause_dropped:
        print(f"\n  [1/5] dropped {clause_dropped} bullet(s) that repeated a "
              f"clause from an earlier bullet in the same scene.", end="",
              flush=True)
    pruned = _prune_bullet_takeaway_echo(plan)
    if pruned:
        print(f"\n  [1/5] pruned {pruned} bullet(s) that echoed a takeaway.",
              end="", flush=True)
    _harvest_takeaways(plan)
    fixed = _fix_placeholder_titles(plan)
    if fixed:
        print(f"\n  [1/5] retitled {fixed} placeholder scene title(s).",
              end="", flush=True)
    dd_removed = _sanitize_design_decisions(plan)
    if dd_removed:
        print(f"\n  [1/5] dropped {dd_removed} degenerate design_decision "
              f"field(s) (generic/repeated).", end="", flush=True)
    dd_sourced = _source_design_decisions(plan, content)
    if dd_sourced:
        print(f"\n  [1/5] sourced {dd_sourced} design_decision field(s) from "
              f"the DOCUMENTS' contrast sentences.", end="", flush=True)
    if isinstance(plan.get("protected_trigrams"), list):
        plan["protected_trigrams"] = \
            [str(p) for p in plan["protected_trigrams"]][:4096]
    elif content:
        _persist_protected_trigrams(plan, content)
    protected = _protected_terms(plan)
    _annotate_source_chunks(plan, content)
    # Phase 2 Pass B: the planner left narration blank, so a short dedicated
    # narration pass writes the whole spoken track. On failure the
    # deterministic chain below rebuilds narration from scene fields instead -
    # a bad narrator can never block a build.
    narrated = _narrate_plan(plan, content, voice=voice, topics=topics)
    if narrated:
        print("\n  [1/5] narration written by dedicated pass (B).",
              end="", flush=True)
    else:
        _assign_openers(plan, voice=voice)
        _assign_closers(plan, voice=voice)
        _dedupe_narration_templates(plan, voice=voice, protected=protected)
        _deepen_narrations(plan, voice=voice)
    _sanitize_plan_source_leaks(plan, voice=voice, source_content=source_content,
                                default_refs=source_files)
    _enforce_unique_narration_trigrams(plan, quiet=True, protected=protected)
    _repair_unsafe_narrations(plan, voice=voice, protected=protected)
    _repair_thin_narrations(plan, voice=voice, protected=protected)
    _trim_narration_word_count(plan, voice=voice, quiet=True)
    if topics:
        plan["opening"] = _force_opening_on_topic(str(plan.get("opening", "")),
                                                     topics)
    ungrounded_dropped = _drop_ungrounded_slide_text(plan, source_bigrams,
                                                     source_tokens)
    if ungrounded_dropped:
        print(f"\n  [1/5] dropped {ungrounded_dropped} ungrounded slide text "
              f"item(s) (no source anchor).", end="", flush=True)
    elapsed = time.monotonic() - t0
    problems = []
    if _narration_is_pure_english(plan, voice=voice):
        problems.append("narration pure-English")
    if topics and not _plan_is_on_topic(plan, topics):
        problems.append("topic drift")
    if topics and not _opening_is_on_topic(plan, topics):
        problems.append("opening drifted")
    if _narration_tail_repeat(plan, protected=protected):
        problems.append("repeating narration phrase")
    unsafe_leftover = _unsafe_repeat_scenes(plan, protected)
    if unsafe_leftover:
        problems.append("unrepairable narration repeat (scene(s) "
                        + ",".join(str(i) for i in unsafe_leftover) + ")")
    if _opening_template_hit(plan):
        problems.append("opening reuses prompt example sentence")
    topic_coverage = _topic_coverage_problem(plan, top_terms)
    if topic_coverage:
        problems.append(topic_coverage)
    g = _grounding_issues(plan, source_bigrams, source_tokens)
    if g:
        problems.append(f"{len(g)} ungrounded slide text item(s)")
    rb = _repeated_bullets(plan)
    if rb:
        problems.append(f"copy-paste bullets reused across scenes: {len(rb)} item(s)")
    nd = _near_dupe_bullets(plan)
    if nd:
        problems.append(f"near-duplicate bullets vs takeaways: {len(nd)} item(s)")
    pt = _placeholder_titles(plan)
    if pt:
        problems.append(f"placeholder title restates section: {len(pt)} scene(s)")
    ft = _merged_token_issues(plan)
    if ft:
        problems.append(f"fused slide token(s) unreadable: {len(ft)} item(s)")
    pc = _scene_count_problem(plan)
    if pc:
        problems.append(pc)
    dropped_topics = _trim_dropped_topic(plan, outline)
    if dropped_topics:
        problems.append(f"overflow trim dropped required source topic(s): "
                        f"{', '.join(map(str, dropped_topics))[:200]}")
    if _section_problems(plan):
        problems.append("section label = full enum (not a single section)")
    if problems:
        print(f"WARNING: {' + '.join(problems)} after one retry; proceeding anyway. "
              f"({elapsed:.0f}s)", flush=True)
    else:
        print(f"\u2713  ({elapsed:.0f}s)", flush=True)
    return plan, topics

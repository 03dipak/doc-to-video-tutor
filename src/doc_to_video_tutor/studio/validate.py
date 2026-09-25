"""guard_plan / review_plan and render-blocking narration gates."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import _SOFT_PREFIXES
from .narration import (
    _narration_repeat_report,
    _protected_terms,
    _unsafe_repeat_scenes,
    narration_matches_voice_policy,
)
from .plan import (
    _grounding_issues,
    _placeholder_titles,
    _repeated_bullets,
    _scene_count_problem,
    _section_problems,
)
from .speech import contains_corrupt_text
from .text import (
    _has_non_latin_script,
    _merged_token_issues,
    _nar_tokens,
    _near_dupe_bullets,
    _ngram_set,
)
from .topics import (
    _opening_is_on_topic,
    _opening_template_hit,
    _plan_is_on_topic,
    _topic_coverage_problem,
)
from .voice import NarrationVoice, _make_voice


def _narration_integrity_problems(plan: dict) -> list[str]:
    """Post-repair sanity: no narration damaged by the repeat machinery.

    Empty narration, sub-minimum spoken length, or fragment artifacts mean the
    deterministic repair refused/left damage -> the plan must be regenerated
    (hard), never shipped and rendered.
    """
    problems: list[str] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        nar = str(sc.get("narration", ""))
        if not nar.strip():
            problems.append(f"scene {i}: empty narration")
            continue
        if len(_nar_tokens(nar)) < 6:
            problems.append(f"scene {i}: narration too short to speak "
                            f"({len(_nar_tokens(nar))} tokens) — repair damaged it")
        if contains_corrupt_text(nar):
            problems.append(
                f"scene {i}: narration has corrupt/mixed-script text (Devanagari "
                "mixed with Cyrillic/Arabic/CJK, control chars, or \ufffd) — "
                "not speakable")
        if not _has_teaching_content(sc):
            problems.append(
                f"scene {i}: transition-only/empty scene — no bullets, steps, "
                f"flow, or design decision and narration is filler "
                f"({len(_nar_tokens(nar))} tokens)")
        if re.match(r"[.,!?;:]", nar.strip()):
            problems.append(f"scene {i}: narration starts with punctuation")
        for frag in (". .", ". . .", "  ,", " .", ",."):
            if frag in nar:
                problems.append(f"scene {i}: fragment artifact {frag!r}")
                break
    return problems


_SLIDE_TEXT_FIELDS = ("title", "section", "bullets", "steps", "flow",
                      "analogy", "design_decision", "visual_diagram",
                      "code_snippet", "takeaways")


def _scene_metadata_problems(plan: dict) -> list[str]:
    problems: list[str] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        if not str(sc.get("topic", "")).strip():
            problems.append(f"scene {i}: missing topic metadata")
        refs = sc.get("source_refs")
        if (not isinstance(refs, list)
                or not any(str(value).strip() for value in refs)):
            problems.append(f"scene {i}: missing source_refs metadata")
        pages = sc.get("bullet_pages")
        if isinstance(pages, list) and pages:
            flat = [str(value) for page in pages
                    if isinstance(page, list) for value in page]
            bullets = [str(value) for value in (sc.get("bullets") or [])]
            if flat != bullets:
                problems.append(
                    f"scene {i}: bullet_pages does not match bullets")
            elif any(len(page) > 4 for page in pages
                     if isinstance(page, list)):
                problems.append(f"scene {i}: bullet_pages exceeds 4 items")
    return problems


def _slide_text_language_problems(plan: dict) -> list[str]:
    """Slide visible text must stay ENGLISH (Latin script).

    v011_006 shipped 4 Devanagari slide items (3 bullets + 1 analogy) through
    every gate: ``contains_corrupt_text`` only sees MIXED scripts, and the
    grounding ``_anchored`` shortcut treats <2-Latin-token text as "anchored",
    so PURE non-Latin invention was invisible to both. This mirrors the
    narration-corrupt precedent: hard FAIL so the plan is honestly rejected /
    resampled instead of rendering Marathi onto slides.
    """
    problems: list[str] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        for field in _SLIDE_TEXT_FIELDS:
            value = sc.get(field, "")
            values = value if isinstance(value, list) else [value]
            for item_index, item in enumerate(values, 1):
                text = str(item)
                if _has_non_latin_script(text):
                    suffix = f"[{item_index}]" if isinstance(value, list) else ""
                    problems.append(
                        f"scene {i} SlideTextNotEnglish: non-Latin script in "
                        f"{field}{suffix}: {text[:64]!r}")
    for field in ("title", "opening"):
        text = str(plan.get(field, ""))
        if _has_non_latin_script(text):
            problems.append(
                f"plan {field} SlideTextNotEnglish: non-Latin script: "
                f"{text[:64]!r}")
    for j, t in enumerate(plan.get("takeaways") or [], 1):
        if _has_non_latin_script(str(t)):
            problems.append(
                f"takeaway {j} SlideTextNotEnglish: non-Latin script: "
                f"{str(t)[:64]!r}")
    return problems


_TRANSITION_FILLER = frozenset(
    {w for w in (
        "ab aur ye yah yo to bhi hi hai hain ho ki ke ko ka kar karke se mein "
        "par per ne na is isse us iska iske uska uske kehta kehte chaliye dekhte "
        "samjhenge aage badhte badta taraf lekin phir the and of to in on at for "
        "with that this from into onto some then where here when") if len(w) >= 3})


def _has_teaching_content(sc: dict) -> bool:
    """A scene must carry real lesson material, not a bare transition.

    Mirrors the reviewer rule: reject scenes with fewer than 2 bullets or
    purely transitional text. Any slide substance (bullets/steps/flow/analogy
    or a design decision) makes the scene speakable; otherwise the narration
    alone must carry a real informational core (content-word density).
    """
    if len(sc.get("bullets") or []) >= 2:
        return True
    if sc.get("steps") or sc.get("flow") or sc.get("analogy"):
        return True
    if str(sc.get("design_decision", "")).strip():
        return True
    return _narration_has_content(
        str(sc.get("narration", "")), str(sc.get("title", "")))


def _narration_has_content(nar: str, title: str) -> bool:
    """True when a narration without slide substance still teaches: it must be
    long enough and dominated by non-title, non-filler words. A Scene-3-style
    'Ab aage badhte hain <title> ki taraf' echo scores ~0 content."""
    title_words = {w for w in re.sub(r"[\u2192:()\d]", " ", title)
                   .casefold().split() if len(w) >= 3}
    tokens = [w.strip(".,;:!?") for w in nar.casefold().split()]
    tokens = [w for w in tokens if len(w) >= 3]
    if len(tokens) < 8:
        return False
    content = [w for w in tokens
               if w not in title_words and w not in _TRANSITION_FILLER]
    return len(content) >= 3 and len(content) / len(tokens) >= 0.4
def _render_blocking_problems(plan: dict,
                              voice: NarrationVoice | None = None,
                              protected: frozenset[str] | None = None
                              ) -> list[str]:
    """Hard, pre-render audit: problems that MUST be zero before TTS/render.

    Deterministic only (no LLM): recompute the repeat gate from the plan's own
    content, so a plan that shipwrecks the free 7B never reaches audio/video
    with a banned narration phrase, an unrepairable repeat, damaged narration,
    or an out-of-range scene count. Each finding is actionable and names the
    exact offending scene(s)/phrase(s) so the fix is mechanical.
    """
    protected = _protected_terms(plan) if protected is None else protected
    banned, _ = _narration_repeat_report(plan, protected)
    problems: list[str] = []
    if banned:
        problems.append(f"RENDER-BLOCKING: {len(banned)} banned narration "
                        f"phrase(s) unresolved: {sorted(banned)[:4]}"
                        + (" ..." if len(banned) > 4 else ""))
    unsafe = _unsafe_repeat_scenes(plan, protected)
    if unsafe:
        problems.append("RENDER-BLOCKING: unrepairable narration repeat "
                        f"(scene(s) {','.join(str(i) for i in unsafe)})")
    problems += _narration_integrity_problems(plan)
    problems += _scene_metadata_problems(plan)
    problems += _slide_text_language_problems(plan)
    pc = _scene_count_problem(plan)
    if pc:
        problems.append(f"RENDER-BLOCKING: {pc}")
    return problems
def guard_plan(plan: dict, topics: list[str],
               source_bigrams: list[str] | None = None,
               source_tokens: list[str] | None = None,
               source_top: list[str] | None = None,
               voice: NarrationVoice | None = None) -> list[str]:
    """Return a list of problems found in a lesson plan (empty = PASS)."""
    problems = []
    passed, _reason = narration_matches_voice_policy(plan, voice)
    if not passed:
        problems.append("narration pure-English")
    if topics and not _plan_is_on_topic(plan, topics):
        problems.append("topic drift")
    if topics and not _opening_is_on_topic(plan, topics):
        problems.append("opening drifted")
    protected = _protected_terms(plan)
    banned, _prot = _narration_repeat_report(plan, protected)
    unsafe = _unsafe_repeat_scenes(plan, protected)
    if unsafe:
        problems.append("unrepairable narration repeat (scene(s) "
                        + ",".join(str(i) for i in unsafe) + ")")
    elif banned:
        problems.append("repeating narration phrase")
    problems += _narration_integrity_problems(plan)
    problems += _scene_metadata_problems(plan)
    problems += _slide_text_language_problems(plan)
    if _opening_template_hit(plan):
        problems.append("opening reuses prompt example sentence")
    if source_top:
        cov = _topic_coverage_problem(plan, source_top)
        if cov:
            problems.append(cov)
    problems += _section_problems(plan)
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
    if source_bigrams and source_tokens:
        ngrams = _ngram_set(source_bigrams)
        issues = _grounding_issues(plan, ngrams, set(source_tokens))
        if issues:
            problems.append(f"ungrounded slide text: {len(issues)} item(s)")
    return problems
def review_plan(path: Path, topics: list[str] | None = None) -> int:
    """Audit a saved .plan.json and print a verdict. Exit 0 = PASS, 1 = issues."""
    data = json.loads(path.read_text(encoding="utf-8"))
    plan = data.get("plan", data)
    saved = data.get("topics", [])
    ref = topics or saved
    src_bg = data.get("source_bigrams")
    src_tk = data.get("source_tokens")
    src_top = data.get("source_top")
    voice = _make_voice(data.get("voice") or (data.get("narration_voice") or {}).get("name"))
    problems = guard_plan(plan, ref, source_bigrams=src_bg, source_tokens=src_tk,
                          source_top=src_top, voice=voice)
    hard = [p for p in problems if not any(p.startswith(s) for s in _SOFT_PREFIXES)]
    soft = [p for p in problems if any(p.startswith(s) for s in _SOFT_PREFIXES)]
    protected = _protected_terms(plan)
    _banned, _prot = _narration_repeat_report(plan, protected)
    ungrounded = (_grounding_issues(plan, _ngram_set(src_bg),
                                    set(src_tk)) if src_bg and src_tk else [])
    print(f"Reviewing {path}")
    print(f"  title     : {plan.get('title')!r}")
    print(f"  voice     : {voice.name}")
    print(f"  scenes    : {len(plan.get('scenes', []))}")
    print(f"  protected repeats: {len(_prot)}")
    if ref:
        print(f"  topics    : {', '.join(ref)}")
    if hard:
        print("  VERDICT   : ISSUES")
        for p in hard:
            print(f"    - {p}")
        for d in ungrounded:
            print(f"      {d}")
        return 1
    if soft:
        print("  VERDICT   : PASS (soft warnings)")
        for p in soft:
            print(f"    ~ {p}")
        return 0
    print("  VERDICT   : PASS")
    return 0

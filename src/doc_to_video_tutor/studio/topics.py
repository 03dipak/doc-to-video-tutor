"""On-topic checks and topic-coverage analysis."""

from __future__ import annotations

from .config import _BANNED_DISTINCT, _BANNED_NGRAMS
from .text import _WORD, _tokens_of, _topic_tokens


def _opening_is_on_topic(plan: dict, topics: list[str]) -> bool:
    """Strict: the spoken opening itself must reference a lesson topic."""
    if not topics:
        return True
    ref = _topic_tokens(topics)
    mine = {w.lower() for w in _WORD.findall(str(plan.get("opening", "")))}
    mine = {w for w in mine if len(w) > 2}
    return bool(mine & ref)
def _force_opening_on_topic(opening: str, topics: list[str]) -> str:
    """Deterministic last resort: ensure the opening names a lesson topic.

    The free 7B often generates a plausible Hinglish opening that drifts
    away from the actual topic tokens; resampling usually wastes budget
    without fixing it.  When a hard-gate check shows the topic words are
    absent, deterministically prefix the opening with a topic-naming phrase
    so `_opening_is_on_topic` always passes.
    """
    if not topics:
        return opening
    low = opening.lower()
    if any(t.lower() in low for t in topics):
        return opening
    ref = _topic_tokens(topics)
    name = next(iter(sorted(ref, key=len, reverse=True)),
                topics[0] if topics else "")
    if not name:
        return opening
    return f"Chaliye {name} samajh ke shuruwat karte hai. {opening}"
def _opening_template_hit(plan: dict) -> bool:
    """Opening that verbatim/near-verbatim reuses the prompt's own example.

    Exact 3-gram matches always count; a 2-gram match only counts when it
    carries a distinctive word (len>=7) from the example, e.g. 'samajhuyacha'.
    """
    toks = _tokens_of(plan.get("opening", ""))
    if len(toks) < 2:
        return False
    triples = {tuple(toks[i:i + 3]) for i in range(max(0, len(toks) - 2))}
    banned3 = {g for g in _BANNED_NGRAMS if len(g) == 3}
    if triples & banned3:
        return True
    pairs = {tuple(toks[i:i + 2]) for i in range(max(0, len(toks) - 1))}
    banned2 = {g for g in _BANNED_NGRAMS if len(g) == 2}
    return any(tok in _BANNED_DISTINCT for g in (pairs & banned2) for tok in g)
def _plan_is_on_topic(plan: dict, topics: list[str]) -> bool:
    """True when at least one scene title shares a meaningful word with the topics."""
    if not topics:
        return True
    ref = _topic_tokens(topics)
    if not ref:
        return True
    mine: set[str] = set()
    for sc in plan.get("scenes", []):
        mine |= {w.lower() for w in _WORD.findall(str(sc.get("title", "")))}
    mine |= {w.lower() for w in _WORD.findall(str(plan.get("opening", "")))}
    mine |= {w.lower() for w in _WORD.findall(str(plan.get("title", "")))}
    mine = {w for w in mine if len(w) > 2}
    return bool(mine & ref)
def _topic_coverage_problem(plan: dict, top_terms: list[str]) -> str:
    """Flag lessons that barely touch the docs' dominant source vocabulary.

    Grounding only asks 'is this phrase anchored somewhere in the doc', so a
    lesson can be grounded-but-shallow. This measures how many of the docs'
    core terms actually reach scene titles/bullets/takeaways: a stated topic
    with near-zero representation (e.g. 'metric evaluation' semantics) fails.
    """
    if not top_terms:
        return ""
    covered: set[str] = set()
    for sc in plan.get("scenes", []):
        covered |= set(_tokens_of(sc.get("title", "")))
        for b in (sc.get("bullets") or []):
            covered |= set(_tokens_of(b))
    for t in (plan.get("takeaways") or []):
        covered |= set(_tokens_of(t))
    hits = len(covered & set(top_terms))
    need = max(5, len(top_terms) // 4)
    if hits >= need:
        return ""
    return f"topic coverage {hits}/{len(top_terms)} core source terms"

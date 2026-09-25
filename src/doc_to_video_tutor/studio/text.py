"""Token/ngram text-machinery shared across narration, topics, plan and validate."""

from __future__ import annotations

import re
import unicodedata
from itertools import pairwise

_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
# Unsegmented-script ranges (Han, Kana, Hangul, fullwidth). Layer A's canonical
# tokenizer is whitespace-delimited; a CJK passage has no intra-token spaces, so
# every "token" becomes one giant blob and trigram windows turn degenerate.
_UNSEGMENTED = re.compile(
    r"[\u2E80-\u2FDF\u3000-\u303F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF"
    r"\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFFEF]")
# Letters from non-Latin scripts. Slide visible text MUST stay English
# (Latin script) per the STUDIO_PROMPT, but this build shipped 4 Devanagari
# slide items: the mixed-script corrupt detector only sees MIXED scripts and
# the grounding '_anchored' shortcut treats <2-Latin-token text as "anchored",
# so pure non-Latin invention was invisible to every gate. Symbols/arrows
# used in visual_diagram (→ | etc.) are not letters, so they stay Latin-safe.
_NON_LATIN_SCRIPT = re.compile(
    r"[\u0370-\u03FF"          # Greek
    r"\u0400-\u04FF"           # Cyrillic
    r"\u0530-\u058F"           # Armenian
    r"\u0590-\u05FF"           # Hebrew
    r"\u0600-\u06FF"           # Arabic
    r"\u0900-\u097F"           # Devanagari
    r"\u0980-\u09FF"           # Bengali
    r"\u0A00-\u0A7F"           # Gurmukhi
    r"\u0A80-\u0AFF"           # Gujarati
    r"\u0B00-\u0B7F"           # Oriya
    r"\u0B80-\u0BFF"           # Tamil
    r"\u0C00-\u0C7F"           # Telugu
    r"\u0C80-\u0CFF"           # Kannada
    r"\u0D00-\u0D7F"           # Malayalam
    r"\u0E00-\u0E7F"           # Thai
    r"\u0F00-\u0FFF"           # Tibetan
    r"\u10A0-\u10FF"           # Georgian
    r"\u1100-\u11FF"           # Hangul Jamo
    r"\u2E80-\u2FDF"           # CJK radicals
    r"\u3000-\u30FF"           # CJK punct + Kana
    r"\u3400-\u4DBF\u4E00-\u9FFF"   # Han
    r"\uAC00-\uD7AF"           # Hangul
    r"\uF900-\uFAFF"           # CJK compat
    r"\uFF00-\uFFEF]"          # fullwidth
)
_SOURCE_META_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:\*\*)?(?:source|lld\s+ref)\s*(?:\*\*)?\s*:",
    re.IGNORECASE,
)
_SOURCE_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SOURCE_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
_SOURCE_CITATION_RE = re.compile(r"(?<!\w):\d+(?:-\d+)?(?!\w)")
_unsegmented_warned = False

def _strip_source_metadata_blocks(text: str) -> str:
    lines: list[str] = []
    skipping = False
    for line in str(text).splitlines():
        if _SOURCE_META_RE.match(line):
            skipping = True
            continue
        if skipping:
            stripped = line.strip()
            if not stripped:
                lines.append("")
                continue
            if _SOURCE_HEADING_RE.match(line) or _SOURCE_LIST_RE.match(line):
                skipping = False
            elif line[:1].isspace() or stripped.startswith((":", "`", "+", ")", "]")):
                continue
            else:
                skipping = False
        lines.append(line)
    return "\n".join(lines)

def _has_source_citation(text: str) -> bool:
    raw = str(text)
    return bool(_SOURCE_CITATION_RE.search(raw)
                or re.search(r"\b(?:source|lld\s+ref)\s*:", raw, re.IGNORECASE))

def _strip_source_citations(text: str) -> str:
    cleaned = _SOURCE_CITATION_RE.sub("", _strip_source_metadata_blocks(text))
    cleaned = re.sub(r"\bLLD\s+ref\s*:", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()

def _has_non_latin_script(text: str) -> bool:
    """True when a string contains letters from a non-Latin script.

    Slide visible text, bullets, takeways, analogies, diagrams and snippets
    must all stay English (Latin script): the STUDIO_PROMPT requires it, and
    edge-tts SwaraNeural cannot be trusted to read alien script as English.
    Latin-1 / Latin-Extended / IPA / combining marks / arrows / box symbols
    are all Latin-safe; Indic, CJK, Greek, Cyrillic, Hebrew, Arabic, Thai,
    Tibetan, Georgian and Armenian letters are not.
    """
    return bool(_NON_LATIN_SCRIPT.search(str(text)))
def _tokens_of(text: str) -> list[str]:
    """Lowercased alphanumeric tokens of a text (length>=2)."""
    return [w.lower() for w in _WORD.findall(text) if len(w) >= 2]
def _topic_tokens(topics: list[str]) -> set[str]:
    """Bag of lowercase words from a topic list (drop tiny filler words)."""
    tokens: set[str] = set()
    for t in topics:
        words = {w.lower() for w in _WORD.findall(t)}
        tokens |= {w for w in words if len(w) > 2}
    return tokens
def _nar_tokens(nar: str) -> list[str]:
    """Canonical narration tokens — the SINGLE tokenizer for every repeat rule.

    P5 sequence: (1) NFKC-normalize, (2) fold every Unicode whitespace /
    zero-width / bidi char to one ASCII space, (3) treat em/en-dashes,
    compound-code separators ('/', '_') and bracket pairs as word-break
    delimiters before punctuation stripping, so multi-part code identifiers
    (e.g. 'module_name.sub_func()') split predictably into discrete tokens for
    n-gram indexing. Hyphens keep their token ('quality-gate' = one token) so
    prose and code stay symmetric across the gate and the enforcer.
    """
    global _unsegmented_warned
    s = unicodedata.normalize("NFKC", str(nar)).lower()
    # Zero-width / bidi / word-joiner chars have no isspace() truthiness; turn
    # them into real spaces so they cannot fuse two words into one token and
    # skew the trigram windows the repeat gate and enforcer both rely on.
    for ch in "\u200b\u200c\u200d\u200e\u200f\ufeff":
        s = s.replace(ch, " ")
    s = re.sub(r"\s+", " ", s)
    if not _unsegmented_warned and _UNSEGMENTED.search(s):
        _unsegmented_warned = True
        print("  WARN: Layer A repeat safety is whitespace-delimited only; "
              "unsegmented script (CJK/Hangul/fullwidth) detected in narration "
              "- trigram windows will be degenerate. Transliterate to a "
              "spaced script or segment the text before narrating.")
    for ch in ".,!?\u0964\"'\u2019\u2018\u201d\u201c\u2013\u2014/_()[]{}":
        s = s.replace(ch, " ")
    return s.split()
def _nar_gram_key(tok3: tuple[str, ...]) -> str:
    return " ".join(tok3)
def _nar_3grams_t(tokens: list[str]) -> frozenset[str]:
    return frozenset(_nar_3grams_t_ordered(tokens))
def _nar_3grams_t_ordered(tokens: list[str]) -> list[str]:
    return [_nar_gram_key(tuple(tokens[j:j + 3]))
            for j in range(max(0, len(tokens) - 2))
            if len(tokens[j:j + 3]) == 3
            and all(len(w) > 1 for w in tokens[j:j + 3])]
def _entity_tokens(*texts: str) -> frozenset[str]:
    """Acronyms / CamelCase / numeric / path-like tokens (protected from deletion).

    Detected on ORIGINAL casing; stored lowercased to match the canonical gram
    space. A gram that contains any of these is treated as protected
    terminology, never auto-deleted by the repeat enforcer.
    """
    out: set[str] = set()
    for text in texts:
        for tok in re.findall(r"[A-Za-z0-9_.:+/~-]+", str(text)):
            low = tok.lower()
            is_entity = (
                "/" in tok or "_" in tok
                or (tok.count(".") >= 1 and not tok.endswith("."))
                or any(ch.isdigit() for ch in tok)
                or re.fullmatch(r"[A-Z]{2,}[A-Z0-9]*", tok)
                or re.fullmatch(r"(?:[A-Z][a-z]+){2,}", tok)
            )
            if is_entity:
                out.add(low)
    return frozenset(out)
_STOP = {
    "about", "after", "all", "also", "and", "any", "are", "as", "at", "back",
    "be", "because", "been", "before", "both", "but", "by", "can", "could",
    "each", "for", "from", "had", "has", "have", "how", "in", "into", "is",
    "it", "its", "just", "many", "more", "most", "much", "not", "now", "of",
    "on", "one", "only", "or", "our", "out", "over", "per", "such", "than",
    "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "to", "under", "up", "very", "was", "we", "were", "when", "where", "which",
    "while", "who", "why", "will", "with", "would", "you", "your",
}
def _text_ngrams(text: str) -> set[tuple[str, str]]:
    """Adjacent-word pairs of content tokens (non-stopword, len>2) in a text."""
    words = [w.lower() for w in _WORD.findall(text)
             if len(w) > 2 and w.lower() not in _STOP]
    return set(pairwise(words))
def _content_tokens(content: str) -> set[str]:
    """Content (non-stopword, len>2) tokens from the source docs."""
    return {w.lower() for w in _WORD.findall(content)
            if len(w) > 2 and w.lower() not in _STOP}
def _content_ngrams(content: str) -> set[tuple[str, str]]:
    """All content word-bigrams from the source docs, scoped per line.

    Line-scoping avoids anchoring slide text to words that only co-occur
    across unrelated paragraphs.
    """
    ngrams: set[tuple[str, str]] = set()
    for line in content.splitlines():
        ngrams |= _text_ngrams(line)
    return ngrams
def _top_source_terms(content: str, k: int = 24) -> list[str]:
    """The k most frequent content tokens in the source docs (coverage probe)."""
    counts: dict[str, int] = {}
    for w in _WORD.findall(content.lower()):
        if len(w) > 2 and w not in _STOP:
            counts[w] = counts.get(w, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
def _ngram_set(source_bigrams: list[str]) -> set[tuple[str, str]]:
    """Rebuild typing-stable n-gram pairs from persisted "a b" strings."""
    out: set[tuple[str, str]] = set()
    for b in source_bigrams:
        parts = b.split()
        if len(parts) == 2:
            out.add((parts[0], parts[1]))
    return out
def _merged_token_issues(plan: dict, max_run: int = 32) -> list[str]:
    """Slide-visible words fused by the model into one unbreakable token.

    E.g. 'comparedifferentiatescandidatevsbaseline' renders as an unreadable
    43-char run. Flagged for the hand-off to the scene patcher. Code
    identifiers/numbers with '_' or '.' are left alone (real names stay exact).
    """
    fields = ("title", "bullets", "steps", "flow", "analogy",
              "code_snippet", "visual_diagram")
    issues: list[str] = []
    for i, sc in enumerate(plan.get("scenes", []), 1):
        for field in fields:
            val = sc.get(field)
            lines = [str(val)] if isinstance(val, str) else [str(x) for x in (val or [])]
            for ln in lines:
                for tok in re.findall(r"[A-Za-z]+", ln):
                    if len(tok) > max_run:
                        issues.append(f"scene {i} {field}: unbroken token "
                                      f"{tok!r} ({len(tok)} letters)")
    return issues
def _token_set(text: str) -> set[str]:
    """Content tokens (stopwords dropped) for similarity checks."""
    return {t for t in _WORD.findall(str(text).lower())
            if len(t) > 1 and t not in _STOP}
def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
def _near_dupe_bullets(plan: dict, threshold: float = 0.78) -> list[str]:
    """Bullet lines near-duplicating another scene's bullet or a takeaway.

    `_repeated_bullets` only catches exact lowercase matches across scenes and
    never compares bullets VS takeaways. Takeaways are summaries, so a bullet
    that is ~80%+ the same tokens as another scene's bullet or a takeaway is
    copy-paste text, not a distinct fact.
    """
    scenes = plan.get("scenes", [])
    bullets: list[tuple[int, str]] = []
    for i, sc in enumerate(scenes, 1):
        for b in (sc.get("bullets") or []):
            bullets.append((i, str(b).strip().lower()))
    takeaways = [str(t).strip().lower() for t in (plan.get("takeaways") or [])]
    flagged: list[str] = []
    for i, b in bullets:
        if not b:
            continue
        tb = _token_set(b)
        hit = any(_jaccard(tb, _token_set(other)) >= threshold
                  for other_i, other in bullets if other_i != i and other)
        hit = hit or any(t and _jaccard(tb, _token_set(t)) >= threshold
                         for t in takeaways)
        if hit:
            flagged.append(b)
    return sorted({f for f in flagged})

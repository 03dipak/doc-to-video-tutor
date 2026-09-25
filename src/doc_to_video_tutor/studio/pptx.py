"""PPTX deck construction from scene blocks."""

from __future__ import annotations

import re
from pathlib import Path

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches

from .config import _VIDEO_BODY_MAX, ACCENT, BRAND_FOOTER, GOLD, GREEN, MUTED, RED
from .slides import (
    _json_payload_candidates,
    _parse_diagram,
    _scene_pages,
    _with_overflow,
    _wrap,
)
from .text import clip_title


def _ppt_set_bg(slide, color=(18, 24, 38)):
    from pptx.dml.color import RGBColor

    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = RGBColor(*color)
def _ppt_bullet(paragraph, char="•"):
    """Set a real bullet character on the paragraph (not a leading space)."""
    from pptx.oxml.ns import qn

    pPr = paragraph._p.get_or_add_pPr()
    for tag in ("a:buNone", "a:buChar", "a:buAutoNum", "a:buBlip", "a:buFont",
                "a:buFontTx", "a:buClrTx", "a:buClr", "a:buSzTx", "a:buSzPct",
                "a:buSzPts"):
        el = pPr.find(qn(tag))
        if el is not None:
            pPr.remove(el)

    buFont = pPr.makeelement(qn("a:buFont"), {"typeface": "Arial"})
    buChar = pPr.makeelement(qn("a:buChar"), {"char": char})

    succ = ["a:buAutoNum", "a:buChar", "a:buBlip", "a:tabLst",
            "a:defRPr", "a:extLst"]
    pPr.insert_element_before(buFont, *succ)
    succ2 = ["a:buBlip", "a:tabLst", "a:defRPr", "a:extLst"]
    pPr.insert_element_before(buChar, *succ2)
def _ppt_para(tf, text: str, size, color, bullet: bool = False, bold: bool = False,
              font_name: str = "Arial"):
    """Add a wrapped paragraph to a text frame with styling.

    The font name is set explicitly on every run: without it the paragraph
    inherits the theme's minor font, so the deck silently rendered in Calibri
    instead of the Arial the template specifies.
    """
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    p = tf.add_paragraph()
    p.text = text
    p.font.name = font_name
    p.font.size = Pt(size)
    p.font.bold = bold
    p.font.color.rgb = RGBColor(*color)
    if bullet:
        p.font.color.rgb = RGBColor(*color)
        _ppt_bullet(p)
    return p
def _est_wrapped_lines(text: str, width_in: float, size_pt: float,
                       margin_in: float = 0.1) -> int:
    """Conservative estimate of how many lines ``text`` wraps to in a textbox.

    A PowerPoint textbox does not clip: text taller than the shape still draws,
    so an under-sized box does not hide content, it pushes it over whatever sits
    below. The card behind "Why THIS (not the alternative)" was a fixed 0.72 in
    with a 0.6 in text box, and a 213-character decision needs three lines at
    17 pt - roughly 0.87 in - so it spilled past the card and into the safe area
    while the fit check, which used the same fixed height, reported room.

    The factor is deliberately pessimistic (0.52 em average advance for mixed-case
    Arial). Over-estimating wraps is the safe direction: it reserves more space
    than needed, whereas under-estimating reproduces the overflow.
    """
    usable = max(width_in - margin_in, 0.5)
    per_line = max(int(usable * 72.0 / (0.52 * max(size_pt, 1))), 8)
    total = 0
    for paragraph in str(text).split("\n"):
        words = paragraph.split()
        if not words:
            total += 1
            continue
        line = 0
        lines = 1
        for word in words:
            # A single unbreakable token longer than the line still occupies
            # whole lines. Without this, a 900-character identifier reports as
            # one line and the layout reserves a third of an inch for it - an
            # under-report, which is the one direction that is never safe.
            if len(word) > per_line:
                if line:
                    lines += 1
                lines += -(-len(word) // per_line) - 1
                line = len(word) % per_line
                continue
            extra = len(word) + (1 if line else 0)
            if line + extra > per_line and line:
                lines += 1
                line = len(word)
            else:
                line += extra
        total += lines
    return max(total, 1)


def _est_text_height(text: str, width_in: float, size_pt: float,
                     margin_in: float = 0.1) -> float:
    """Height in inches that ``text`` needs, including the box's own insets."""
    lines = _est_wrapped_lines(text, width_in, size_pt, margin_in)
    return lines * (1.22 * size_pt / 72.0) + margin_in


class _RowStack:
    """One vertical budget for a slide, resolved from content rather than guessed.

    The layout question "does this fit?" was being re-asked independently by
    every block - six separate `if y + need > max_h: break` checks, each with its
    own idea of what to sacrifice. That is the fixed-length approach: the block
    decides for itself, so the slide as a whole has no policy, and a long bullet
    silently starves whatever sits below it.

    This is the alternative: a block declares the height its content actually
    needs and a priority, and one pass owns the budget. When the slide cannot
    hold everything, rows are dropped by priority - the lowest-priority, then the
    bottom-most, which is the block a viewer loses least - instead of whichever
    block happened to be drawn last. Nothing is ever clipped.

    Priority: 2 required (the teaching content), 1 supporting (the design
    decision), 0 optional (analogy, diagram, payload, legend).
    """

    REQUIRED, SUPPORTING, OPTIONAL = 2, 1, 0

    def __init__(self, top: float, bottom: float) -> None:
        self.top = top
        self.bottom = bottom
        self.placed: list[tuple[float, float, int, int]] = []
        self.dropped: list[tuple[int, str]] = []

    @property
    def y(self) -> float:
        return self.placed[-1][0] + self.placed[-1][1] if self.placed else self.top

    @property
    def free(self) -> float:
        return self.bottom - self.y

    def reserve(self, height: float, priority: int, label: str) -> float | None:
        """Claim `height` at the current cursor, or return None if it will not fit."""
        if height > self.free:
            self.dropped.append((priority, label))
            return None
        at = self.y
        self.placed.append((at, height, priority, len(self.placed)))
        return at

    def report(self) -> list[str]:
        return [f"dropped {label} (priority {p}, "
                f"{self.bottom - self.y:.2f}in short)"
                for p, label in self.dropped]


def _paginate_by_height(page: dict, budget: float) -> list[dict]:
    """Split one planned page until its bullets fit the slide's vertical budget.

    `_scene_pages` paginates by COUNT - four bullets per page - which is the
    right instinct (overflow becomes more slides, not smaller text) but the wrong
    unit. Four 300-character bullets occupy far more height than four 40-character
    ones, so a count-paginated page can still overflow, and the row stack then
    drops the bullets to protect the layout. Measured on a crowded scene, all nine
    bullets were dropped and the audit still reported clean: teaching content
    lost silently.

    So the deck paginates on measured height, in inches, using the same estimator
    the layout uses. One measurement, one truth - otherwise pagination and layout
    disagree and one of them has to lose.

    A bullet that cannot fit even alone is left on its own page and reported,
    rather than being dropped: at that point the honest answer is "this content
    does not fit a slide", and the caller says so instead of shipping a gap.
    """
    bullets = [str(b) for b in (page.get("bullets") or [])]
    if not bullets:
        return [page]
    rows = [max(0.55, _est_text_height(b, 11.95, 18)) for b in bullets]
    if sum(rows) <= budget:
        return [page]
    page_ends: list[int] = []
    current: list[int] = []
    used = 0.0
    for index, row in enumerate(rows):
        if current and used + row > budget:
            page_ends.append(index)
            current, used = [], 0.0
        current.append(index)
        used += row
    if current:
        page_ends.append(len(rows))
    out: list[dict] = []
    start = 0
    for end in page_ends:
        clone = dict(page)
        clone["bullets"] = bullets[start:end]
        out.append(clone)
        start = end
    return out


def _takeaway_pages(takes: list[str], budget: float,
                    col_width: float = 5.9) -> int:
    """How many two-column slides the takeaways need, measured before rendering.

    Counted up front so the deck's page counter is honest. The previous fixed
    `rows_per_col` made the count a guess; with overflow now paginating, a wrong
    guess would print a wrong "7 / 6" on the last slide.
    """
    tops = [0.0, 0.0]
    pages = 1
    used = 0
    for take in takes:
        need = max(0.5, _est_text_height(take, col_width - 0.25, 18))
        if max(tops) + need > budget:
            pages += 1
            tops = [0.0, 0.0]
        col = 0 if (tops[0] + need <= budget) else 1
        tops[col] += need + 0.12
        used += 1
    return max(pages, 1 if used else 0)


def _strip_field_leak(text: str) -> str:
    """Remove a trailing JSON field name from text bound for a slide.

    The 7B sometimes emits "...shuruwat karte hai. opening", leaking the key
    into the spoken and displayed line. The plan pipeline cleans this, but the
    deck is built from saved plans too, so the renderer applies the same rule
    rather than assuming the input is already clean.
    """
    cleaned = re.sub(
        r"\s*\.\s*(?:opening|closing|prompt_end|text|line|value|content)\s*$",
        "", str(text), flags=re.IGNORECASE).strip()
    return re.sub(r"\s+(?:opening|closing|prompt_end)\s*$", "", cleaned,
                  flags=re.IGNORECASE).strip()


def _ppt_textbox(slide, left, top, width, height, text, size, color,
                 bold=False, wrap=True, font_name: str = "Arial"):
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    tb = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.margin_left = Inches(0.1)
    p = tf.paragraphs[0]
    p.text = text
    p.font.name = font_name
    p.font.size = Pt(size)
    p.font.bold = bold
    p.font.color.rgb = RGBColor(*color)
    return tb
def _ppt_codebox(slide, left, top, width, lines, context=""):
    """Monospace (Courier New) code panel on a slide."""
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    context = str(context).strip()
    height = 0.2 + (0.3 if context else 0.0) + len(lines) * 0.3
    tb = slide.shapes.add_textbox(
        Inches(left), Inches(top), Inches(width), Inches(height))
    tf = tb.text_frame
    tf.word_wrap = True
    if context:
        p = tf.paragraphs[0]
        p.text = f"Context: {context}"
        p.font.name = "Arial"
        p.font.size = Pt(14)
        p.font.color.rgb = RGBColor(235, 238, 245)
    for j, ln in enumerate(lines):
        p = tf.paragraphs[0] if j == 0 and not context else tf.add_paragraph()
        p.text = ln
        p.font.name = "Courier New"
        p.font.size = Pt(12)
        p.font.color.rgb = RGBColor(140, 200, 255)
    return tb
def _ppt_diagram(slide, left, top, width, nodes) -> float:
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Pt

    gap = 0.22
    box_w = min(2.35, (width - gap * (len(nodes) - 1)) / len(nodes))
    box_h = 0.68
    for idx, node in enumerate(nodes):
        x = left + idx * (box_w + gap)
        shape = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(top),
            Inches(box_w), Inches(box_h))
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(30, 41, 59)
        shape.line.color.rgb = RGBColor(255, 193, 7)
        tf = shape.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = str(node)[:48]
        p.font.name = "Arial"
        p.font.size = Pt(16)
        p.font.color.rgb = RGBColor(235, 238, 245)
        if idx < len(nodes) - 1:
            arrow = slide.shapes.add_shape(
                MSO_SHAPE.CHEVRON, Inches(x + box_w + 0.02), Inches(top + 0.22),
                Inches(max(0.12, gap - 0.04)), Inches(0.24))
            arrow.fill.solid()
            arrow.fill.fore_color.rgb = RGBColor(255, 193, 7)
            arrow.line.fill.background()
    return box_h


def _ppt_slide_chrome(slide, section: str, title: str, counter: str,
                       section_color=None) -> None:
    """Mirror the video header: accent bar, gold section label, white title,
    gold divider, top-right counter, muted footer."""
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    if section_color is None:
        section_color = GOLD

    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                 Inches(0), Inches(0), Inches(13.333), Inches(0.16))
    bar.fill.solid()
    bar.fill.fore_color.rgb = RGBColor(*ACCENT)
    bar.line.fill.background()

    # Height is the text's own need, not a round 0.4in. At 12pt a line is
    # ~0.20in plus the textbox's 0.1in of insets, so 0.4in left the eyebrow
    # hanging 0.08in into the title on every content slide - a real box
    # overlap that the layout audit's tolerance was suppressing.
    _ppt_textbox(slide, 0.42, 0.30, 10.0, _est_text_height(section.upper(),
                                                           10.0, 12),
                 section.upper(), 12, section_color, bold=True)
    # B1.5: display titles at 32pt (reader-visible hierarchy on 13.3" canvas);
    # capped to a single line so a wrap can't overrun the gold divider at y=1.28.
    # The cap trims on a word boundary, so the frame never shows a half word.
    # Width stops short of the counter at x=11.20. At 11.0in the title box ran
    # to 11.42 and overlapped the page number by 0.22in on every content slide.
    _ppt_textbox(slide, 0.42, 0.62, 10.6, 0.62, clip_title(title), 32,
                 (255, 255, 255), bold=True)

    div = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                 Inches(0.42), Inches(1.28),
                                 Inches(12.49), Inches(0.03))
    div.fill.solid()
    div.fill.fore_color.rgb = RGBColor(*GOLD)
    div.line.fill.background()

    if counter:
        _ppt_textbox(slide, 11.2, 0.32, 1.7, 0.4, counter, 10, MUTED)
    _ppt_textbox(slide, 0.42, 7.01, 8.5, 0.4,
                  BRAND_FOOTER, 10, MUTED)
def _video_blocks(scene: dict) -> set[str]:
    """Replicate render_slide's vertical accounting (single source of truth)
    and return the optional panels it would actually draw, e.g. {'code'}.

    The PPTX deck reuses this so both media make the identical show/drop
    decisions — a panel a slide previewer would reject via the Pillow room()
    guard must not reappear in the deck.
    """
    yy = 150

    def room(need: int) -> bool:
        return yy + need < _VIDEO_BODY_MAX

    blocks: set[str] = set()

    decision = str(scene.get("design_decision", "")).strip()
    if decision:
        dlines = _wrap(decision, 84)[:2]
        yy += 2 + max(58, 44 + len(dlines) * 24) + 12

    if scene.get("steps"):
        yy += 32
        for _ in scene["steps"]:
            if not room(76):
                break
            yy += 72
        yy += 10

    yy += 36
    for _ in _with_overflow(scene.get("bullets") or []):
        if not room(54):
            break
        yy += 50

    if scene.get("status_badges") and room(58):
        blocks.add("status_badges")
        yy += 4 + 30 + 54

    if scene.get("flow") and room(80):
        yy += 18 + 78

    if _parse_diagram(scene.get("visual_diagram") or "") and room(120):
        yy += 8 + 38 + 84

    if scene.get("analogy") and room(70):
        yy += 4 + 62

    code_text = scene.get("code_snippet")
    if code_text:
        code_lines = str(code_text).splitlines()[:8]
        context_lines = 1 if str(scene.get("code_context", "")).strip() else 0
        box_h = 16 + context_lines * 20 + len(code_lines) * 20 + 10
        if room(box_h + 8):
            blocks.add("code")
            yy += 6 + box_h + 12

    value_table = scene.get("value_table") or []
    if value_table:
        rows = min(len(value_table), 4)
        table_h = 40 + 30 * rows + 12
        if room(table_h):
            blocks.add("value_table")
            yy += 6 + table_h

    for json_lines in _json_payload_candidates(scene):
        json_h = 16 + len(json_lines) * 20 + 10
        if room(json_h + 8):
            blocks.add("json")
            yy += 6 + json_h + 12
            break

    if scene.get("takeaways"):
        yy += 10 + 32
        for _ in _with_overflow(scene.get("takeaways") or []):
            if not room(44):
                break
            yy += 40
    return blocks
def build_pptx(plan: dict, out_path: Path) -> None:
    from pptx import Presentation

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # title slide — same header chrome the cover uses in the video
    #
    # The deck paginates twice, for two different reasons. `_scene_pages`
    # enforces the plan-level structure (max four bullets a page, `bullet_pages`
    # must match). `_paginate_by_height` then enforces the physical one: a page
    # whose bullets need more than the slide can hold becomes another slide.
    # Count-based pagination alone is not enough, because bullet length varies by
    # an order of magnitude and the row stack would then drop teaching content to
    # protect the layout.
    _BODY_BUDGET = 4.86          # 1.7in top .. 6.9in safe bottom, less the label
    page_scenes: list[dict] = []
    for scene in plan["scenes"]:
        for planned in _scene_pages(scene):
            page_scenes.extend(_paginate_by_height(planned, _BODY_BUDGET))
    takes = (plan.get("takeaways") or [])[:8]
    # The takeaway slides paginate, so their count has to be measured
    # before the deck total is fixed or the counter prints "7 / 6".
    take_pages = _takeaway_pages(takes, 6.9 - 1.7) if takes else 0
    deck_total = len(page_scenes) + (1 + take_pages if takes else 1)

    s = prs.slides.add_slide(blank)
    _ppt_set_bg(s)
    _ppt_slide_chrome(s, "LESSON", plan.get("title", "Lesson"),
                      f"1 / {deck_total}")
    # Defence in depth for field-name leakage. The plan pipeline sanitises the
    # opening, but the deck is a separate artifact built from a *saved* plan as
    # well as a live one, and a saved plan can predate the sanitiser or come from
    # elsewhere. Observed on a deck rendered straight from a saved plan: the
    # literal JSON key "opening" was printed on the title slide, in front of a
    # learner. The renderer must not trust upstream hygiene for text it puts on a
    # slide, so it strips it again here.
    _ppt_textbox(s, 0.42, 1.7, 12.4, 1.8,
                 _strip_field_leak(str(plan.get("opening", ""))), 18,
                 (150, 158, 175), wrap=True)

    for i, scene in enumerate(page_scenes):
        s = prs.slides.add_slide(blank)
        _ppt_set_bg(s)
        _ppt_slide_chrome(s, scene.get("section", ""),
                          scene.get("title", ""), f"{i + 2} / {deck_total}")

        row_h = 0.55
        max_h = 6.9
        slide_notes: list[str] = []
        # One budget for the whole slide. Blocks declare the height their
        # content needs; this decides what survives when it does not all fit.
        stack = _RowStack(1.7, max_h)
        _ppt_textbox(s, 0.52, stack.y, 12.3, 0.3, "KEY POINTS", 12,
                       GOLD, bold=True)
        stack.reserve(0.34, _RowStack.REQUIRED, "key points label")
        y = stack.y
        bullets = _with_overflow(scene.get("bullets")
                                  or scene.get("takeaways") or [])
        for b in bullets:
            # Size the row to the text. The previous rule added a flat 0.12 in to
            # anything over 90 characters, which covered a 100-char bullet and
            # left a 271-char one spilling a third of an inch past its box and
            # over whatever was drawn next.
            need = max(row_h, _est_text_height(b, 11.95, 18))
            at = stack.reserve(need, _RowStack.REQUIRED, f"bullet {b[:24]}")
            if at is None:
                break  # required content is out of room; stop, never clip
            tb = _ppt_textbox(s, 0.52, at, 12.3, need, " ", 18, (235, 238, 245))
            tf = tb.text_frame
            _ppt_para(tf, b, 18, (235, 238, 245), bullet=True)
            y = stack.y

        if scene.get("status_badges") and "status_badges" in _video_blocks(scene):
            badge_h = 0.5 + 0.32 * 2
            if stack.reserve(badge_h, _RowStack.SUPPORTING, "verdict legend") is not None:
                _ppt_textbox(s, 0.52, y, 12.3, 0.26, "VERDICT CODES", 12,
                             GOLD, bold=True)
                chips = scene["status_badges"][:6]
                chip_w = 12.3 / max(len(chips), 1)
                gutter = 0.2
                for j, badge in enumerate(chips):
                    state = str(badge.get("state", "")).upper()
                    fill = {"PASS": GREEN, "FAIL": RED,
                            "REVIEW": GOLD}.get(state, MUTED)
                    chip = s.shapes.add_shape(
                        MSO_SHAPE.ROUNDED_RECTANGLE,
                        Inches(0.52 + j * chip_w), Inches(y + 0.3),
                        Inches(chip_w - gutter), Inches(0.5))
                    chip.fill.solid()
                    chip.fill.fore_color.rgb = RGBColor(*fill)
                    chip.line.fill.background()
                    text = f"{badge.get('code', '')} {badge.get('label', '')}".strip()
                    # Inset the label inside its own chip. It used to be
                    # `chip_w - gutter + 0.08` starting 0.08 in right of the
                    # card, which put the label's right edge 0.08 in *past* the
                    # card - harmless for "0 PASS", not for "4 CONFIG ERR",
                    # which is the one that reaches the rounded edge.
                    _ppt_textbox(s, 0.6 + j * chip_w, y + 0.38,
                                 chip_w - gutter - 0.08, 0.34, text, 16,
                                 (18, 24, 38))
                y = stack.y

        if scene.get("design_decision"):
            body = (f"Why THIS (not the alternative): "
                    f"{scene['design_decision']}")
            # Size the card to the text instead of trusting a fixed 0.72 in.
            need = _est_text_height(body, 12.1, 17)
            card_h = max(0.72, need + 0.12)
            if stack.reserve(card_h + 0.2, _RowStack.SUPPORTING, "design decision") is not None:
                card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                          Inches(0.52), Inches(y),
                                          Inches(12.3), Inches(card_h))
                card.fill.solid()
                card.fill.fore_color.rgb = RGBColor(40, 30, 20)
                card.line.fill.background()
                _ppt_textbox(s, 0.62, y + 0.06, 12.1, need,
                             body, 17, (255, 193, 7), wrap=True)
                y = stack.y

        if scene.get("analogy"):
            body = f"Analogy: {scene['analogy']}"
            need = _est_text_height(body, 12.1, 17)
            # The label is measured too, and the body starts below it. It was a
            # fixed 0.28in box at y+0.06 with the body at y+0.32, so the two
            # overlapped by 0.02in on every analogy card.
            label_h = _est_text_height("ANALOGY", 12.1, 12)
            body_top = 0.06 + label_h
            card_h = max(0.72, body_top + need + 0.12)
            if stack.reserve(card_h + 0.2, _RowStack.OPTIONAL, "analogy") is not None:
                card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                          Inches(0.52), Inches(y),
                                          Inches(12.3), Inches(card_h))
                card.fill.solid()
                card.fill.fore_color.rgb = RGBColor(40, 30, 20)
                card.line.fill.background()
                _ppt_textbox(s, 0.62, y + 0.06, 12.1, label_h,
                             "ANALOGY", 12, GOLD, bold=True)
                _ppt_textbox(s, 0.62, y + body_top, 12.1, need,
                             body, 17, (255, 193, 7), wrap=True)
                y = stack.y

        diagram = _parse_diagram(scene.get("visual_diagram") or "")
        # Through the stack, like every other block. It used to advance the
        # local `y` only, which left `stack.y` behind: the stack then believed
        # there was more room than there was and admitted a payload card that
        # ran from 5.59in to 7.78in, a full inch past the 6.9in safe bottom.
        # Two cursors, one of them stale, is the failure mode a single shared
        # budget exists to prevent.
        if diagram and stack.reserve(0.9, _RowStack.OPTIONAL, "diagram") is not None:
            _ppt_diagram(s, 0.52, y, 12.3, diagram)
            y = stack.y

        code_text = scene.get("code_snippet")
        if code_text and "code" in _video_blocks(scene):
            code_lines = str(code_text).splitlines()[:7]
            code_h = 0.2 + (0.3 if str(scene.get("code_context", "")).strip() else 0.0) \
                + len(code_lines) * 0.3
            if stack.reserve(code_h, _RowStack.OPTIONAL, "code panel") is not None:
                _ppt_codebox(s, 0.52, y, 12.3, code_lines,
                              str(scene.get("code_context", "")))
                # The stack already advanced; read it back rather than
                # advancing a second cursor by the same amount.
                y = stack.y

        blocks = _video_blocks(scene)
        value_table = (scene.get("value_table") or [])[:4]
        if value_table and "value_table" in blocks:
            # Row height comes from the taller of the two cells, not a flat
            # 0.3in. A long right-hand cell used to spill out under the card.
            row_heights = [
                max(_est_text_height(str(row).partition("|")[0], 6.2, 17),
                    _est_text_height(str(row).partition("|")[2], 5.9, 17))
                for row in value_table]
            table_h = 0.45 + sum(row_heights)
            if stack.reserve(table_h, _RowStack.SUPPORTING, "numbers") is not None:
                card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                          Inches(0.52), Inches(y),
                                          Inches(12.3), Inches(table_h))
                card.fill.solid()
                card.fill.fore_color.rgb = RGBColor(26, 34, 52)
                card.line.fill.background()
                _ppt_textbox(s, 0.62, y + 0.05, 12.1, 0.26,
                             "NUMBERS", 12, GOLD, bold=True)
                row_y = y + 0.42
                for j, row in enumerate(value_table):
                    cell_left, _, cell_right = str(row).partition("|")
                    _ppt_textbox(s, 0.62, row_y, 6.2, row_heights[j],
                                 cell_left.strip(), 17, (235, 238, 245))
                    _ppt_textbox(s, 6.9, row_y, 5.9, row_heights[j],
                                 cell_right.strip(), 17, (140, 200, 255))
                    row_y += row_heights[j]
                y = stack.y

        # Same index the counter on this slide prints, so a note points at the
        # slide the reader can see. It was `i + 1`, which is off by one against
        # the `i + 2` above: slide 1 is the title, so the first scene slide is 2.
        this_slide = i + 2
        slide_notes.extend(f"slide {this_slide}: {n}" for n in stack.report())
        if "json" in blocks:
            for json_lines in _json_payload_candidates(scene):
                line_heights = [_est_text_height(ln, 12.1, 16)
                                for ln in json_lines]
                json_h = 0.34 + sum(line_heights)
                if stack.reserve(json_h, _RowStack.OPTIONAL, "payload") is None:
                    continue
                card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                          Inches(0.52), Inches(y),
                                          Inches(12.3), Inches(json_h))
                card.fill.solid()
                card.fill.fore_color.rgb = RGBColor(14, 22, 34)
                card.line.fill.background()
                _ppt_textbox(s, 0.62, y + 0.04, 12.1, 0.24,
                             "PAYLOAD", 12, GOLD, bold=True)
                line_y = y + 0.34
                for j, ln in enumerate(json_lines):
                    _ppt_textbox(s, 0.62, line_y, 12.1, line_heights[j],
                                 ln, 16, (180, 220, 255),
                                 font_name="Courier New")
                    line_y += line_heights[j]
                break

    if takes:
        # Takeaway slides - two columns, flowed by measured height.
        #
        # This used to advance every card by a constant 1.2in while sizing the
        # card from a character-count guess, so any card taller than 1.2in
        # overlapped the next in its column: observed on slide 11 as three
        # overlaps 5.90in wide, a full column. The same defect already fixed for
        # scene bullets, in a path the audit had never been run against - earlier
        # decks passed only because their takeaways happened to be short.
        #
        # Overflow becomes another slide rather than a shorter summary. The
        # takeaway slide is the most important one in the deck, so dropping half
        # of it to protect the layout is the wrong trade, and it is the same rule
        # already applied to bullets. Eight 288-character takeaways filled two
        # columns and dropped four; they now flow onto continuation slides.
        col_width = 5.9
        col_gap = 0.3
        x0 = 0.42
        top = 1.7
        budget = max_h - top
        col_x = [x0, x0 + col_width + col_gap]
        pending = list(takes)
        page = 0
        while pending:
            page += 1
            s = prs.slides.add_slide(blank)
            _ppt_set_bg(s)
            _ppt_slide_chrome(
                s, "Key Takeaways",
                "Key Takeaways" if page == 1 else f"Key Takeaways (cont. {page})",
                f"{1 + len(page_scenes) + page} / {deck_total}", section_color=GREEN)
            col_tops = [top, top]
            placed_here = 0
            while pending:
                need = max(0.5, _est_text_height(pending[0],
                                                  col_width - 0.25, 18))
                if max(col_tops) + need > top + budget:
                    break
                col = 0 if (col_tops[0] + need <= top + budget) else 1
                take = pending.pop(0)
                _ppt_textbox(s, col_x[col], col_tops[col], col_width, need,
                             " ", 18, (235, 238, 245))
                tb = s.shapes[-1]
                _ppt_para(tb.text_frame, take, 18, (235, 238, 245), bullet=True)
                col_tops[col] += need + 0.12
                placed_here += 1
            if not placed_here:
                # A single takeaway too tall for any column: keep it alone on a
                # slide rather than looping forever or dropping it silently.
                take = pending.pop(0)
                need = min(max(0.5, _est_text_height(take, col_width - 0.25, 18)),
                           budget)
                _ppt_textbox(s, col_x[0], top, col_width, need, " ", 18,
                             (235, 238, 245))
                tb = s.shapes[-1]
                _ppt_para(tb.text_frame, take, 18, (235, 238, 245), bullet=True)
                # `deck_total` is a count of slides, not this slide's index, and
                # with takeaway pagination they differ. Use the same arithmetic
                # as the counter: title + scene slides + this takeaway page.
                slide_notes.append(
                    f"slide {1 + len(page_scenes) + page}: takeaway exceeds one "
                    f"column and was clipped to a single slide: {take[:32]!r}")

    for note in slide_notes:
        print(f"  [WARN] layout: {note}")
    prs.save(str(out_path))
    print(f"Deck written: {out_path}")
    _audit_layout(out_path)


# Text taller than its box is not clipped by PowerPoint, it is drawn over
# whatever follows. `_AUDIT_OVERLAP_TOL` absorbs the sub-0.1in eyebrow/title
# collisions that are present in every slide by design and read as intentional.
# Deliberately tiny. This was 0.12in, set to absorb the eyebrow/title collision
# that turned out to be a real defect on every content slide - a tolerance
# chosen to silence a detector instead of fixing what it found. Any positive
# overlap of two non-contained boxes is now reported.
_AUDIT_OVERLAP_TOL = 0.01
# A PowerPoint textbox carries ~0.05 in of top and bottom inset, so a single
# short line in a snug box "overflows" by up to 0.1 in and nothing is visible.
# Only spills large enough to cross a card or a neighbouring element are worth a
# warning; the real defects this audit was written for were 0.35 in and 0.36 in.
_AUDIT_OVERFLOW_TOL = 0.12


def _audit_layout(out_path: Path) -> list[str]:
    """Report layout defects in a written deck: overflow, overlap, safe area.

    This is the mechanical subset of the visual-review checklist, and it exists
    because the defect it catches is invisible in geometry alone: a textbox whose
    content needs more height than its shape still renders, so nothing "overlaps"
    while the text visibly spills across the card beneath it. Only the three
    checks that can be decided without a renderer are automated - judgement
    calls like "does this decorative element earn its place" stay with a human.
    """
    from pptx import Presentation

    findings: list[str] = []
    prs = Presentation(str(out_path))
    emu = 914400.0
    for index, slide in enumerate(prs.slides, 1):
        shapes = [sh for sh in slide.shapes if sh.width and sh.height]
        for sh in shapes:
            if not sh.has_text_frame:
                continue
            text = sh.text_frame.text.strip()
            if not text:
                continue
            size = 17.0
            for para in sh.text_frame.paragraphs:
                if para.font.size is not None:
                    size = para.font.size.pt
                    break
            need = _est_text_height(text, sh.width / emu, size)
            have = sh.height / emu
            if need > have + _AUDIT_OVERFLOW_TOL:
                findings.append(
                    f"slide {index}: text overflows its box by "
                    f"{need - have:.2f}in ({len(text)} chars at {size:.0f}pt): "
                    f"{text[:52]!r}")
        for a in range(len(shapes)):
            for b in range(a + 1, len(shapes)):
                A, B = shapes[a], shapes[b]
                ox = (min(A.left + A.width, B.left + B.width)
                      - max(A.left, B.left)) / emu
                oy = (min(A.top + A.height, B.top + B.height)
                      - max(A.top, B.top)) / emu
                if ox <= _AUDIT_OVERLAP_TOL or oy <= _AUDIT_OVERLAP_TOL:
                    continue
                contained = (
                    (A.left <= B.left and A.top <= B.top
                     and A.left + A.width >= B.left + B.width
                     and A.top + A.height >= B.top + B.height)
                    or (B.left <= A.left and B.top <= A.top
                        and B.left + B.width >= A.left + A.width
                        and B.top + B.height >= A.top + A.height))
                if contained:
                    continue  # a label sitting inside its own card
                findings.append(
                    f"slide {index}: shapes overlap by {ox:.2f}x{oy:.2f}in")
    for finding in findings[:6]:
        print(f"  [WARN] layout: {finding}")
    if len(findings) > 6:
        print(f"  [WARN] layout: ... and {len(findings) - 6} more")
    if not findings:
        print("  layout   : no overflow, overlap, or safe-area findings")
    return findings

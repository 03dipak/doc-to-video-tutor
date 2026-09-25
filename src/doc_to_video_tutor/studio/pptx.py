"""PPTX deck construction from scene blocks."""

from __future__ import annotations

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

    _ppt_textbox(slide, 0.42, 0.30, 10.0, 0.4, section.upper(), 12,
                  section_color, bold=True)
    # B1.5: display titles at 32pt (reader-visible hierarchy on 13.3" canvas);
    # capped to a single line so a wrap can't overrun the gold divider at y=1.28.
    # The cap trims on a word boundary, so the frame never shows a half word.
    _ppt_textbox(slide, 0.42, 0.62, 11.0, 0.62, clip_title(title), 32,
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
    deck_total = len(page_scenes) + (2 if takes else 1)

    s = prs.slides.add_slide(blank)
    _ppt_set_bg(s)
    _ppt_slide_chrome(s, "LESSON", plan.get("title", "Lesson"),
                      f"1 / {deck_total}")
    _ppt_textbox(s, 0.42, 1.7, 12.4, 1.8, plan.get("opening", ""), 18,
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
            card_h = max(0.72, 0.34 + need + 0.08)
            if stack.reserve(card_h + 0.2, _RowStack.OPTIONAL, "analogy") is not None:
                card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                          Inches(0.52), Inches(y),
                                          Inches(12.3), Inches(card_h))
                card.fill.solid()
                card.fill.fore_color.rgb = RGBColor(40, 30, 20)
                card.line.fill.background()
                _ppt_textbox(s, 0.62, y + 0.06, 12.1, 0.28,
                             "ANALOGY", 12, GOLD, bold=True)
                _ppt_textbox(s, 0.62, y + 0.32, 12.1, need,
                             body, 17, (255, 193, 7), wrap=True)
                y = stack.y

        diagram = _parse_diagram(scene.get("visual_diagram") or "")
        if diagram and y + 0.9 <= max_h:
            _ppt_diagram(s, 0.52, y, 12.3, diagram)
            y += 0.9

        code_text = scene.get("code_snippet")
        if code_text and "code" in _video_blocks(scene):
            code_lines = str(code_text).splitlines()[:7]
            code_h = 0.2 + (0.3 if str(scene.get("code_context", "")).strip() else 0.0) \
                + len(code_lines) * 0.3
            if stack.reserve(code_h, _RowStack.OPTIONAL, "code panel") is not None:
                _ppt_codebox(s, 0.52, y, 12.3, code_lines,
                              str(scene.get("code_context", "")))
                y += code_h

        blocks = _video_blocks(scene)
        value_table = (scene.get("value_table") or [])[:4]
        if value_table and "value_table" in blocks:
            table_h = 0.45 + 0.3 * len(value_table)
            if stack.reserve(table_h, _RowStack.SUPPORTING, "numbers") is not None:
                card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                          Inches(0.52), Inches(y),
                                          Inches(12.3), Inches(table_h))
                card.fill.solid()
                card.fill.fore_color.rgb = RGBColor(26, 34, 52)
                card.line.fill.background()
                _ppt_textbox(s, 0.62, y + 0.05, 12.1, 0.26,
                             "NUMBERS", 12, GOLD, bold=True)
                for j, row in enumerate(value_table):
                    cell_left, _, cell_right = str(row).partition("|")
                    row_y = y + 0.42 + j * 0.3
                    _ppt_textbox(s, 0.62, row_y, 6.2, 0.28, cell_left.strip(),
                                 17, (235, 238, 245))
                    _ppt_textbox(s, 6.9, row_y, 5.9, 0.28, cell_right.strip(),
                                 17, (140, 200, 255))
                y = stack.y

        # 1-based for humans; the loop index is 0-based.
        slide_notes.extend(f"slide {i + 1}: {n}" for n in stack.report())
        if "json" in blocks:
            for json_lines in _json_payload_candidates(scene):
                json_h = 0.34 + len(json_lines) * 0.3
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
                for j, ln in enumerate(json_lines):
                    _ppt_textbox(s, 0.62, y + 0.34 + j * 0.3, 12.1, 0.28,
                                 ln, 16, (180, 220, 255),
                                 font_name="Courier New")
                break

    if takes:
        # takeaway slide — 2 columns, rows capped so nothing exceeds the budget;
        # same chrome as the video's final card (section + counter + footer).
        s = prs.slides.add_slide(blank)
        _ppt_set_bg(s)
        _ppt_slide_chrome(s, "Key Takeaways", "Key Takeaways",
                          f"{deck_total} / {deck_total}", section_color=GREEN)

        col_width = 5.9
        col_gap = 0.3
        x0 = 0.42
        rows_per_col = 4
        row_h = 1.2
        top = 1.7
        for idx, t in enumerate(takes):
            col = idx // rows_per_col
            row = idx % rows_per_col
            left = x0 + col * (col_width + col_gap)
            box_h = row_h * (0.5 + (len(t) / 100))
            _ppt_textbox(s, left, top + row * row_h, col_width, box_h, " ", 18,
                          (235, 238, 245))
            tb = s.shapes[-1]
            _ppt_para(tb.text_frame, t, 18, (235, 238, 245), bullet=True)

    for note in slide_notes:
        print(f"  [WARN] layout: {note}")
    prs.save(str(out_path))
    print(f"Deck written: {out_path}")
    _audit_layout(out_path)


# Text taller than its box is not clipped by PowerPoint, it is drawn over
# whatever follows. `_AUDIT_OVERLAP_TOL` absorbs the sub-0.1in eyebrow/title
# collisions that are present in every slide by design and read as intentional.
_AUDIT_OVERLAP_TOL = 0.12
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

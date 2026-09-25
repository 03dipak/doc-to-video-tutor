"""PPTX deck construction from scene blocks."""

from __future__ import annotations

from pathlib import Path

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches

from .config import _VIDEO_BODY_MAX, ACCENT, BRAND_FOOTER, GOLD, GREEN, MUTED
from .slides import _parse_diagram, _scene_pages, _with_overflow, _wrap


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
def _ppt_para(tf, text: str, size, color, bullet: bool = False, bold: bool = False):
    """Add a wrapped paragraph to a text frame with styling."""
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    p = tf.add_paragraph()
    p.text = text
    p.font.size = Pt(size)
    p.font.bold = bold
    p.font.color.rgb = RGBColor(*color)
    if bullet:
        p.font.color.rgb = RGBColor(*color)
        _ppt_bullet(p)
    return p
def _ppt_textbox(slide, left, top, width, height, text, size, color,
                 bold=False, wrap=True):
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    tb = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.margin_left = Inches(0.1)
    p = tf.paragraphs[0]
    p.text = text
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
    _ppt_textbox(slide, 0.42, 0.62, 11.0, 0.62, str(title)[:44], 32,
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

    # title slide — same header chrome the cover card uses in the video
    page_scenes: list[dict] = []
    for scene in plan["scenes"]:
        page_scenes.extend(_scene_pages(scene))
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
        y = 1.7
        max_h = 6.9
        _ppt_textbox(s, 0.52, y, 12.3, 0.3, "KEY POINTS", 12,
                       GOLD, bold=True)
        y += 0.34
        bullets = _with_overflow(scene.get("bullets")
                                  or scene.get("takeaways") or [])
        for b in bullets:
            need = row_h + (0.12 if len(b) > 90 else 0)
            if y + need > max_h:
                break  # mirror the video's vertical budget: stop, don't overlap
            tb = _ppt_textbox(s, 0.52, y, 12.3, need, " ", 18, (235, 238, 245))
            tf = tb.text_frame
            _ppt_para(tf, b, 18, (235, 238, 245), bullet=True)
            y += need

        if scene.get("design_decision") and y + 0.92 <= max_h:
            card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                         Inches(0.52), Inches(y),
                                         Inches(12.3), Inches(0.72))
            card.fill.solid()
            card.fill.fore_color.rgb = RGBColor(40, 30, 20)
            card.line.fill.background()
            _ppt_textbox(s, 0.62, y + 0.06, 12.1, 0.6,
                         f"Why THIS (not the alternative): {scene['design_decision']}",
                         17, (255, 193, 7), wrap=True)
            y += 0.92

        if scene.get("analogy") and y + 0.92 <= max_h:
            card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                         Inches(0.52), Inches(y),
                                         Inches(12.3), Inches(0.72))
            card.fill.solid()
            card.fill.fore_color.rgb = RGBColor(40, 30, 20)
            card.line.fill.background()
            _ppt_textbox(s, 0.62, y + 0.06, 12.1, 0.28,
                         "ANALOGY", 12, GOLD, bold=True)
            _ppt_textbox(s, 0.62, y + 0.32, 12.1, 0.5,
                         f"Analogy: {scene['analogy']}", 17,
                         (255, 193, 7), wrap=True)
            y += 0.92

        diagram = _parse_diagram(scene.get("visual_diagram") or "")
        if diagram and y + 0.9 <= max_h:
            _ppt_diagram(s, 0.52, y, 12.3, diagram)
            y += 0.9

        code_text = scene.get("code_snippet")
        if code_text and "code" in _video_blocks(scene):
            code_lines = str(code_text).splitlines()[:7]
            code_h = 0.2 + (0.3 if str(scene.get("code_context", "")).strip() else 0.0) \
                + len(code_lines) * 0.3
            if y + code_h <= max_h:
                _ppt_codebox(s, 0.52, y, 12.3, code_lines,
                              str(scene.get("code_context", "")))

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

    prs.save(str(out_path))
    print(f"Deck written: {out_path}")

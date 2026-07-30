from pathlib import Path
import re

from docx import Document
from docx.shared import Pt, RGBColor
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


# ─── Inline helpers ──────────────────────────────────────────────────────────

def _strip_html_comments(text: str) -> str:
    """Remove <!-- ... --> comment blocks (multi-line aware)."""
    return re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL).strip()


def _add_inline_runs(paragraph, text: str):
    """Parse a line of Markdown inline markup (bold, italic, inline code, links)
    and add it to *paragraph* as styled runs.

    Handles:
      **bold**   __bold__
      *italic*   _italic_
      ***bold-italic***
      `inline code`
      [label](url)  → "label (url)"
    """
    # Pattern captures: *** bold-italic ***, **bold**, *italic*, `code`, [label](url)
    pattern = re.compile(
        r'(\*\*\*(.+?)\*\*\*'      # ***bold-italic***
        r'|\*\*(.+?)\*\*'          # **bold**
        r'|__(.+?)__'              # __bold__
        r'|\*(.+?)\*'              # *italic*
        r'|_(.+?)_'                # _italic_
        r'|`(.+?)`'                # `inline code`
        r'|\[([^\]]+)\]\([^)]+\)'  # [label](url) - keep just label
        r')',
        re.DOTALL
    )

    pos = 0
    for m in pattern.finditer(text):
        # Plain text before this match
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])

        full = m.group(0)
        if full.startswith('***'):
            run = paragraph.add_run(m.group(2))
            run.bold = True
            run.italic = True
        elif full.startswith('**') or full.startswith('__'):
            run = paragraph.add_run(m.group(3) or m.group(4))
            run.bold = True
        elif full.startswith('*') or full.startswith('_'):
            run = paragraph.add_run(m.group(5) or m.group(6))
            run.italic = True
        elif full.startswith('`'):
            run = paragraph.add_run(m.group(7))
            run.font.name = 'Consolas'
            run.font.size = Pt(9.5)
        elif full.startswith('['):
            # Link: just keep the label text
            run = paragraph.add_run(m.group(8))
            run.underline = True

        pos = m.end()

    # Remaining plain text
    if pos < len(text):
        paragraph.add_run(text[pos:])


def _add_code_block(doc, lines):
    """Add a monospaced code block paragraph."""
    paragraph = doc.add_paragraph()
    run = paragraph.add_run("\n".join(lines))
    run.font.name = "Consolas"
    run.font.size = Pt(9)
    # Light grey shading for the paragraph
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), 'F2F2F2')
    pPr.append(shd)


def _add_blockquote(doc, text: str):
    """Render a Markdown blockquote (> text) as an indented italic paragraph."""
    stripped = re.sub(r'^>\s*', '', text).strip()
    # Strip inline MD from the blockquote text too
    para = doc.add_paragraph()
    para.paragraph_format.left_indent = Pt(18)
    run = para.add_run(re.sub(r'\*\*|__|\*|_|`', '', stripped))
    run.italic = True


def _add_horizontal_rule(doc):
    """Add a thin horizontal line."""
    para = doc.add_paragraph()
    pPr = para._p.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '6')
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), 'AAAAAA')
    pBdr.append(bottom)
    pPr.append(pBdr)


def _parse_table_rows(lines):
    rows = []
    for line in lines:
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        rows.append(parts)
    return rows


# ─── Core converter ──────────────────────────────────────────────────────────

def _convert_content_to_doc(doc: Document, content: str, title: str = None):
    """Parse *content* (Markdown string) and populate *doc* (python-docx Document)."""
    content = _strip_html_comments(content)
    if title:
        doc.add_heading(title, level=0)

    lines = content.splitlines()
    i = 0
    in_code = False
    code_lang = ""
    code_lines = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Fenced code block ────────────────────────────────────────────────
        if stripped.startswith("```"):
            if in_code:
                _add_code_block(doc, code_lines)
                code_lines = []
                in_code = False
                code_lang = ""
            else:
                in_code = True
                code_lang = stripped[3:].strip()
            i += 1
            continue

        if in_code:
            code_lines.append(line)
            i += 1
            continue

        # ── Skip blank/comment-only lines ────────────────────────────────────
        if not stripped:
            doc.add_paragraph("")
            i += 1
            continue

        # ── Headings ─────────────────────────────────────────────────────────
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            level = min(hashes, 4)
            text = stripped[hashes:].strip()
            doc.add_heading(text, level=level)
            i += 1
            continue

        # ── Horizontal rule ──────────────────────────────────────────────────
        if re.match(r'^[-*_]{3,}$', stripped):
            _add_horizontal_rule(doc)
            i += 1
            continue

        # ── Blockquote ───────────────────────────────────────────────────────
        if stripped.startswith("> ") or stripped == ">":
            _add_blockquote(doc, stripped)
            i += 1
            continue

        # ── Bullet list ──────────────────────────────────────────────────────
        if re.match(r'^[-*+] ', stripped):
            text = re.sub(r'^[-*+] ', '', stripped)
            para = doc.add_paragraph(style="List Bullet")
            _add_inline_runs(para, text)
            i += 1
            continue

        # ── Numbered list ────────────────────────────────────────────────────
        num_match = re.match(r'^(\d+)\.\s+', stripped)
        if num_match:
            text = stripped[num_match.end():]
            para = doc.add_paragraph(style="List Number")
            _add_inline_runs(para, text)
            i += 1
            continue

        # ── Table ────────────────────────────────────────────────────────────
        if "|" in stripped and i + 1 < len(lines):
            next_stripped = lines[i + 1].strip()
            if re.match(r'^[\s|:\-]+$', next_stripped) and "|" in next_stripped:
                table_lines = [line]
                i += 2  # skip separator row
                while i < len(lines) and "|" in lines[i]:
                    table_lines.append(lines[i])
                    i += 1
                rows = _parse_table_rows(table_lines)
                if rows:
                    col_count = max(len(r) for r in rows)
                    table = doc.add_table(rows=len(rows), cols=col_count)
                    table.style = "Table Grid"
                    for r_idx, row in enumerate(rows):
                        for c_idx in range(col_count):
                            cell = table.cell(r_idx, c_idx)
                            value = row[c_idx] if c_idx < len(row) else ""
                            # Bold the header row
                            if r_idx == 0:
                                run = cell.paragraphs[0].add_run(value)
                                run.bold = True
                            else:
                                _add_inline_runs(cell.paragraphs[0], value)
                continue

        # ── Regular paragraph ────────────────────────────────────────────────
        para = doc.add_paragraph()
        _add_inline_runs(para, stripped)
        i += 1

    if in_code and code_lines:
        _add_code_block(doc, code_lines)

    return doc


# ─── Public API ──────────────────────────────────────────────────────────────

def convert_markdown_to_docx(md_path, docx_path):
    md_path = Path(md_path)
    docx_path = Path(docx_path)
    try:
        content = md_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    doc = Document()
    _convert_content_to_doc(doc, content)
    try:
        docx_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(docx_path))
        return True
    except Exception:
        return False


def create_docx_directly(content, docx_path, title=None):
    """
    Create a DOCX file directly from a Markdown content string.

    Supports: headings, bold/italic/inline-code inline markup, bullet lists,
    numbered lists, tables (with bold header row), fenced code blocks (grey
    background, Consolas font), blockquotes (indented italic), horizontal rules,
    and HTML comment stripping.

    Args:
        content: Markdown-formatted string.
        docx_path: Path where the .docx file will be saved.
        title: Optional document title added as a Heading 0.

    Returns:
        True if the file was saved successfully, False otherwise.
    """
    docx_path = Path(docx_path)
    if not content or not content.strip():
        return False
    doc = Document()
    _convert_content_to_doc(doc, content, title=title)
    try:
        docx_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(docx_path))
        return True
    except Exception:
        return False


def convert_markdown_tree(root_dir, exclude_names=None):
    root_dir = Path(root_dir)
    exclude = {name.lower() for name in (exclude_names or [])}
    generated = []
    for md_file in root_dir.rglob("*.md"):
        if md_file.name.lower() in exclude:
            continue
        docx_path = md_file.with_suffix(".docx")
        if convert_markdown_to_docx(md_file, docx_path):
            generated.append(str(docx_path))
    return generated

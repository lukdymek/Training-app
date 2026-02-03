from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _make_run(text: str) -> etree._Element:
    r = etree.Element(_w("r"))
    t = etree.SubElement(r, _w("t"))
    if text.startswith(" ") or text.endswith(" "):
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    return r


def _replace_bookmark_range(root: etree._Element, bookmark_name: str, value: str) -> bool:
    """
    Replace content inside bookmark range with `value`.
    Returns True if anything changed.
    """
    changed = False
    value = "" if value is None else str(value)

    starts = root.xpath(f'.//w:bookmarkStart[@w:name="{bookmark_name}"]', namespaces=NS)
    if not starts:
        return False

    for start in starts:
        bm_id = start.get(_w("id"))
        if bm_id is None:
            continue

        ends = root.xpath(f'.//w:bookmarkEnd[@w:id="{bm_id}"]', namespaces=NS)
        if not ends:
            continue
        end = ends[0]

        start_parent = start.getparent()
        end_parent = end.getparent()

        # Common case: same parent -> remove siblings between them
        if start_parent is not None and start_parent is end_parent:
            parent = start_parent
            children = list(parent)

            try:
                i_start = children.index(start)
                i_end = children.index(end)
            except ValueError:
                continue

            if i_end < i_start:
                continue

            # Remove everything between start and end
            for _ in range(i_end - i_start - 1):
                del parent[i_start + 1]

            # Insert a run with text right after bookmarkStart
            parent.insert(i_start + 1, _make_run(value))
            changed = True
            continue

        # Fallback (rare): start/end not same parent.
        # We do a minimal safe update: put the value into the first w:t inside the same paragraph (if any).
        para = start.xpath("ancestor::w:p[1]", namespaces=NS)
        if para:
            p = para[0]
            # find text nodes between start and end inside this paragraph
            # If none, just append a run into paragraph
            t_nodes = p.xpath('.//w:t', namespaces=NS)
            if t_nodes:
                t_nodes[0].text = value
                changed = True
            else:
                p.append(_make_run(value))
                changed = True

    return changed


def fill_bookmarks(docx_bytes: bytes, values: dict[str, str]) -> bytes:
    """
    Fill bookmarks across all word/*.xml parts of a DOCX safely by replacing
    the bookmark RANGE, not a 'following text' guess.
    """
    in_mem = BytesIO(docx_bytes)

    with ZipFile(in_mem, "r") as zin:
        names = zin.namelist()

        out_mem = BytesIO()
        with ZipFile(out_mem, "w", compression=ZIP_DEFLATED) as zout:
            for name in names:
                data = zin.read(name)

                if name.startswith("word/") and name.endswith(".xml"):
                    try:
                        root = etree.fromstring(data)
                        any_changed = False

                        for k, v in values.items():
                            if _replace_bookmark_range(root, k, "" if v is None else str(v)):
                                any_changed = True

                        if any_changed:
                            data = etree.tostring(
                                root,
                                xml_declaration=True,
                                encoding="UTF-8"
                            )
                    except Exception:
                        # If parsing fails, keep the original part unchanged
                        pass

                zout.writestr(name, data)

        return out_mem.getvalue()

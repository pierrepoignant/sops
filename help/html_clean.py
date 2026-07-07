"""Clean article HTML pasted or produced by rich-text editors (Word, Quill…)
into the plain semantic HTML the article page styles.

Transforms are mechanical and idempotent:
- drop Quill UI artifacts (<span class="ql-ui">) and contenteditable attrs
- rebuild Quill lists (<ol><li data-list="bullet">) as real <ul>/<ol>,
  nesting ql-indent-1 items one level down
- strip style/class/data-*/on* attributes (href, src, alt, title, colspan,
  rowspan and friends survive)
- unwrap <span> left without attributes and <strong>/<u>/<b> wrapping an
  entire heading (headings are already bold)
- remove empty spacer blocks (<p><br></p>, <h3><br></h3>, …) unless they
  hold an image, table or embed
"""
import re

from bs4 import BeautifulSoup, NavigableString, Tag

HEADINGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')

# Attributes worth keeping; everything else (style, class, data-*, on*,
# contenteditable…) is editor noise on article HTML.
_KEEP_ATTRS = {'href', 'src', 'alt', 'title', 'target', 'rel', 'colspan',
               'rowspan', 'id', 'name', 'width', 'height'}


def _rebuild_quill_lists(soup):
    for lst in soup.find_all(['ol', 'ul']):
        items = lst.find_all('li', recursive=False)
        if not items or not any(li.has_attr('data-list') for li in items):
            continue
        new_lists, cur, cur_type, nest = [], None, None, None
        for li in items:
            typ = 'ul' if li.get('data-list') == 'bullet' else 'ol'
            indented = any(c.startswith('ql-indent') for c in li.get('class') or [])
            if indented and cur is not None:
                if nest is None:
                    nest = soup.new_tag(typ)
                    cur.contents[-1].append(nest)
                nest.append(li.extract())
                continue
            nest = None
            if cur is None or typ != cur_type:
                cur = soup.new_tag(typ)
                cur_type = typ
                new_lists.append(cur)
            cur.append(li.extract())
        for nl in reversed(new_lists):
            lst.insert_after(nl)
        lst.decompose()


def clean_article_html(html):
    """Return a cleaned version of ``html``; safe to run repeatedly."""
    if not html or not html.strip():
        return ''
    soup = BeautifulSoup(html, 'html.parser')

    for span in soup.find_all('span', class_='ql-ui'):
        span.decompose()

    _rebuild_quill_lists(soup)

    for h in soup.find_all(HEADINGS):
        for tag in h.find_all(['strong', 'b', 'u']):
            tag.unwrap()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr not in _KEEP_ATTRS:
                del tag.attrs[attr]

    for span in soup.find_all('span'):
        if not span.attrs:
            span.unwrap()

    for block in soup.find_all(['p', *HEADINGS]):
        if not block.get_text(strip=True) \
                and not block.find(['img', 'table', 'iframe', 'video']):
            block.decompose()

    out = str(soup)
    out = re.sub(r'\s+</(p|li|h[1-6])>', r'</\1>', out)
    out = re.sub(r'</(p|ul|ol|h[1-6]|table)>\s*', r'</\1>\n', out)
    return out.strip()

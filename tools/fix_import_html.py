"""Post-import HTML cleanup for the SOPs imported by tools/import_sops.py.

Fixes Word/mammoth conversion artifacts in the imported articles' body_html:

  - heading hierarchy: shift levels so each article's top body heading is h2
    (the page renders the article title above the body), keeping relative depth
  - remove empty headings and a leading heading that duplicates the title
  - normalize whitespace (tabs, runs of spaces) inside headings
  - merge adjacent same-type sibling lists (Word exports one <ul> per item)
  - turn runs of >= 2 paragraphs that start with a literal bullet (•, o, -, …)
    or a 1./2./3. sequence into real <ul>/<ol>
  - one known text fix: 'elevé de factures' -> 'Relevé de factures'

Also refreshes search_text and rewrites the v1 SopVersion snapshot (content is
semantically the same document, so this is a correction, not a new version).

Usage:
    .venv/bin/python tools/fix_import_html.py --db sqlite --dry-run
    .venv/bin/python tools/fix_import_html.py --db ovh
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BRAND = 'essenciagua'
HEADS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']

BULLET_RE = re.compile(r'^\s*([•·▪‣◦]|o(?=[\s ])|-(?=\s))[\s ]*')
NUM_RE = re.compile(r'^\s*(\d{1,2})[\.\)][\s ]+')

TEXT_FIXES = {
    'elevé de factures': 'Relevé de factures',
}


def norm(s):
    return re.sub(r'[\s ]+', ' ', s or '').strip().lower()


def fix_article(soup, title, stats):
    from bs4 import NavigableString

    # 0a. Word table-of-contents blocks: a p/li made only of internal #_Toc
    # links (plus page numbers/dots) is TOC junk — drop it. Cross-reference
    # links inside real sentences are unwrapped instead (the anchors go away).
    for a in soup.find_all('a', href=re.compile(r'^#_Toc')):
        block = a.find_parent(['p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        if block is None:
            a.unwrap()
            stats['toc'] += 1
            continue
        links_text = ''.join(x.get_text() for x in block.find_all(
            'a', href=re.compile(r'^#_Toc')))
        rest = block.get_text().replace(links_text, '')
        if not re.sub(r'[\s\d\.·…\t ]+', '', rest):
            parent_list = block.parent if block.name == 'li' else None
            block.decompose()
            if parent_list is not None and not parent_list.find_all('li'):
                parent_list.decompose()
            stats['toc'] += 1
        else:
            a.unwrap()
            stats['toc'] += 1
    # A now-orphaned "Table des matières" label above the removed TOC.
    for el in soup.find_all(['p'] + HEADS):
        if norm(el.get_text(' ', strip=True)) in ('table des matières',
                                                  'table des matieres',
                                                  'sommaire'):
            el.decompose()
            stats['toc'] += 1

    # 0. Word bookmark/TOC anchors: drop empty ones, unwrap the rest.
    for a in soup.find_all('a'):
        if a.get('href'):
            continue
        if a.get_text(strip=True) or a.find('img'):
            a.unwrap()
        else:
            a.decompose()
        stats['anchors'] += 1

    # 1. Empty headings out.
    for h in soup.find_all(HEADS):
        if not h.get_text(strip=True) and not h.find('img'):
            h.decompose()
            stats['empty_heads'] += 1

    # 2. Leading heading that repeats the article title.
    first_el = next((n for n in soup.children if getattr(n, 'name', None)), None)
    if first_el is not None and first_el.name in HEADS \
            and norm(first_el.get_text(' ', strip=True)) == norm(title):
        first_el.decompose()
        stats['dup_title'] += 1

    # 3. Known text fixes + heading whitespace normalization.
    for h in soup.find_all(HEADS):
        text = h.get_text(' ', strip=True)
        fixed = TEXT_FIXES.get(text.strip(), None)
        clean = re.sub(r'[\s \t]+', ' ', fixed or text).strip()
        if clean != text:
            imgs = h.find_all('img')
            h.clear()
            h.append(NavigableString(clean))
            for img in imgs:
                h.append(img)
            stats['head_text'] += 1

    # 4. Shift heading levels so the top level used in this body is h2.
    levels = sorted({int(h.name[1]) for h in soup.find_all(HEADS)})
    if levels and levels[0] != 2:
        delta = 2 - levels[0]
        for h in soup.find_all(HEADS):
            h.name = f'h{min(6, max(2, int(h.name[1]) + delta))}'
        stats['level_shift'] += 1

    # 5. Merge adjacent same-type sibling lists (whitespace-only gaps).
    changed = True
    while changed:
        changed = False
        for lst in soup.find_all(['ul', 'ol']):
            nxt = lst.next_sibling
            while isinstance(nxt, NavigableString) and not str(nxt).strip():
                nxt = nxt.next_sibling
            if getattr(nxt, 'name', None) == lst.name:
                for li in list(nxt.find_all('li', recursive=False)):
                    lst.append(li)
                nxt.decompose()
                stats['lists_merged'] += 1
                changed = True
                break

    # 6. Runs of fake-bullet / manually-numbered paragraphs -> real lists.
    def para_kind(p):
        if getattr(p, 'name', None) != 'p':
            return None
        text = p.get_text()
        if BULLET_RE.match(text):
            return 'ul'
        m = NUM_RE.match(text)
        if m:
            return ('ol', int(m.group(1)))
        return None

    body_children = [n for n in soup.children]
    i = 0
    while i < len(body_children):
        kind = para_kind(body_children[i])
        if not kind:
            i += 1
            continue
        run = [body_children[i]]
        j = i + 1
        while j < len(body_children):
            n = body_children[j]
            if isinstance(n, NavigableString) and not str(n).strip():
                j += 1
                continue
            k = para_kind(n)
            if not k:
                break
            same_ul = kind == 'ul' and k == 'ul'
            seq_ol = (isinstance(kind, tuple) and isinstance(k, tuple)
                      and k[1] == kind[1] + len(run))
            if not (same_ul or seq_ol):
                break
            run.append(n)
            j += 1
        is_ol = isinstance(kind, tuple)
        converted = len(run) >= 2 and (not is_ol or kind[1] == 1)
        if converted:
            new_list = soup.new_tag('ol' if is_ol else 'ul')
            run[0].insert_before(new_list)
            for p in run:
                strip_re = NUM_RE if is_ol else BULLET_RE
                # Strip the literal marker from the paragraph's first text node.
                for t in p.find_all(string=True):
                    if str(t).strip():
                        t.replace_with(strip_re.sub('', str(t), count=1))
                        break
                li = soup.new_tag('li')
                for child in list(p.children):
                    li.append(child.extract())
                new_list.append(li)
                p.decompose()
            stats['fake_lists'] += 1
            body_children = [n for n in soup.children]  # container changed
            i = 0
        else:
            i = j if len(run) >= 2 else i + 1

    return soup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='ovh')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--slug', help='only this article (for testing)')
    args = ap.parse_args()

    from bs4 import BeautifulSoup
    from __init__ import create_app
    from init_db import db

    app = create_app(db_name=args.db)
    with app.app_context():
        from help.models import HelpArticle, SopVersion
        from help.search import html_to_text

        q = HelpArticle.query.filter_by(brand=BRAND)
        if args.slug:
            q = q.filter_by(slug=args.slug)
        arts = q.order_by(HelpArticle.department, HelpArticle.sort_order).all()

        totals = {}
        for art in arts:
            stats = {k: 0 for k in ('toc', 'anchors', 'empty_heads', 'dup_title',
                                    'head_text', 'level_shift', 'lists_merged',
                                    'fake_lists')}
            soup = fix_article(BeautifulSoup(art.body_html, 'html.parser'),
                               art.title, stats)
            if not any(stats.values()):
                continue
            print(f"{art.slug[:80]}: " +
                  ', '.join(f'{k}={v}' for k, v in stats.items() if v))
            for k, v in stats.items():
                totals[k] = totals.get(k, 0) + v
            if args.dry_run:
                continue
            art.body_html = str(soup)
            art.search_text = re.sub(
                r'\s+', ' ',
                f'{art.title} {html_to_text(art.body_html)}').strip()[:60000]
            v1 = SopVersion.query.filter_by(article_id=art.id, version_no=1).first()
            if v1:
                v1.body_html = art.body_html
            db.session.commit()

        print(f"\n{'DRY RUN — ' if args.dry_run else ''}Totaux: " +
              (', '.join(f'{k}={v}' for k, v in totals.items()) or 'rien à corriger'))


if __name__ == '__main__':
    main()

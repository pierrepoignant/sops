"""One-shot importer: department folders of Word SOPs -> the SOP platform.

Source layout (one department per first-level folder, SOPs in processus/):

    <SOURCE>/<Département>/processus/*.docx

Each .docx becomes one published SOP in the department. Very large documents
(long text with several top-level headings) are split into sub-SOPs: an L1
category named after the document is created and each section becomes its own
article in it. Embedded images are uploaded to S3 and registered as brand
media assets (deduplicated by content hash).

Usage (from the repo root, with the project venv — mammoth required):

    .venv/bin/python tools/import_sops.py --source ~/Downloads/SOPs --dry-run
    .venv/bin/python tools/import_sops.py --source ~/Downloads/SOPs --db ovh

Idempotent: a document whose article slug already exists is skipped, so the
import can be re-run after a partial failure without duplicating anything.
"""
import argparse
import hashlib
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BRAND = 'essenciagua'

# Split a document into sub-SOPs when its plain text is longer than this AND
# it has at least SPLIT_MIN_SECTIONS top-level headings to split at.
SPLIT_MIN_CHARS = 12000
SPLIT_MIN_SECTIONS = 3
# A section shorter than this is not worth a standalone sub-SOP: it is merged
# into the neighboring section (its heading is kept inside the body).
MIN_SECTION_CHARS = 700

EXT_BY_TYPE = {
    'image/png': 'png', 'image/jpeg': 'jpeg', 'image/jpg': 'jpeg',
    'image/gif': 'gif', 'image/bmp': 'bmp', 'image/x-emf': 'emf',
    'image/webp': 'webp', 'image/tiff': 'tiff',
}

DEPT_ICONS = {
    'commande': 'fa-box-open',
    'conditionnement': 'fa-fill-drip',
    'controle-qualite': 'fa-clipboard-check',
    'distillation': 'fa-vial',
    'hygiene-et-securite': 'fa-broom',
    'mmq': 'fa-book',
    'non-conformites': 'fa-exclamation-triangle',
    'reception-et-stockage': 'fa-warehouse',
}


def slugify(value, fallback='item'):
    value = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^a-zA-Z0-9]+', '-', value).strip('-').lower()
    return value or fallback


def doc_title(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r'\s+', ' ', stem.replace('_', ' ')).strip()


def scan_source(source):
    """[(dept_name, [docx paths])] — first-level folders with a processus/."""
    out = []
    for name in sorted(os.listdir(source), key=str.casefold):
        dpath = os.path.join(source, name)
        if name.startswith('.') or not os.path.isdir(dpath):
            continue
        proc = next((os.path.join(dpath, s) for s in os.listdir(dpath)
                     if s.lower() == 'processus'
                     and os.path.isdir(os.path.join(dpath, s))), None)
        if not proc:
            print(f'!! {name}: no processus/ folder — skipped')
            continue
        docs = sorted(
            (os.path.join(proc, f) for f in os.listdir(proc)
             if f.lower().endswith('.docx') and not f.startswith('~$')),
            key=str.casefold)
        out.append((name, docs))
    return out


def convert_docx(path):
    """(soup, images) — images is {cid: (bytes, content_type, alt)}; <img>
    tags carry src='cid:<cid>' until articles are known."""
    import mammoth
    from bs4 import BeautifulSoup

    images = {}

    def convert_image(image):
        with image.open() as b:
            data = b.read()
        cid = f'i{len(images)}'
        images[cid] = (data, (image.content_type or 'application/octet-stream').lower(),
                       getattr(image, 'alt_text', None))
        return {'src': f'cid:{cid}', 'class': 'help-img'}

    with open(path, 'rb') as f:
        result = mammoth.convert_to_html(
            f, convert_image=mammoth.images.img_element(convert_image))
    return BeautifulSoup(result.value, 'html.parser'), images


def split_plan(soup, title):
    """[(section_title, [nodes])] — one section for a normal document, several
    when it is long enough and has top-level headings to split at."""
    from bs4 import NavigableString

    nodes = [n for n in soup.contents
             if not (isinstance(n, NavigableString) and not str(n).strip())]
    text_len = len(soup.get_text(' ', strip=True))

    level = None
    for h in ('h1', 'h2'):
        if sum(1 for n in nodes if getattr(n, 'name', None) == h) >= SPLIT_MIN_SECTIONS:
            level = h
            break
    if text_len < SPLIT_MIN_CHARS or level is None:
        return [(title, nodes)], text_len

    # (title, heading_node, nodes) per top-level heading; preamble has neither.
    sections, cur_title, cur_head, cur = [], None, None, []
    for n in nodes:
        if getattr(n, 'name', None) == level:
            if cur or cur_title:
                sections.append((cur_title, cur_head, cur))
            cur_title = re.sub(r'\s+', ' ', n.get_text(' ', strip=True)).strip()
            cur_head, cur = n, []
        else:
            cur.append(n)
    if cur or cur_title:
        sections.append((cur_title, cur_head, cur))

    def sec_len(nodes):
        return sum(len(n.get_text(' ', strip=True)) if hasattr(n, 'get_text')
                   else len(str(n).strip()) for n in nodes)

    # Merge short sections into their neighbor so every sub-SOP has substance.
    # Merged content keeps its heading inside the body.
    merged = []
    pending = []  # nodes (incl. headings) waiting to be prepended to the next section
    for title_, head, nodes_ in sections:
        if sec_len(nodes_) >= MIN_SECTION_CHARS:
            body = pending + nodes_
            pending = []
            merged.append([title_ or 'Introduction', body])
        elif merged and not pending:
            merged[-1][1].extend(([head] if head else []) + nodes_)
        else:
            pending.extend(([head] if head else []) + nodes_)
    if pending:
        if merged:
            merged[-1][1].extend(pending)
        else:
            merged.append(['Introduction', pending])

    if len(merged) < 2:
        return [(title, nodes)], text_len
    return [(t or f'Partie {i + 1}', ns) for i, (t, ns) in enumerate(merged)], text_len


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True)
    ap.add_argument('--db', default='ovh', help='ovh (prod) or sqlite (local test)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    source = os.path.expanduser(args.source)
    departments = scan_source(source)

    # --- Conversion pass (no DB/S3 needed) ---
    plan = []  # (dept_name, [(doc_path, title, [(sec_title, html)], images, split)])
    for dept_name, docs in departments:
        entries = []
        for path in docs:
            title = doc_title(path)
            soup, images = convert_docx(path)
            sections, text_len = split_plan(soup, title)
            split = len(sections) > 1
            rendered = [(t, ''.join(str(n) for n in nodes)) for t, nodes in sections]
            entries.append((path, title, rendered, images, split))
            mb = os.path.getsize(path) / 1e6
            mark = f'SPLIT en {len(sections)} sous-SOP' if split else 'SOP simple'
            print(f'  {dept_name} / {title}  ({mb:.1f} Mo, {text_len} car., '
                  f'{len(images)} images) -> {mark}')
            if split:
                for t, _ in rendered:
                    print(f'      - {t}')
        plan.append((dept_name, entries))

    n_docs = sum(len(e) for _, e in plan)
    n_arts = sum(len(r) for _, e in plan for (_, _, r, _, _) in e)
    n_imgs = sum(len(i) for _, e in plan for (_, _, _, i, _) in e)
    print(f'\nTotal: {len(plan)} départements, {n_docs} documents, '
          f'{n_arts} articles, {n_imgs} images')
    if args.dry_run:
        return

    # --- Import pass ---
    from __init__ import create_app
    from init_db import db

    app = create_app(db_name=args.db)
    with app.app_context():
        from help.models import SopDepartment, HelpCategory, HelpArticle, SopVersion
        from help.search import html_to_text
        from help.html_clean import clean_article_html
        from media import storage
        from media.models import MediaAsset

        if not storage.is_configured():
            sys.exit('S3 storage is not configured (OVH__* env vars) — aborting.')

        by_hash = {}  # sha1 -> media slug (dedup across documents)

        def upload_image(data, content_type, alt, base_slug, idx):
            h = hashlib.sha1(data).hexdigest()
            if h in by_hash:
                return by_hash[h]
            ext = EXT_BY_TYPE.get(content_type, 'bin')
            slug = f'{base_slug[:60]}-{idx:02d}'
            n = 1
            while MediaAsset.query.filter_by(slug=slug).first():
                n += 1
                slug = f'{base_slug[:60]}-{idx:02d}-{n}'
            key = f'{storage.MEDIA_PREFIX}import/{slug}.{ext}'
            if not storage.object_exists(key):
                storage.put_object(key, data, content_type)
            db.session.add(MediaAsset(
                brand=BRAND, slug=slug, filename=f'{slug}.{ext}',
                content_type=content_type, s3_key=key, size=len(data)))
            by_hash[h] = slug
            return slug

        dept_sort = (db.session.query(
            db.func.coalesce(db.func.max(SopDepartment.sort_order), -1))
            .filter_by(brand=BRAND).scalar() or -1) + 1
        created_arts = skipped = 0

        for dept_name, entries in plan:
            dept_slug = slugify(dept_name, 'departement')
            dept = SopDepartment.query.filter_by(brand=BRAND, slug=dept_slug).first()
            if not dept:
                dept = SopDepartment(
                    brand=BRAND, slug=dept_slug, name=dept_name,
                    icon=DEPT_ICONS.get(dept_slug, 'fa-folder-open'),
                    sort_order=dept_sort)
                db.session.add(dept)
                db.session.flush()
                dept_sort += 1
                print(f'+ département {dept_name}')

            art_sort = (db.session.query(
                db.func.coalesce(db.func.max(HelpArticle.sort_order), -1))
                .filter_by(brand=BRAND, department=dept_slug).scalar() or -1) + 1

            for path, title, sections, images, split in entries:
                doc_slug = slugify(f'{dept_slug}-{title}')[:150]
                category = 'Général'
                if split:
                    category = title[:160]
                    if not HelpCategory.query.filter_by(
                            brand=BRAND, department=dept_slug, name=category).first():
                        cat_sort = (db.session.query(
                            db.func.coalesce(db.func.max(HelpCategory.sort_order), -1))
                            .filter_by(brand=BRAND, department=dept_slug,
                                       parent_id=None).scalar() or -1) + 1
                        db.session.add(HelpCategory(
                            brand=BRAND, department=dept_slug, name=category,
                            sort_order=cat_sort))
                        db.session.flush()

                img_idx = 0
                for si, (sec_title, html) in enumerate(sections):
                    slug = doc_slug if not split else slugify(
                        f'{doc_slug}-{sec_title}')[:158]
                    if HelpArticle.query.filter_by(slug=slug).first():
                        print(f'  = {slug} existe déjà — ignoré')
                        skipped += 1
                        continue
                    # Materialize this article's images.
                    for cid, (data, ctype, alt) in images.items():
                        marker = f'cid:{cid}'
                        if marker not in html:
                            continue
                        img_idx += 1
                        mslug = upload_image(data, ctype, alt, doc_slug, img_idx)
                        html = html.replace(f'"{marker}"', f'"/media/file/{mslug}"')
                    body = clean_article_html(html)
                    art_title = sec_title if split else title
                    art = HelpArticle(
                        brand=BRAND, department=dept_slug, slug=slug,
                        title=art_title[:255], category=category,
                        body_html=body,
                        search_text=re.sub(
                            r'\s+', ' ',
                            f'{art_title} {html_to_text(body)}').strip()[:60000],
                        sort_order=art_sort, is_published=True)
                    art_sort += 1
                    db.session.add(art)
                    db.session.flush()
                    db.session.add(SopVersion(
                        article_id=art.id, version_no=1, title=art.title,
                        category=art.category, body_html=art.body_html))
                    created_arts += 1
                db.session.commit()  # per document, so a crash loses little
                print(f'  ✓ {title} ({len(sections)} article(s))')

        print(f'\nImport terminé : {created_arts} articles créés, '
              f'{skipped} ignorés (déjà présents), '
              f'{len(by_hash)} images uploadées.')


if __name__ == '__main__':
    main()

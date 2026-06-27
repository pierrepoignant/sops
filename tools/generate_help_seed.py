"""Build-time generator: convert the WINO boutique handbook (.docx) into
help-article seed files + extracted media assets committed to the repo.

Run with the tooling venv (mammoth + beautifulsoup4):
    . .toolvenv/bin/activate
    python tools/generate_help_seed.py "/path/to/Book boutiques ....docx"

Outputs (consumed at runtime by the idempotent seeder in help/seed.py):
    media/seed/help/<slug>.<ext>      extracted images
    media/seed/manifest.json          [{slug, filename, content_type}]
    help/seed/articles/<slug>.html    article body HTML
    help/seed/manifest.json           [{slug, title, category, order, search_text, html_file}]

This script is NOT imported at runtime — mammoth/bs4 are dev-only.
"""
import json
import os
import re
import sys
import unicodedata

import mammoth
from bs4 import BeautifulSoup, NavigableString

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(ROOT, 'media', 'seed', 'help')
HELP_ARTICLES_DIR = os.path.join(ROOT, 'help', 'seed', 'articles')

EXT_BY_TYPE = {
    'image/png': 'png', 'image/jpeg': 'jpeg', 'image/jpg': 'jpeg',
    'image/gif': 'gif', 'image/bmp': 'bmp', 'image/x-emf': 'emf',
    'image/webp': 'webp',
}


def slugify(value, fallback='item'):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^a-zA-Z0-9]+', '-', value).strip('-').lower()
    return value or fallback


def main(doc_path):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(HELP_ARTICLES_DIR, exist_ok=True)

    media_manifest = []
    counter = {'n': 0}

    def convert_image(image):
        counter['n'] += 1
        ext = EXT_BY_TYPE.get((image.content_type or '').lower(), 'bin')
        slug = f"help-{counter['n']:03d}"
        filename = f'{slug}.{ext}'
        with image.open() as b:
            data = b.read()
        with open(os.path.join(MEDIA_DIR, filename), 'wb') as out:
            out.write(data)
        media_manifest.append({
            'slug': slug,
            'filename': filename,
            'content_type': image.content_type or 'application/octet-stream',
        })
        attrs = {'src': f'/media/file/{slug}', 'class': 'help-img'}
        if getattr(image, 'alt_text', None):
            attrs['alt'] = image.alt_text
        return attrs

    with open(doc_path, 'rb') as f:
        result = mammoth.convert_to_html(
            f, convert_image=mammoth.images.img_element(convert_image)
        )

    soup = BeautifulSoup(result.value, 'html.parser')
    nodes = [n for n in soup.contents if not (isinstance(n, NavigableString) and not n.strip())]

    # Split into category (h1) > article (h2). Content before the first h2 of a
    # category becomes an intro article titled after the h1.
    articles = []
    cur_cat = 'Général'
    cur = None  # {'title','category','nodes'}

    def flush():
        if cur and any(str(x).strip() for x in cur['nodes']):
            articles.append(cur)

    for node in nodes:
        name = getattr(node, 'name', None)
        if name == 'h1':
            flush()
            cur_cat = node.get_text(' ', strip=True) or cur_cat
            cur = {'title': cur_cat, 'category': cur_cat, 'nodes': []}
        elif name == 'h2':
            title = node.get_text(' ', strip=True)
            if not title:
                if cur:
                    cur['nodes'].append(node)
                continue
            flush()
            cur = {'title': title, 'category': cur_cat, 'nodes': []}
        else:
            if cur is None:
                cur = {'title': cur_cat, 'category': cur_cat, 'nodes': []}
            cur['nodes'].append(node)
    flush()

    help_manifest = []
    seen = set()
    for i, art in enumerate(articles):
        base = f"{slugify(art['category'], 'cat')}-{slugify(art['title'], 'art')}"
        slug = base
        k = 2
        while slug in seen:
            slug = f'{base}-{k}'
            k += 1
        seen.add(slug)

        body_html = ''.join(str(n) for n in art['nodes']).strip()
        text = BeautifulSoup(body_html, 'html.parser').get_text(' ', strip=True)
        search_text = re.sub(r'\s+', ' ', f"{art['title']} {text}").strip()

        with open(os.path.join(HELP_ARTICLES_DIR, f'{slug}.html'), 'w', encoding='utf-8') as out:
            out.write(body_html)

        help_manifest.append({
            'slug': slug,
            'title': art['title'],
            'category': art['category'],
            'order': i,
            'search_text': search_text[:8000],
            'html_file': f'articles/{slug}.html',
        })

    with open(os.path.join(ROOT, 'media', 'seed', 'manifest.json'), 'w', encoding='utf-8') as out:
        json.dump(media_manifest, out, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, 'help', 'seed', 'manifest.json'), 'w', encoding='utf-8') as out:
        json.dump(help_manifest, out, ensure_ascii=False, indent=2)

    print(f'Articles: {len(help_manifest)}  |  Media: {len(media_manifest)}')
    cats = {}
    for a in help_manifest:
        cats.setdefault(a['category'], 0)
        cats[a['category']] += 1
    for c, n in cats.items():
        print(f'  {c}: {n} article(s)')
    if result.messages:
        print(f'mammoth messages: {len(result.messages)} (first 3)')
        for m in result.messages[:3]:
            print('  -', m)


if __name__ == '__main__':
    doc = sys.argv[1] if len(sys.argv) > 1 else None
    if not doc or not os.path.exists(doc):
        sys.exit('usage: generate_help_seed.py <path-to.docx>')
    main(doc)

"""Lightweight, portable help search.

Approach: multi-term substring matching over the article title + a plaintext
projection (`search_text`) of the body. Matching is accent- and
case-insensitive ("hygiene" finds "Hygiène"). Each query term must appear
(AND semantics); results are ranked by where the terms hit (title > body) and
how many distinct terms match, then a highlighted snippet is built around the
first match.

This needs no extra infrastructure and runs identically on MySQL and SQLite.
For much larger corpora, swap the candidate filter for a MySQL FULLTEXT index
(MATCH ... AGAINST) — the ranking/snippet code can stay.
"""
import html
import re
import unicodedata

from help.models import HelpArticle


def _fold_with_map(s):
    """Lowercase, accent-stripped copy of ``s`` plus an index map so folded
    positions can be traced back to the original string. Returns
    (folded, idx) where idx[i] is the original index of folded[i]."""
    out = []
    idx = []
    for i, ch in enumerate(s):
        base = ''.join(c for c in unicodedata.normalize('NFD', ch)
                       if not unicodedata.combining(c)).lower()
        for c in base:
            out.append(c)
            idx.append(i)
    return ''.join(out), idx


def fold(s):
    return _fold_with_map(s or '')[0]


def _terms(q):
    return [fold(t) for t in re.split(r'\s+', (q or '').strip()) if len(t) >= 2]


def html_to_text(value):
    if not value:
        return ''
    value = re.sub(r'(?is)<(script|style).*?</\1>', ' ', value)
    value = re.sub(r'(?s)<[^>]+>', ' ', value)
    value = html.unescape(value)
    return re.sub(r'\s+', ' ', value).strip()


def _snippet(text, terms, width=160):
    """Snippet of ``text`` around the earliest term hit, with every hit inside
    the window wrapped in <mark>. Matching runs on the folded text; slices are
    mapped back to the original so accents survive in the output."""
    folded, idx = _fold_with_map(text)

    def orig(fpos):
        return idx[fpos] if fpos < len(idx) else len(text)

    pos = -1
    for t in terms:
        i = folded.find(t)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    f_start = 0 if pos == -1 else max(0, pos - width // 3)
    f_end = min(f_start + width, len(folded))

    spans = []
    for t in set(terms):
        start = f_start
        while True:
            i = folded.find(t, start, f_end)
            if i == -1:
                break
            spans.append((i, i + len(t)))
            start = i + 1
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))

    out = []
    if orig(f_start) > 0:
        out.append('…')
    cur = f_start
    for s, e in merged:
        out.append(html.escape(text[orig(cur):orig(s)]))
        out.append('<mark>' + html.escape(text[orig(s):orig(e)]) + '</mark>')
        cur = e
    out.append(html.escape(text[orig(cur):orig(f_end)]))
    if orig(f_end) < len(text):
        out.append('…')
    return ''.join(out)


def search(q, brand, limit=30):
    terms = _terms(q)
    if not terms:
        return []
    articles = HelpArticle.query.filter_by(brand=brand, is_published=True).all()
    results = []
    for a in articles:
        title_f = fold(a.title)
        text_f = fold(a.search_text)
        if not all((t in title_f or t in text_f) for t in terms):
            continue
        title_hits = sum(1 for t in terms if t in title_f)
        score = title_hits * 100 + sum(1 for t in terms if t in text_f)
        results.append((score, a))
    results.sort(key=lambda r: (-r[0], r[1].sort_order, r[1].title))
    out = []
    for _, a in results[:limit]:
        out.append({
            'slug': a.slug, 'title': a.title, 'category': a.category,
            'snippet': _snippet(a.search_text or a.title, terms),
        })
    return out

"""Lightweight, portable help search.

Approach: multi-term substring matching over the article title + a plaintext
projection (`search_text`) of the body. Each query term must appear (AND
semantics); results are ranked by where the terms hit (title > body) and how
many distinct terms match, then a highlighted snippet is built around the
first match.

This needs no extra infrastructure and runs identically on MySQL and SQLite.
For much larger corpora, swap the candidate filter for a MySQL FULLTEXT index
(MATCH ... AGAINST) — the ranking/snippet code can stay.
"""
import html
import re

from help.models import HelpArticle


def _terms(q):
    return [t for t in re.split(r'\s+', (q or '').strip().lower()) if len(t) >= 2]


def html_to_text(value):
    if not value:
        return ''
    value = re.sub(r'(?is)<(script|style).*?</\1>', ' ', value)
    value = re.sub(r'(?s)<[^>]+>', ' ', value)
    value = html.unescape(value)
    return re.sub(r'\s+', ' ', value).strip()


def _snippet(text, terms, width=160):
    low = text.lower()
    pos = -1
    for t in terms:
        i = low.find(t)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        snippet = text[:width]
    else:
        start = max(0, pos - width // 3)
        snippet = ('…' if start > 0 else '') + text[start:start + width]
        if start + width < len(text):
            snippet += '…'
    out = html.escape(snippet)
    for t in sorted(set(terms), key=len, reverse=True):
        out = re.sub('(' + re.escape(html.escape(t)) + ')', r'<mark>\1</mark>', out,
                     flags=re.IGNORECASE)
    return out


def search(q, brand, limit=30):
    terms = _terms(q)
    if not terms:
        return []
    articles = HelpArticle.query.filter_by(brand=brand, is_published=True).all()
    results = []
    for a in articles:
        title_low = (a.title or '').lower()
        text_low = (a.search_text or '').lower()
        if not all((t in title_low or t in text_low) for t in terms):
            continue
        title_hits = sum(1 for t in terms if t in title_low)
        score = title_hits * 100 + sum(1 for t in terms if t in text_low)
        results.append((score, a))
    results.sort(key=lambda r: (-r[0], r[1].sort_order, r[1].title))
    out = []
    for _, a in results[:limit]:
        out.append({
            'slug': a.slug, 'title': a.title, 'category': a.category,
            'snippet': _snippet(a.search_text or a.title, terms),
        })
    return out

"""The tools an assistant can call, and the registry that lists them.

Every tool is read-only and sees one brand — the token's. They read the same
rows as the pages: `help.search.search` for the search, the published
article for the procedure, so the assistant reads what the page shows.
"""
from flask import g

from help.models import HelpArticle, HelpCategory, SopAttachment, SopDepartment, SopVersion
from help.search import html_to_text, search as run_search

DEFAULT_CAP = 50


class ToolError(Exception):
    """A message for the assistant (bad argument, nothing found), not a bug."""


_REGISTRY = {}   # name -> dict(fn, title, description, schema)


def tool(name, title, description, properties=None, required=()):
    """Register ``fn(token, **args) -> dict`` under ``name``."""
    schema = {'type': 'object', 'properties': properties or {}, 'additionalProperties': False}
    if required:
        schema['required'] = list(required)

    def deco(fn):
        _REGISTRY[name] = {'fn': fn, 'title': title, 'description': description.strip(), 'schema': schema}
        return fn
    return deco


def catalogue():
    return [{'name': n, 'title': t['title'], 'description': t['description'], 'inputSchema': t['schema']}
            for n, t in _REGISTRY.items()]


def call(token, name, args):
    t = _REGISTRY.get(name or '')
    if not t:
        raise ToolError(f"outil inconnu : {name}")
    unknown = set(args) - set(t['schema']['properties'])
    if unknown:
        raise ToolError(f"arguments inconnus : {', '.join(sorted(unknown))}")
    missing = [r for r in t['schema'].get('required', []) if args.get(r) in (None, '')]
    if missing:
        raise ToolError(f"arguments requis : {', '.join(missing)}")
    return t['fn'](token, **args)


# ------------------------------------------------------------------ helpers

def _limit(value, default=20, cap=DEFAULT_CAP):
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(1, min(n, cap))


def _dept(brand, slug_or_name):
    if not slug_or_name:
        return None
    d = SopDepartment.query.filter_by(brand=brand, slug=slug_or_name).first()
    if d is None:
        d = SopDepartment.query.filter_by(brand=brand, name=slug_or_name).first()
    if d is None:
        raise ToolError(f"département inconnu : {slug_or_name} (voir list_departments)")
    return d


def _article_brief(a):
    return {'slug': a.slug, 'title': a.title, 'department': a.department, 'category': a.category,
            'updated_at': a.updated_at, 'review_due': a.review_due}


# -------------------------------------------------------------------- tools

@tool('whoami', 'Qui est connecté',
      "La personne connectée, la marque ouverte par ce jeton et la liste des outils. "
      "À appeler en cas de doute sur la connexion.")
def whoami(token):
    user = token.user
    return {'brand': token.brand,
            'user': {'id': user.id, 'name': user.display_name, 'email': user.email,
                     'department': user.department, 'is_admin': bool(user.is_admin)},
            'scope': token.scope, 'tools': list(_REGISTRY)}


@tool('list_departments', 'Les départements',
      "Les départements de la marque (Boutique, Production…), avec le nombre de "
      "procédures publiées dans chacun.")
def list_departments(token):
    depts = (SopDepartment.query.filter_by(brand=token.brand)
             .order_by(SopDepartment.sort_order, SopDepartment.name).all())
    out = []
    for d in depts:
        n = HelpArticle.query.filter_by(brand=token.brand, department=d.slug, is_published=True).count()
        out.append({'slug': d.slug, 'name': d.name, 'procedures': n})
    return {'departments': out}


@tool('list_categories', 'Le plan d\'un département',
      "Les catégories d'un département, en arbre, avec les procédures publiées de "
      "chacune (slug et titre). C'est la table des matières du manuel.",
      properties={'department': {'type': 'string', 'description': 'le slug du département (list_departments)'}},
      required=('department',))
def list_categories(token, department):
    d = _dept(token.brand, department)
    cats = (HelpCategory.query.filter_by(brand=token.brand, department=d.slug)
            .order_by(HelpCategory.sort_order, HelpCategory.name).all())
    articles = (HelpArticle.query.filter_by(brand=token.brand, department=d.slug, is_published=True)
                .order_by(HelpArticle.sort_order, HelpArticle.title).all())
    by_cat = {}
    for a in articles:
        by_cat.setdefault(a.category, []).append({'slug': a.slug, 'title': a.title})
    by_parent = {}
    for c in cats:
        by_parent.setdefault(c.parent_id, []).append(c)

    def node(c):
        return {'name': c.name,
                'procedures': by_cat.pop(c.name, []),
                'children': [node(x) for x in by_parent.get(c.id, [])]}
    tree = [node(c) for c in by_parent.get(None, [])]
    # Procedures whose category has no row (seeded content) still count.
    loose = [{'name': name, 'procedures': lst, 'children': []} for name, lst in by_cat.items()]
    return {'department': {'slug': d.slug, 'name': d.name}, 'categories': tree + loose}


@tool('search_sops', 'Chercher une procédure',
      "Les procédures dont le titre ou le texte contient tous les mots demandés, "
      "les meilleures d'abord, avec un extrait. Deux lettres au moins par mot.",
      properties={'query': {'type': 'string', 'description': 'quelques mots, ex. « ouverture caisse »'},
                  'department': {'type': 'string', 'description': 'limiter à un département (slug)'},
                  'limit': {'type': 'integer', 'description': f'1 à {DEFAULT_CAP}, 20 par défaut'}},
      required=('query',))
def search_sops(token, query, department=None, limit=None):
    n = _limit(limit)
    results = run_search(query, token.brand, limit=DEFAULT_CAP)
    if department:
        d = _dept(token.brand, department)
        slugs = {a.slug for a in HelpArticle.query.filter_by(brand=token.brand, department=d.slug).all()}
        results = [r for r in results if r['slug'] in slugs]
    return {'query': query, 'count': len(results), 'results': results[:n]}


@tool('get_sop', 'Lire une procédure',
      "Une procédure en entier : son texte (sans la mise en forme), son département, "
      "sa catégorie, ses pièces jointes, sa version et sa dernière vérification.",
      properties={'slug': {'type': 'string', 'description': 'le slug (search_sops, list_categories)'}},
      required=('slug',))
def get_sop(token, slug):
    a = HelpArticle.query.filter_by(brand=token.brand, slug=slug, is_published=True).first()
    if a is None:
        raise ToolError(f"procédure introuvable : {slug}")
    d = SopDepartment.query.filter_by(brand=token.brand, slug=a.department).first()
    version = (SopVersion.query.filter_by(article_id=a.id)
               .order_by(SopVersion.version_no.desc()).first())
    attachments = SopAttachment.query.filter_by(article_id=a.id).order_by(SopAttachment.filename).all()
    out = _article_brief(a)
    out.update({
        'department_name': d.name if d else a.department,
        'text': html_to_text(a.body_html),
        'version': version.version_no if version else None,
        'last_reviewed_at': a.last_reviewed_at,
        'attachments': [{'filename': x.filename, 'content_type': x.content_type, 'size': x.size}
                        for x in attachments],
    })
    return out


@tool('recent_changes', 'Les procédures récemment modifiées',
      "Les procédures publiées modifiées dans les derniers jours, les plus récentes "
      "d'abord — pour savoir ce qui a changé dans le manuel.",
      properties={'days': {'type': 'integer', 'description': '1 à 365, 30 par défaut'},
                  'limit': {'type': 'integer', 'description': f'1 à {DEFAULT_CAP}, 20 par défaut'}})
def recent_changes(token, days=None, limit=None):
    from datetime import datetime, timedelta
    d = max(1, min(int(days or 30), 365))
    since = datetime.utcnow() - timedelta(days=d)
    rows = (HelpArticle.query.filter(HelpArticle.brand == token.brand, HelpArticle.is_published.is_(True),
                                     HelpArticle.updated_at >= since)
            .order_by(HelpArticle.updated_at.desc()).limit(_limit(limit)).all())
    return {'since': since, 'procedures': [_article_brief(a) for a in rows]}


def current_token():
    return getattr(g, 'mcp_token', None)

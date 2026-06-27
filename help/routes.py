import re
import uuid
import unicodedata
from collections import OrderedDict
from functools import wraps

from flask import (render_template, request, jsonify, abort, redirect,
                   url_for, flash, g)
from flask_login import login_required, current_user

from init_db import db
from help import help_bp
from help.models import HelpArticle, HelpCategory, SopDepartment
from help.search import search as run_search, html_to_text


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def _brand():
    """Active brand id for this request (set by the app before_request hook)."""
    return getattr(g, 'brand', None) or 'sablesienne'


def _slugify(value, fallback='article'):
    value = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^a-zA-Z0-9]+', '-', value).strip('-').lower()
    return value or fallback


def _unique_slug(base, exclude_id=None):
    base = _slugify(base)
    slug = base
    while True:
        existing = HelpArticle.query.filter_by(slug=slug).first()
        if not existing or existing.id == exclude_id:
            return slug
        slug = f'{base}-{uuid.uuid4().hex[:6]}'


# --- Departments ---

def _departments(brand=None):
    brand = brand or _brand()
    return (SopDepartment.query.filter_by(brand=brand)
            .order_by(SopDepartment.sort_order, SopDepartment.name).all())


def _get_department(dept_slug, brand=None):
    brand = brand or _brand()
    return SopDepartment.query.filter_by(brand=brand, slug=dept_slug).first()


# --- Category tree (within a brand + department) ---

def _category_levels(brand, department):
    """Categories grouped by parent for one (brand, department):
    (ordered L1 list, {parent_id: [children]})."""
    cats = (HelpCategory.query
            .filter_by(brand=brand, department=department)
            .order_by(HelpCategory.sort_order, HelpCategory.name).all())
    by_parent = {}
    for c in cats:
        by_parent.setdefault(c.parent_id, []).append(c)
    return by_parent.get(None, []), by_parent


def _category_options(brand, department):
    """[(name, depth)] in tree order for the article category dropdown."""
    roots, by_parent = _category_levels(brand, department)
    opts = []
    for r in roots:
        opts.append((r.name, 0))
        for ch in by_parent.get(r.id, []):
            opts.append((ch.name, 1))
    return opts


def _build_tree(articles, brand, department):
    """Nest articles under the category tree for one (brand, department).

    Returns (tree, orphans) where each tree node is
    ``{'cat', 'name', 'articles', 'children': [node...]}`` and orphans is a list
    of ``{'name', 'articles'}`` for articles whose category has no record."""
    by_cat = OrderedDict()
    for a in articles:
        by_cat.setdefault(a.category, []).append(a)
    roots, by_parent = _category_levels(brand, department)
    seen = set()

    def node(c):
        seen.add(c.name)
        return {'cat': c, 'name': c.name,
                'articles': by_cat.get(c.name, []),
                'children': [node(ch) for ch in by_parent.get(c.id, [])]}

    tree = [node(r) for r in roots]
    orphans = [{'name': name, 'articles': arts}
               for name, arts in by_cat.items() if name not in seen]
    return tree, orphans


def _reader_tree(brand, department):
    """Published-only category tree (one department) with empty branches pruned."""
    articles = (HelpArticle.query
                .filter_by(brand=brand, department=department, is_published=True)
                .order_by(HelpArticle.sort_order, HelpArticle.title).all())
    tree, orphans = _build_tree(articles, brand, department)

    def has_content(n):
        return bool(n['articles']) or any(has_content(c) for c in n['children'])

    pruned = []
    for n in tree:
        n['children'] = [c for c in n['children'] if has_content(c)]
        if has_content(n):
            pruned.append(n)
    return pruned, orphans


def _dept_counts(brand):
    """{department_slug: published SOP count} for the brand."""
    rows = (db.session.query(HelpArticle.department, db.func.count(HelpArticle.id))
            .filter_by(brand=brand, is_published=True)
            .group_by(HelpArticle.department).all())
    return dict(rows)


# --- Reader (all authenticated users) ---

@help_bp.route('/')
@login_required
def index():
    brand = _brand()
    q = (request.args.get('q') or '').strip()
    if q:
        results = run_search(q, brand)
        return render_template('help/search.html', q=q, results=results)
    depts = _departments(brand)
    counts = _dept_counts(brand)
    # Single department -> jump straight into it.
    if len(depts) == 1:
        return redirect(url_for('help.department', dept_slug=depts[0].slug))
    return render_template('help/index.html', departments=depts, counts=counts, q=q)


@help_bp.route('/api/search')
@login_required
def api_search():
    q = (request.args.get('q') or '').strip()
    return jsonify(results=run_search(q, _brand()))


@help_bp.route('/d/<dept_slug>')
@login_required
def department(dept_slug):
    brand = _brand()
    dept = _get_department(dept_slug, brand)
    if not dept:
        abort(404)
    tree, orphans = _reader_tree(brand, dept.slug)
    return render_template('help/department.html', dept=dept, tree=tree,
                           orphans=orphans, departments=_departments(brand))


@help_bp.route('/article/<slug>')
@login_required
def article(slug):
    brand = _brand()
    art = HelpArticle.query.filter_by(slug=slug, brand=brand).first()
    if not art or not art.is_published:
        if not (art and current_user.is_admin):
            abort(404)
    dept = _get_department(art.department, brand)
    tree, orphans = _reader_tree(brand, art.department)
    return render_template('help/article.html', art=art, dept=dept, tree=tree,
                           orphans=orphans)


# --- Management (admin only) ---

def _manage_department(brand):
    """Resolve the department being managed (?department=slug), defaulting to
    the first one. Returns the SopDepartment or None when none exist yet."""
    slug = (request.args.get('department') or '').strip()
    if slug:
        d = _get_department(slug, brand)
        if d:
            return d
    depts = _departments(brand)
    return depts[0] if depts else None


@help_bp.route('/manage')
@login_required
@admin_required
def manage():
    brand = _brand()
    dept = _manage_department(brand)
    if not dept:
        return render_template('help/manage.html', dept=None, tree=[], orphans=[],
                               total=0, departments=[])
    articles = (HelpArticle.query
                .filter_by(brand=brand, department=dept.slug)
                .order_by(HelpArticle.sort_order, HelpArticle.title).all())
    tree, orphans = _build_tree(articles, brand, dept.slug)
    return render_template('help/manage.html', dept=dept, tree=tree, orphans=orphans,
                           total=len(articles), departments=_departments(brand))


@help_bp.route('/new')
@login_required
@admin_required
def new():
    brand = _brand()
    dept = _manage_department(brand)
    if not dept:
        flash("Créez d'abord un département.", 'warning')
        return redirect(url_for('help.departments_manage'))
    preselect = (request.args.get('category') or '').strip()
    return render_template('help/edit.html', art=None, dept=dept,
                           category_options=_category_options(brand, dept.slug),
                           preselect=preselect)


@help_bp.route('/<int:art_id>/edit')
@login_required
@admin_required
def edit(art_id):
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    dept = _get_department(art.department, brand)
    return render_template('help/edit.html', art=art, dept=dept,
                           category_options=_category_options(brand, art.department),
                           preselect=None)


# --- Departments (admin only) ---

@help_bp.route('/departments')
@login_required
@admin_required
def departments_manage():
    brand = _brand()
    depts = _departments(brand)
    counts = dict(db.session.query(HelpArticle.department, db.func.count(HelpArticle.id))
                  .filter_by(brand=brand).group_by(HelpArticle.department).all())
    return render_template('help/departments.html', departments=depts, counts=counts)


@help_bp.route('/departments/new', methods=['POST'])
@login_required
@admin_required
def department_create():
    brand = _brand()
    name = (request.form.get('name') or '').strip()
    icon = (request.form.get('icon') or '').strip() or 'fa-folder-open'
    if not name:
        flash('Nom requis.', 'warning')
        return redirect(url_for('help.departments_manage'))
    slug = _slugify(name, 'departement')
    if _get_department(slug, brand):
        flash('Un département porte déjà ce nom.', 'warning')
        return redirect(url_for('help.departments_manage'))
    nxt = (db.session.query(db.func.coalesce(db.func.max(SopDepartment.sort_order), -1))
           .filter_by(brand=brand).scalar() or -1) + 1
    db.session.add(SopDepartment(brand=brand, slug=slug, name=name, icon=icon,
                                 sort_order=nxt))
    db.session.commit()
    flash('Département ajouté.', 'success')
    return redirect(url_for('help.departments_manage'))


@help_bp.route('/departments/<int:dept_id>/update', methods=['POST'])
@login_required
@admin_required
def department_update(dept_id):
    brand = _brand()
    dept = SopDepartment.query.filter_by(id=dept_id, brand=brand).first()
    if not dept:
        abort(404)
    name = (request.form.get('name') or '').strip()
    icon = (request.form.get('icon') or '').strip()
    if name:
        dept.name = name
    if icon:
        dept.icon = icon
    try:
        if request.form.get('sort_order') is not None:
            dept.sort_order = int(request.form.get('sort_order') or dept.sort_order)
    except (ValueError, TypeError):
        pass
    db.session.commit()
    flash('Département mis à jour.', 'success')
    return redirect(url_for('help.departments_manage'))


@help_bp.route('/departments/<int:dept_id>/delete', methods=['POST'])
@login_required
@admin_required
def department_delete(dept_id):
    brand = _brand()
    dept = SopDepartment.query.filter_by(id=dept_id, brand=brand).first()
    if not dept:
        abort(404)
    in_use = HelpArticle.query.filter_by(brand=brand, department=dept.slug).count()
    if in_use:
        flash(f'Impossible : {in_use} SOP(s) dans ce département.', 'warning')
        return redirect(url_for('help.departments_manage'))
    HelpCategory.query.filter_by(brand=brand, department=dept.slug).delete()
    db.session.delete(dept)
    db.session.commit()
    flash('Département supprimé.', 'success')
    return redirect(url_for('help.departments_manage'))


# --- Categories (admin only) ---

@help_bp.route('/categories')
@login_required
@admin_required
def categories():
    brand = _brand()
    dept = _manage_department(brand)
    if not dept:
        return redirect(url_for('help.departments_manage'))
    cats = (HelpCategory.query.filter_by(brand=brand, department=dept.slug)
            .order_by(HelpCategory.sort_order, HelpCategory.name).all())
    counts = dict(db.session.query(HelpArticle.category, db.func.count(HelpArticle.id))
                  .filter_by(brand=brand, department=dept.slug)
                  .group_by(HelpArticle.category).all())
    return render_template('help/categories.html', categories=cats, counts=counts,
                           dept=dept, departments=_departments(brand))


def _wants_json():
    return request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _back():
    return redirect(request.referrer or url_for('help.manage'))


@help_bp.route('/categories/new', methods=['POST'])
@login_required
@admin_required
def category_create():
    brand = _brand()
    name = (request.form.get('name') or '').strip()
    dept_slug = (request.form.get('department') or '').strip()
    dept = _get_department(dept_slug, brand)
    if not dept:
        flash('Département invalide.', 'warning')
        return _back()
    parent_id = request.form.get('parent_id')
    parent = None
    if parent_id:
        parent = HelpCategory.query.filter_by(id=int(parent_id), brand=brand,
                                              department=dept.slug).first()
        if parent and parent.parent_id is not None:
            parent = db.session.get(HelpCategory, parent.parent_id)
    if not name:
        flash('Nom requis.', 'warning')
    elif HelpCategory.query.filter_by(brand=brand, department=dept.slug, name=name).first():
        flash('Une catégorie porte déjà ce nom.', 'warning')
    else:
        siblings = HelpCategory.query.filter_by(
            brand=brand, department=dept.slug,
            parent_id=parent.id if parent else None)
        nxt = (siblings.with_entities(
            db.func.coalesce(db.func.max(HelpCategory.sort_order), -1)).scalar() or -1) + 1
        db.session.add(HelpCategory(brand=brand, department=dept.slug, name=name,
                                    sort_order=nxt,
                                    parent_id=parent.id if parent else None))
        db.session.commit()
        flash('Catégorie ajoutée.', 'success')
    return _back()


@help_bp.route('/categories/<int:cat_id>/update', methods=['POST'])
@login_required
@admin_required
def category_update(cat_id):
    brand = _brand()
    cat = HelpCategory.query.filter_by(id=cat_id, brand=brand).first()
    if not cat:
        abort(404)
    payload = request.get_json(silent=True) if request.is_json else request.form
    new_name = (payload.get('name') or '').strip()
    if 'sort_order' in payload:
        try:
            cat.sort_order = int(payload.get('sort_order') or cat.sort_order)
        except (ValueError, TypeError):
            pass
    if new_name and new_name != cat.name:
        clash = HelpCategory.query.filter_by(brand=brand, department=cat.department,
                                             name=new_name).first()
        if clash and clash.id != cat.id:
            if _wants_json():
                return jsonify(ok=False, error='Une catégorie porte déjà ce nom.'), 409
            flash('Une catégorie porte déjà ce nom.', 'warning')
            return _back()
        # Cascade the rename to every article in this brand+department.
        HelpArticle.query.filter_by(brand=brand, department=cat.department,
                                    category=cat.name).update(
            {HelpArticle.category: new_name}, synchronize_session=False)
        cat.name = new_name
    db.session.commit()
    if _wants_json():
        return jsonify(ok=True, name=cat.name)
    flash('Catégorie mise à jour.', 'success')
    return _back()


@help_bp.route('/categories/<int:cat_id>/delete', methods=['POST'])
@login_required
@admin_required
def category_delete(cat_id):
    brand = _brand()
    cat = HelpCategory.query.filter_by(id=cat_id, brand=brand).first()
    if not cat:
        abort(404)
    fallback = 'Général'
    in_use = HelpArticle.query.filter_by(brand=brand, department=cat.department,
                                         category=cat.name).count()
    if in_use:
        if not HelpCategory.query.filter_by(brand=brand, department=cat.department,
                                            name=fallback).first():
            db.session.add(HelpCategory(brand=brand, department=cat.department,
                                        name=fallback, sort_order=9999))
        HelpArticle.query.filter_by(brand=brand, department=cat.department,
                                    category=cat.name).update(
            {HelpArticle.category: fallback}, synchronize_session=False)
    moved_children = HelpCategory.query.filter_by(parent_id=cat.id).count()
    if moved_children:
        HelpCategory.query.filter_by(parent_id=cat.id).update(
            {HelpCategory.parent_id: cat.parent_id}, synchronize_session=False)
    db.session.delete(cat)
    db.session.commit()
    note = f' {in_use} SOP(s) déplacé(s) vers « {fallback} ».' if in_use else ''
    flash(f'Catégorie supprimée.{note}', 'success')
    return _back()


@help_bp.route('/reorder', methods=['POST'])
@login_required
@admin_required
def reorder():
    """Persist the drag-and-drop layout of one department's manage page."""
    brand = _brand()
    data = request.get_json(silent=True) or {}
    cat_map = {c.id: c for c in HelpCategory.query.filter_by(brand=brand).all()}

    def as_int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    desired = {}
    for row in data.get('categories', []):
        cid = as_int(row.get('id'))
        if cid is None or cid not in cat_map:
            continue
        pid = as_int(row.get('parent_id'))
        if pid not in cat_map or pid == cid:
            pid = None
        desired[cid] = pid
    for cid, pid in desired.items():
        if pid is not None and desired.get(pid) is not None:
            return jsonify(ok=False, error='Profondeur maximale : 2 niveaux.'), 400

    for row in data.get('categories', []):
        cid = as_int(row.get('id'))
        cat = cat_map.get(cid)
        if not cat:
            continue
        cat.parent_id = desired.get(cid)
        so = as_int(row.get('sort_order'))
        if so is not None:
            cat.sort_order = so

    for row in data.get('articles', []):
        art = HelpArticle.query.filter_by(id=as_int(row.get('id')), brand=brand).first()
        if not art:
            continue
        cat = cat_map.get(as_int(row.get('category_id')))
        if cat:
            art.category = cat.name
        so = as_int(row.get('sort_order'))
        if so is not None:
            art.sort_order = so

    db.session.commit()
    return jsonify(ok=True)


def _save_from_form(art, brand):
    title = (request.form.get('title') or '').strip()
    dept_slug = (request.form.get('department') or art.department or '').strip()
    category = (request.form.get('category') or 'Général').strip() or 'Général'
    body_html = request.form.get('body_html') or ''
    is_published = request.form.get('is_published') == 'on'
    try:
        sort_order = int(request.form.get('sort_order') or 0)
    except ValueError:
        sort_order = 0
    if not title:
        return None, 'Le titre est requis.'
    if not _get_department(dept_slug, brand):
        return None, 'Département invalide.'
    art.brand = brand
    art.department = dept_slug
    art.title = title
    art.category = category
    art.body_html = body_html
    art.search_text = re.sub(r'\s+', ' ', f'{title} {html_to_text(body_html)}').strip()[:60000]
    art.is_published = is_published
    art.sort_order = sort_order
    return art, None


@help_bp.route('/create', methods=['POST'])
@login_required
@admin_required
def create():
    brand = _brand()
    art = HelpArticle()
    art, err = _save_from_form(art, brand)
    if err:
        flash(err, 'warning')
        return redirect(url_for('help.new'))
    art.slug = _unique_slug(art.title)
    db.session.add(art)
    db.session.commit()
    flash('SOP créé.', 'success')
    return redirect(url_for('help.article', slug=art.slug))


@help_bp.route('/<int:art_id>/update', methods=['POST'])
@login_required
@admin_required
def update(art_id):
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    art, err = _save_from_form(art, brand)
    if err:
        flash(err, 'warning')
        return redirect(url_for('help.edit', art_id=art_id))
    db.session.commit()
    flash('SOP mis à jour.', 'success')
    return redirect(url_for('help.article', slug=art.slug))


@help_bp.route('/<int:art_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete(art_id):
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    db.session.delete(art)
    db.session.commit()
    flash('SOP supprimé.', 'success')
    return redirect(url_for('help.manage'))

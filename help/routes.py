import difflib
import json
import re
import uuid
import unicodedata
from collections import OrderedDict
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (render_template, request, jsonify, abort, redirect,
                   url_for, flash, g, Response)
from flask_login import login_required, current_user

from init_db import db
from help import help_bp
from help.models import (HelpArticle, HelpCategory, SopDepartment,
                         SopAttachment, SopVersion, SopRead,
                         SopArticleView, SopSearchLog, SopQuiz,
                         SopQuizQuestion, SopQuizAttempt)
from help.html_clean import clean_article_html
from help.search import search as run_search, html_to_text
from media import storage


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def user_can_edit(user, brand, dept_slug=None):
    """Admins edit everything. Contributors edit the SOPs of the department
    they are allocated to; with dept_slug=None, True if the contributor has a
    department at all."""
    if not user or not user.is_authenticated:
        return False
    if user.is_admin:
        return True
    if getattr(user, 'is_contributor', False) and user.department:
        return dept_slug is None or user.department == dept_slug
    return False


def editor_required(f):
    """Admin, or a contributor with a department. Department-specific checks
    happen inside the route via _require_edit."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not user_can_edit(current_user, _brand()):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def _require_edit(dept_slug):
    if not user_can_edit(current_user, _brand(), dept_slug):
        abort(403)


def _editable_departments(brand):
    """Departments the current user may manage, in display order."""
    depts = _departments(brand)
    if current_user.is_admin:
        return depts
    return [d for d in depts if d.slug == current_user.department]


def user_owns_department(user, dept):
    """Department owners (with admins) manage the department's quiz and see
    its stats. ``dept`` is a SopDepartment."""
    if not user or not user.is_authenticated or not dept:
        return False
    return user.is_admin or dept.owner_id == user.id


def owned_departments(user, brand):
    """Departments whose stats/quiz this user manages (admin: all)."""
    if not user or not user.is_authenticated:
        return []
    if user.is_admin:
        return _departments(brand)
    return (SopDepartment.query.filter_by(brand=brand, owner_id=user.id)
            .order_by(SopDepartment.sort_order, SopDepartment.name).all())


def _brand_users(brand):
    """Users considered part of a brand: their email domain is one of the
    brand's allowed OAuth/login domains. (Users aren't brand-scoped in the DB,
    so domain membership is the working definition.)"""
    from auth.models import User
    from brands import allowed_domains_for_brand
    domains = set(allowed_domains_for_brand(brand))
    return [u for u in User.query.all()
            if u.email and u.email.rsplit('@', 1)[-1].lower() in domains]


def _dept_users(brand, dept_slug):
    """Brand users allocated to a department — the expected readers of its
    SOPs and the audience of its quiz/notifications."""
    return [u for u in _brand_users(brand) if u.department == dept_slug]


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

def _log_search(q, count):
    """Record a search for the stats page. While the user is still typing,
    successive prefixes within a minute update the same row instead of
    stacking one log entry per keystroke."""
    brand = _brand()
    recent = (SopSearchLog.query
              .filter_by(user_id=current_user.id, brand=brand)
              .order_by(SopSearchLog.id.desc()).first())
    now = datetime.utcnow()
    if (recent and (now - recent.created_at).total_seconds() < 60
            and (q.lower().startswith(recent.query_text.lower())
                 or recent.query_text.lower().startswith(q.lower()))):
        recent.query_text = q[:255]
        recent.results_count = count
        recent.created_at = now
    else:
        db.session.add(SopSearchLog(brand=brand, user_id=current_user.id,
                                    query_text=q[:255], results_count=count))
    db.session.commit()


@help_bp.route('/')
@login_required
def index():
    brand = _brand()
    q = (request.args.get('q') or '').strip()
    if q:
        results = run_search(q, brand)
        _log_search(q, len(results))
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
    results = run_search(q, _brand())
    if len(q) >= 2:
        _log_search(q, len(results))
    return jsonify(results=results)


@help_bp.route('/d/<dept_slug>')
@login_required
def department(dept_slug):
    brand = _brand()
    dept = _get_department(dept_slug, brand)
    if not dept:
        abort(404)
    tree, orphans = _reader_tree(brand, dept.slug)

    # Quizzes: staff see the active ones (with their own scores); the owner
    # (or an admin) manages the full list from the "Gestion des quiz" tab.
    can_manage_quiz = user_owns_department(current_user, dept)
    all_quizzes = (SopQuiz.query.filter_by(brand=brand, department=dept.slug)
                   .order_by(SopQuiz.created_at.desc()).all())
    my_attempts_by_quiz = {}
    for a in (SopQuizAttempt.query
              .join(SopQuiz)
              .filter(SopQuiz.brand == brand, SopQuiz.department == dept.slug,
                      SopQuizAttempt.user_id == current_user.id)
              .order_by(SopQuizAttempt.id).all()):
        my_attempts_by_quiz.setdefault(a.quiz_id, []).append(a)
    # Staff list: active quizzes that actually have approved questions.
    active_quizzes = [(q, my_attempts_by_quiz.get(q.id, []))
                      for q in all_quizzes
                      if q.is_active and q.approved_questions]
    admin_quizzes = []
    if can_manage_quiz:
        for q in all_quizzes:
            attempts = q.attempts.order_by(SopQuizAttempt.id.desc()).all()
            avg = (sum(a.score / a.total for a in attempts if a.total)
                   / len(attempts) * 100) if attempts else None
            admin_quizzes.append({'quiz': q, 'n_attempts': len(attempts),
                                  'avg_pct': avg})

    return render_template('help/department.html', dept=dept, tree=tree,
                           orphans=orphans, departments=_departments(brand),
                           can_manage_quiz=can_manage_quiz,
                           active_quizzes=active_quizzes,
                           admin_quizzes=admin_quizzes,
                           ai_ok=_ai_configured())


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
    # Flat, display-ordered list of the department's SOPs for top-nav prev/next.
    flat = []
    for n in tree:
        flat.extend(n['articles'])
        for c in n['children']:
            flat.extend(c['articles'])
    for o in orphans:
        flat.extend(o['articles'])
    ids = [a.id for a in flat]
    prev_art = next_art = None
    if art.id in ids:
        i = ids.index(art.id)
        prev_art = flat[i - 1] if i > 0 else None
        next_art = flat[i + 1] if i < len(flat) - 1 else None
    can_edit = user_can_edit(current_user, brand, art.department)

    # View log (feeds the admin stats).
    db.session.add(SopArticleView(article_id=art.id, user_id=current_user.id))
    db.session.commit()

    # Read acknowledgment state for the current user.
    current_vno = _current_version_no(art)
    my_ack = (SopRead.query.filter_by(article_id=art.id, user_id=current_user.id)
              .order_by(SopRead.version_no.desc(), SopRead.id.desc()).first())
    ack_current = bool(my_ack and my_ack.version_no >= current_vno)

    versions = []
    readers = []
    if can_edit:
        versions = (SopVersion.query.filter_by(article_id=art.id)
                    .order_by(SopVersion.version_no.desc()).all())
    if current_user.is_admin:  # Lectures tab is admin-only
        readers = _reader_status(art, current_vno)

    return render_template('help/article.html', art=art, dept=dept, tree=tree,
                           orphans=orphans, prev_art=prev_art, next_art=next_art,
                           versions=versions, readers=readers,
                           current_vno=current_vno, my_ack=my_ack,
                           ack_current=ack_current,
                           storage_ok=storage.is_configured())


def _ai_configured():
    from help import ai_quiz
    return ai_quiz.is_configured()


def _current_version_no(art):
    last = (SopVersion.query.filter_by(article_id=art.id)
            .order_by(SopVersion.version_no.desc()).first())
    return last.version_no if last else 0


def _reader_status(art, current_vno):
    """[(user, last_ack, up_to_date)] for the users allocated to the article's
    department — the read-coverage view admins see on the Lectures tab."""
    acks = {}
    for r in (SopRead.query.filter_by(article_id=art.id)
              .order_by(SopRead.version_no, SopRead.id)):
        acks[r.user_id] = r  # keeps the highest version per user
    expected = _dept_users(_brand(), art.department)
    rows = []
    for u in sorted(expected, key=lambda u: u.display_name.lower()):
        ack = acks.get(u.id)
        rows.append((u, ack, bool(ack and ack.version_no >= current_vno)))
    return rows


@help_bp.route('/<int:art_id>/ack', methods=['POST'])
@login_required
def acknowledge(art_id):
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art or not (art.is_published or current_user.is_admin):
        abort(404)
    db.session.add(SopRead(article_id=art.id, user_id=current_user.id,
                           version_no=_current_version_no(art)))
    db.session.commit()
    flash('Lecture confirmée — merci !', 'success')
    return redirect(url_for('help.article', slug=art.slug))


@help_bp.route('/<int:art_id>/reviewed', methods=['POST'])
@login_required
@editor_required
def mark_reviewed(art_id):
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    _require_edit(art.department)
    art.last_reviewed_at = datetime.utcnow()
    art.last_reviewed_by_id = current_user.id
    art.review_due = date.today() + timedelta(days=365)
    db.session.commit()
    flash('SOP marqué comme vérifié — prochaine revue dans 12 mois.', 'success')
    return redirect(url_for('help.article', slug=art.slug))


# --- Management (admin only) ---

def _manage_department(brand):
    """Resolve the department being managed (?department=slug), defaulting to
    the first one the user may edit. Returns the SopDepartment or None."""
    editable = _editable_departments(brand)
    slug = (request.args.get('department') or '').strip()
    if slug:
        for d in editable:
            if d.slug == slug:
                return d
        if _get_department(slug, brand):
            abort(403)
    return editable[0] if editable else None


@help_bp.route('/manage')
@login_required
@editor_required
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
                           total=len(articles),
                           departments=_editable_departments(brand))


@help_bp.route('/new')
@login_required
@editor_required
def new():
    brand = _brand()
    dept = _manage_department(brand)
    if not dept:
        flash("Créez d'abord un département.", 'warning')
        return redirect(url_for('help.departments_manage')
                        if current_user.is_admin else url_for('help.index'))
    preselect = (request.args.get('category') or '').strip()
    return render_template('help/edit.html', art=None, dept=dept,
                           category_options=_category_options(brand, dept.slug),
                           preselect=preselect,
                           owner_options=_owner_options(brand, dept.slug))


@help_bp.route('/<int:art_id>/edit')
@login_required
@editor_required
def edit(art_id):
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    _require_edit(art.department)
    dept = _get_department(art.department, brand)
    return render_template('help/edit.html', art=art, dept=dept,
                           category_options=_category_options(brand, art.department),
                           preselect=None,
                           owner_options=_owner_options(brand, art.department))


def _owner_options(brand, dept_slug):
    """Candidate SOP owners: admins + contributors allocated to the department."""
    from auth.models import User
    out = {u.id: u for u in User.query.filter_by(role='admin').all()}
    for u in User.query.filter_by(role='contributor', department=dept_slug).all():
        out[u.id] = u
    return sorted(out.values(), key=lambda u: u.display_name.lower())


# --- Departments (admin only) ---

@help_bp.route('/departments')
@login_required
@admin_required
def departments_manage():
    brand = _brand()
    depts = _departments(brand)
    counts = dict(db.session.query(HelpArticle.department, db.func.count(HelpArticle.id))
                  .filter_by(brand=brand).group_by(HelpArticle.department).all())
    owner_candidates = sorted(_brand_users(brand),
                              key=lambda u: u.display_name.lower())
    return render_template('help/departments.html', departments=depts,
                           counts=counts, owner_candidates=owner_candidates)


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
    if 'owner_id' in request.form:
        raw = request.form.get('owner_id')
        try:
            dept.owner_id = int(raw) if raw else None
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
@editor_required
def categories():
    brand = _brand()
    dept = _manage_department(brand)
    if not dept:
        return redirect(url_for('help.departments_manage')
                        if current_user.is_admin else url_for('help.index'))
    cats = (HelpCategory.query.filter_by(brand=brand, department=dept.slug)
            .order_by(HelpCategory.sort_order, HelpCategory.name).all())
    counts = dict(db.session.query(HelpArticle.category, db.func.count(HelpArticle.id))
                  .filter_by(brand=brand, department=dept.slug)
                  .group_by(HelpArticle.category).all())
    return render_template('help/categories.html', categories=cats, counts=counts,
                           dept=dept, departments=_editable_departments(brand))


def _wants_json():
    return request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _back():
    return redirect(request.referrer or url_for('help.manage'))


@help_bp.route('/categories/new', methods=['POST'])
@login_required
@editor_required
def category_create():
    brand = _brand()
    name = (request.form.get('name') or '').strip()
    dept_slug = (request.form.get('department') or '').strip()
    dept = _get_department(dept_slug, brand)
    if not dept:
        flash('Département invalide.', 'warning')
        return _back()
    _require_edit(dept.slug)
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
@editor_required
def category_update(cat_id):
    brand = _brand()
    cat = HelpCategory.query.filter_by(id=cat_id, brand=brand).first()
    if not cat:
        abort(404)
    _require_edit(cat.department)
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
@editor_required
def category_delete(cat_id):
    brand = _brand()
    cat = HelpCategory.query.filter_by(id=cat_id, brand=brand).first()
    if not cat:
        abort(404)
    _require_edit(cat.department)
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
@editor_required
def reorder():
    """Persist the drag-and-drop layout of one department's manage page."""
    brand = _brand()
    data = request.get_json(silent=True) or {}
    cat_map = {c.id: c for c in HelpCategory.query.filter_by(brand=brand).all()
               if user_can_edit(current_user, brand, c.department)}

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
        if not art or not user_can_edit(current_user, brand, art.department):
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
    # Review cycle fields.
    owner_id = request.form.get('owner_id')
    try:
        art.owner_id = int(owner_id) if owner_id else None
    except (ValueError, TypeError):
        art.owner_id = None
    review_due = (request.form.get('review_due') or '').strip()
    if review_due:
        try:
            art.review_due = datetime.strptime(review_due, '%Y-%m-%d').date()
        except ValueError:
            pass
    else:
        art.review_due = None
    return art, None


def _notify_team(art, editor, is_new):
    """Email the brand's users that a SOP was created/updated. Opt-in via the
    'notify_team' checkbox on the edit form; skipped for drafts."""
    from auth.email_sender import send_email
    brand = _brand()
    recipients = [u.email for u in _brand_users(brand)
                  if u.id != editor.id and u.last_login
                  and (u.department == art.department or u.is_admin)]
    if not recipients:
        return 0
    link = request.url_root.rstrip('/') + url_for('help.article', slug=art.slug)
    verb = 'a été ajoutée' if is_new else 'a été mise à jour'
    body = (
        f"Bonjour,\n\n"
        f"La procédure « {art.title} » ({art.category}) {verb} par "
        f"{editor.display_name}.\n\n"
        f"Consultez-la et confirmez votre lecture :\n{link}\n\n"
        f"— Espace SOP"
    )
    subject = f'SOP {"nouvelle" if is_new else "mise à jour"} : {art.title}'
    try:
        return send_email(recipients, subject, body, brand_id=brand)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('SOP notification email failed')
        return 0


def _snapshot(art, editor_id, baseline=None):
    """Append a SopVersion capturing the article's current state, unless it is
    identical to the latest one. ``baseline`` snapshots a pre-edit state (used
    the first time an article created before versioning gets edited)."""
    state = baseline or {'title': art.title, 'category': art.category,
                         'body_html': art.body_html}
    last = (SopVersion.query.filter_by(article_id=art.id)
            .order_by(SopVersion.version_no.desc()).first())
    if last and (last.title == state['title']
                 and last.category == state['category']
                 and last.body_html == state['body_html']):
        return
    db.session.add(SopVersion(article_id=art.id,
                              version_no=(last.version_no + 1) if last else 1,
                              edited_by_id=editor_id, **state))


@help_bp.route('/create', methods=['POST'])
@login_required
@editor_required
def create():
    brand = _brand()
    art = HelpArticle()
    art, err = _save_from_form(art, brand)
    if err:
        flash(err, 'warning')
        return redirect(url_for('help.new'))
    _require_edit(art.department)
    art.slug = _unique_slug(art.title)
    db.session.add(art)
    db.session.flush()
    _snapshot(art, current_user.id)
    db.session.commit()
    if request.form.get('notify_team') == 'on' and art.is_published:
        n = _notify_team(art, current_user, is_new=True)
        if n:
            flash(f"L'équipe a été notifiée ({n} destinataires).", 'info')
    flash('SOP créé.', 'success')
    return redirect(url_for('help.article', slug=art.slug))


@help_bp.route('/<int:art_id>/update', methods=['POST'])
@login_required
@editor_required
def update(art_id):
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    _require_edit(art.department)
    # Articles older than versioning have no v1: snapshot their pre-edit state
    # first so the diff of this edit has something to compare against.
    pre_edit = {'title': art.title, 'category': art.category,
                'body_html': art.body_html}
    has_versions = SopVersion.query.filter_by(article_id=art.id).count() > 0
    art, err = _save_from_form(art, brand)
    if err:
        flash(err, 'warning')
        return redirect(url_for('help.edit', art_id=art_id))
    _require_edit(art.department)
    if not has_versions:
        _snapshot(art, None, baseline=pre_edit)
        db.session.flush()
    _snapshot(art, current_user.id)
    db.session.commit()
    if request.form.get('notify_team') == 'on' and art.is_published:
        n = _notify_team(art, current_user, is_new=False)
        if n:
            flash(f"L'équipe a été notifiée ({n} destinataires).", 'info')
    flash('SOP mis à jour.', 'success')
    return redirect(url_for('help.article', slug=art.slug))


@help_bp.route('/<int:art_id>/clean-html', methods=['POST'])
@login_required
@editor_required
def clean_html(art_id):
    """One-click cleanup of editor cruft (inline styles, Quill artifacts,
    spacer paragraphs) in the article body. Versioned like a normal edit."""
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    _require_edit(art.department)
    cleaned = clean_article_html(art.body_html)
    if cleaned == (art.body_html or '').strip():
        flash('Le HTML de ce SOP est déjà propre.', 'info')
        return redirect(url_for('help.edit', art_id=art.id))
    pre_edit = {'title': art.title, 'category': art.category,
                'body_html': art.body_html}
    has_versions = SopVersion.query.filter_by(article_id=art.id).count() > 0
    before = len(art.body_html or '')
    art.body_html = cleaned
    art.search_text = re.sub(
        r'\s+', ' ', f'{art.title} {html_to_text(cleaned)}').strip()[:60000]
    if not has_versions:
        _snapshot(art, None, baseline=pre_edit)
        db.session.flush()
    _snapshot(art, current_user.id)
    db.session.commit()
    flash(f'HTML nettoyé : {before} → {len(cleaned)} caractères.', 'success')
    return redirect(url_for('help.edit', art_id=art.id))


@help_bp.route('/<int:art_id>/delete', methods=['POST'])
@login_required
@editor_required
def delete(art_id):
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    _require_edit(art.department)
    for att in art.attachments:
        storage.delete_object(att.s3_key)
    # Quiz questions live at department level; just unlink their source SOP.
    SopQuizQuestion.query.filter_by(article_id=art.id).update(
        {SopQuizQuestion.article_id: None}, synchronize_session=False)
    db.session.delete(art)
    db.session.commit()
    flash('SOP supprimé.', 'success')
    return redirect(url_for('help.manage'))


# --- Attachments ---

ATTACHMENT_PREFIX = 'sops/attachments/'


@help_bp.route('/<int:art_id>/attachments', methods=['POST'])
@login_required
@editor_required
def attachment_upload(art_id):
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    _require_edit(art.department)
    if not storage.is_configured():
        flash("Le stockage S3 n'est pas configuré.", 'warning')
        return redirect(url_for('help.article', slug=art.slug) + '#fichiers')
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        flash('Aucun fichier sélectionné.', 'warning')
        return redirect(url_for('help.article', slug=art.slug) + '#fichiers')
    saved = 0
    for f in files:
        data = f.read()
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'bin'
        key = f'{ATTACHMENT_PREFIX}{art.id}/{uuid.uuid4().hex}.{ext}'
        try:
            storage.put_object(key, data, f.mimetype or 'application/octet-stream')
        except Exception as e:
            flash(f"Échec de l'envoi de {f.filename} : {e}", 'danger')
            continue
        db.session.add(SopAttachment(
            article_id=art.id, filename=f.filename,
            content_type=f.mimetype or 'application/octet-stream',
            s3_key=key, size=len(data), uploaded_by_id=current_user.id))
        saved += 1
    db.session.commit()
    if saved:
        flash(f'{saved} fichier(s) ajouté(s).', 'success')
    return redirect(url_for('help.article', slug=art.slug) + '#fichiers')


@help_bp.route('/attachments/<int:att_id>/download')
@login_required
def attachment_download(att_id):
    att = db.session.get(SopAttachment, att_id)
    if not att or att.article.brand != _brand():
        abort(404)
    if not att.article.is_published and not current_user.is_admin:
        abort(404)
    if not storage.is_configured():
        abort(503)
    try:
        data, content_type = storage.get_object_bytes(att.s3_key)
    except Exception:
        abort(404)
    resp = Response(data, mimetype=content_type or att.content_type)
    resp.headers['Content-Disposition'] = f'attachment; filename="{att.filename}"'
    return resp


@help_bp.route('/attachments/<int:att_id>/delete', methods=['POST'])
@login_required
@editor_required
def attachment_delete(att_id):
    att = db.session.get(SopAttachment, att_id)
    if not att or att.article.brand != _brand():
        abort(404)
    _require_edit(att.article.department)
    slug = att.article.slug
    storage.delete_object(att.s3_key)
    db.session.delete(att)
    db.session.commit()
    flash('Fichier supprimé.', 'success')
    return redirect(url_for('help.article', slug=slug) + '#fichiers')


# --- Versions ---

def _get_version(art_id, version_no, brand):
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    _require_edit(art.department)
    ver = SopVersion.query.filter_by(article_id=art.id,
                                     version_no=version_no).first()
    if not ver:
        abort(404)
    return art, ver


@help_bp.route('/<int:art_id>/version/<int:version_no>')
@login_required
@editor_required
def version_view(art_id, version_no):
    art, ver = _get_version(art_id, version_no, _brand())
    is_latest = ver.version_no == _current_version_no(art)
    return render_template('help/version_view.html', art=art, ver=ver,
                           is_latest=is_latest,
                           dept=_get_department(art.department))


@help_bp.route('/<int:art_id>/version/<int:version_no>/restore', methods=['POST'])
@login_required
@editor_required
def version_restore(art_id, version_no):
    art, ver = _get_version(art_id, version_no, _brand())
    if ver.version_no == _current_version_no(art):
        flash('Cette version est déjà la version actuelle.', 'info')
        return redirect(url_for('help.article', slug=art.slug) + '#versions')
    art.title = ver.title
    art.category = ver.category
    art.body_html = ver.body_html
    art.search_text = re.sub(
        r'\s+', ' ', f'{ver.title} {html_to_text(ver.body_html)}').strip()[:60000]
    _snapshot(art, current_user.id)
    db.session.commit()
    flash(f'Version v{ver.version_no} restaurée (enregistrée comme nouvelle version).',
          'success')
    return redirect(url_for('help.article', slug=art.slug) + '#versions')


def _diff_segments(old_text, new_text, context=20):
    """Word-level diff as [(op, text)] with op in eq/del/ins; long equal runs
    are collapsed to their edges with an ellipsis."""
    old_w, new_w = old_text.split(), new_text.split()
    segs = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_w, new_w, autojunk=False).get_opcodes():
        if op == 'equal':
            words = old_w[i1:i2]
            if len(words) > context * 2 + 5:
                segs.append(('eq', ' '.join(words[:context])))
                segs.append(('skip', ''))
                segs.append(('eq', ' '.join(words[-context:])))
            else:
                segs.append(('eq', ' '.join(words)))
        else:
            if op in ('replace', 'delete'):
                segs.append(('del', ' '.join(old_w[i1:i2])))
            if op in ('replace', 'insert'):
                segs.append(('ins', ' '.join(new_w[j1:j2])))
    return segs


@help_bp.route('/<int:art_id>/version/<int:version_no>/diff')
@login_required
@editor_required
def version_diff(art_id, version_no):
    brand = _brand()
    art, ver = _get_version(art_id, version_no, brand)
    try:
        from_no = int(request.args.get('from', version_no - 1))
    except (TypeError, ValueError):
        from_no = version_no - 1
    prev = (SopVersion.query.filter_by(article_id=art.id, version_no=from_no)
            .first()) if from_no >= 1 else None
    old_text = html_to_text(prev.body_html) if prev else ''
    new_text = html_to_text(ver.body_html)
    segments = _diff_segments(re.sub(r'\s+', ' ', old_text).strip(),
                              re.sub(r'\s+', ' ', new_text).strip())
    changed = any(op in ('del', 'ins') for op, _ in segments)
    return render_template('help/version_diff.html', art=art, ver=ver, prev=prev,
                           segments=segments, changed=changed,
                           dept=_get_department(art.department))


# --- QR codes ---

def _article_or_404(slug, brand):
    art = HelpArticle.query.filter_by(slug=slug, brand=brand).first()
    if not art or not (art.is_published or
                       user_can_edit(current_user, brand, art.department)):
        abort(404)
    return art


@help_bp.route('/article/<slug>/qr.svg')
@login_required
def article_qr(slug):
    import segno
    art = _article_or_404(slug, _brand())
    link = request.url_root.rstrip('/') + url_for('help.article', slug=art.slug)
    qr = segno.make(link, error='m')
    svg = qr.svg_inline(scale=6, dark='#1a1a1a')
    resp = Response(f'<svg xmlns="http://www.w3.org/2000/svg" '
                    f'viewBox="0 0 {qr.symbol_size(6)[0]} {qr.symbol_size(6)[1]}">'
                    f'{svg}</svg>', mimetype='image/svg+xml')
    resp.headers['Cache-Control'] = 'private, max-age=86400'
    return resp


@help_bp.route('/article/<slug>/qr')
@login_required
def article_qr_poster(slug):
    art = _article_or_404(slug, _brand())
    link = request.url_root.rstrip('/') + url_for('help.article', slug=art.slug)
    return render_template('help/qr_poster.html', art=art, link=link,
                           dept=_get_department(art.department))


# --- Training quizzes (several per department) ---

def _get_quiz(quiz_id, manage=False):
    """Load a quiz of the active brand (+ its department); 403 unless the
    caller owns the department when manage=True."""
    quiz = db.session.get(SopQuiz, quiz_id)
    if not quiz or quiz.brand != _brand():
        abort(404)
    dept = _get_department(quiz.department)
    if not dept:
        abort(404)
    if manage and not user_owns_department(current_user, dept):
        abort(403)
    return quiz, dept


def _generate_into_quiz(quiz, dept, count):
    """AI-generate ``count`` proposed questions into ``quiz``. Returns an
    error message to flash, or None on success."""
    from help import ai_quiz
    brand = _brand()
    articles = (HelpArticle.query
                .filter_by(brand=brand, department=dept.slug, is_published=True)
                .order_by(HelpArticle.sort_order, HelpArticle.title).all())
    if not articles:
        return "Aucun SOP publié dans ce département — rien à générer."
    # Avoid near-duplicates across every quiz of the department.
    existing = (SopQuizQuestion.query.join(SopQuiz)
                .filter(SopQuiz.brand == brand,
                        SopQuiz.department == dept.slug,
                        SopQuizQuestion.status != 'rejected').all())
    try:
        questions = ai_quiz.generate_questions(dept, articles, count=count,
                                               existing_questions=existing)
    except RuntimeError as e:
        return str(e)
    by_slug = {a.slug: a.id for a in articles}
    for q in questions:
        db.session.add(SopQuizQuestion(
            quiz_id=quiz.id,
            article_id=by_slug.get(q.get('article_slug')),
            question=q['question'],
            options_json=json.dumps(q['options'], ensure_ascii=False),
            correct_index=q['correct_index'], explanation=q['explanation']))
    db.session.commit()
    flash(f'{len(questions)} questions générées — validez celles à garder.',
          'success')
    return None


@help_bp.route('/d/<dept_slug>/quiz/new', methods=['POST'])
@login_required
def quiz_create(dept_slug):
    brand = _brand()
    dept = _get_department(dept_slug, brand)
    if not dept:
        abort(404)
    if not user_owns_department(current_user, dept):
        abort(403)
    try:
        count = max(1, min(20, int(request.form.get('count') or 10)))
    except ValueError:
        count = 10
    title = (request.form.get('title') or '').strip()
    if not title:
        title = f"Quiz {dept.name} du {datetime.utcnow().strftime('%d/%m/%Y')}"
    quiz = SopQuiz(brand=brand, department=dept.slug, title=title[:160],
                   created_by_id=current_user.id)
    db.session.add(quiz)
    db.session.flush()
    err = _generate_into_quiz(quiz, dept, count)
    if err:
        db.session.rollback()
        flash(err, 'danger')
        return redirect(url_for('help.department', dept_slug=dept.slug) + '#quiz-admin')
    return redirect(url_for('help.quiz_admin', quiz_id=quiz.id))


@help_bp.route('/quiz/<int:quiz_id>/admin')
@login_required
def quiz_admin(quiz_id):
    quiz, dept = _get_quiz(quiz_id, manage=True)
    attempts = quiz.attempts.order_by(SopQuizAttempt.id.desc()).all()
    return render_template('help/quiz_admin.html', quiz=quiz, dept=dept,
                           attempts=attempts, ai_ok=_ai_configured())


@help_bp.route('/quiz/<int:quiz_id>/generate', methods=['POST'])
@login_required
def quiz_generate(quiz_id):
    quiz, dept = _get_quiz(quiz_id, manage=True)
    try:
        count = max(1, min(20, int(request.form.get('count') or 10)))
    except ValueError:
        count = 10
    err = _generate_into_quiz(quiz, dept, count)
    if err:
        flash(err, 'danger')
    return redirect(url_for('help.quiz_admin', quiz_id=quiz.id))


@help_bp.route('/quiz/<int:quiz_id>/toggle', methods=['POST'])
@login_required
def quiz_toggle(quiz_id):
    quiz, dept = _get_quiz(quiz_id, manage=True)
    if not quiz.is_active and not quiz.approved_questions:
        flash("Validez au moins une question avant d'activer ce quiz.",
              'warning')
    else:
        quiz.is_active = not quiz.is_active
        db.session.commit()
        flash(f"Quiz « {quiz.title} » "
              f"{'activé — visible des employés' if quiz.is_active else 'désactivé'}.",
              'success')
    dest = request.form.get('back') or ''
    if dest == 'admin':
        return redirect(url_for('help.quiz_admin', quiz_id=quiz.id))
    return redirect(url_for('help.department', dept_slug=dept.slug) + '#quiz-admin')


@help_bp.route('/quiz/<int:quiz_id>/delete', methods=['POST'])
@login_required
def quiz_delete(quiz_id):
    quiz, dept = _get_quiz(quiz_id, manage=True)
    db.session.delete(quiz)  # questions + attempts cascade
    db.session.commit()
    flash(f'Quiz « {quiz.title} » supprimé.', 'success')
    return redirect(url_for('help.department', dept_slug=dept.slug) + '#quiz-admin')


@help_bp.route('/quiz/questions/<int:q_id>/<action>', methods=['POST'])
@login_required
def quiz_question_action(q_id, action):
    if action not in ('approve', 'reject', 'delete'):
        abort(404)
    q = db.session.get(SopQuizQuestion, q_id)
    if not q:
        abort(404)
    quiz, _dept = _get_quiz(q.quiz_id, manage=True)
    if action == 'delete':
        db.session.delete(q)
    else:
        q.status = 'approved' if action == 'approve' else 'rejected'
    db.session.commit()
    return redirect(url_for('help.quiz_admin', quiz_id=quiz.id))


@help_bp.route('/quiz/<int:quiz_id>/take')
@login_required
def quiz_take(quiz_id):
    quiz, dept = _get_quiz(quiz_id)
    questions = quiz.approved_questions
    if not (quiz.is_active and questions):
        flash("Ce quiz n'est pas actif.", 'warning')
        return redirect(url_for('help.department', dept_slug=dept.slug) + '#quiz')
    my_attempts = (quiz.attempts.filter_by(user_id=current_user.id)
                   .order_by(SopQuizAttempt.id.desc()).all())
    return render_template('help/quiz_take.html', quiz=quiz, dept=dept,
                           questions=questions, my_attempts=my_attempts)


@help_bp.route('/quiz/<int:quiz_id>/submit', methods=['POST'])
@login_required
def quiz_submit(quiz_id):
    quiz, dept = _get_quiz(quiz_id)
    questions = quiz.approved_questions
    if not (quiz.is_active and questions):
        flash("Ce quiz n'est pas actif.", 'warning')
        return redirect(url_for('help.department', dept_slug=dept.slug) + '#quiz')
    answers = []
    graded = []
    score = 0
    for q in questions:
        raw = request.form.get(f'q{q.id}')
        try:
            picked = int(raw)
        except (TypeError, ValueError):
            picked = -1
        ok = picked == q.correct_index
        score += 1 if ok else 0
        answers.append(picked)
        graded.append((q, picked, ok))
    attempt = SopQuizAttempt(quiz_id=quiz.id, user_id=current_user.id,
                             score=score, total=len(questions),
                             answers_json=json.dumps(answers))
    db.session.add(attempt)
    db.session.commit()
    return render_template('help/quiz_result.html', quiz=quiz, dept=dept,
                           graded=graded, score=score, total=len(questions))

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
                         SopAttachment, SopDeptAttachment, SopVersion, SopRead,
                         SopArticleView, SopSearchLog, SopQuiz,
                         SopQuizQuestion, SopQuizAttempt, SopPendingChange)
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
    """Admins edit everything. Other users edit the SOPs of the departments
    whose contributors list they are on — the department owner is implicitly
    a contributor. With dept_slug=None, True if they can edit at least one
    department of the brand."""
    if not user or not user.is_authenticated:
        return False
    if user.is_admin:
        return True
    q = SopDepartment.query.filter(
        SopDepartment.brand == brand,
        db.or_(SopDepartment.owner_id == user.id,
               SopDepartment.contributors.any(id=user.id)))
    if dept_slug:
        q = q.filter(SopDepartment.slug == dept_slug)
    return db.session.query(q.exists()).scalar()


def editor_required(f):
    """Admin, or a contributor/owner of at least one department. Department-
    specific checks happen inside the route via _require_edit."""
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
    return [d for d in depts
            if d.owner_id == current_user.id
            or any(u.id == current_user.id for u in d.contributors)]


def user_owns_department(user, dept):
    """Department owners (with admins) manage the department's quiz and see
    its stats. ``dept`` is a SopDepartment."""
    if not user or not user.is_authenticated or not dept:
        return False
    return user.is_admin or dept.owner_id == user.id


def user_can_verify(user, dept, brand=None):
    """Who may verify (approve) SOPs. Admins always can; otherwise it depends
    on the admin Configuration screen: the department owner (default) or the
    one specific user designated there."""
    if not user or not user.is_authenticated or not dept:
        return False
    if user.is_admin:
        return True
    from administration.models import AppSetting
    brand = brand or _brand()
    if AppSetting.get(brand, 'sop_approver_mode', 'owner') == 'user':
        uid = AppSetting.get(brand, 'sop_approver_user_id')
        return bool(uid) and str(user.id) == str(uid)
    return dept.owner_id == user.id


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

    dept_file_groups = _group_by_folder(dept.attachments)
    dept_folders = sorted({a.folder for a in dept.attachments if a.folder},
                          key=str.lower)

    return render_template('help/department.html', dept=dept, tree=tree,
                           orphans=orphans, departments=_departments(brand),
                           can_manage_quiz=can_manage_quiz,
                           active_quizzes=active_quizzes,
                           admin_quizzes=admin_quizzes,
                           ai_ok=_ai_configured(),
                           dept_file_groups=dept_file_groups,
                           dept_folders=dept_folders,
                           can_manage_files=_can_manage_dept_files(current_user, dept),
                           storage_ok=storage.is_configured())


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

    can_verify = user_can_verify(current_user, dept, brand)
    versions = []
    readers = []
    if can_edit or can_verify:
        versions = (SopVersion.query.filter_by(article_id=art.id)
                    .order_by(SopVersion.version_no.desc()).all())
    if current_user.is_admin:  # Lectures tab is admin-only
        readers = _reader_status(art, current_vno)

    return render_template('help/article.html', art=art, dept=dept, tree=tree,
                           orphans=orphans, prev_art=prev_art, next_art=next_art,
                           versions=versions, readers=readers,
                           can_verify=can_verify,
                           current_vno=current_vno, my_ack=my_ack,
                           ack_current=ack_current,
                           file_groups=_group_by_folder(art.attachments),
                           file_folders=sorted(
                               {a.folder for a in art.attachments if a.folder},
                               key=str.lower),
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
def mark_reviewed(art_id):
    """Verify a SOP — reserved to the configured approver (department owner
    by default, or the user designated in the admin Configuration) and admins. The
    verification is stamped on the current version, so the Versions tab shows
    exactly which state was checked and by whom."""
    brand = _brand()
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    if not user_can_verify(current_user, _get_department(art.department, brand), brand):
        abort(403)
    # Make sure there is a version to stamp (articles older than versioning
    # get their current state recorded as v1).
    if not SopVersion.query.filter_by(article_id=art.id).count():
        _snapshot(art, None)
        db.session.flush()
    latest = (SopVersion.query.filter_by(article_id=art.id)
              .order_by(SopVersion.version_no.desc()).first())
    latest.verified_at = datetime.utcnow()
    latest.verified_by_id = current_user.id
    art.last_reviewed_at = latest.verified_at
    art.last_reviewed_by_id = current_user.id
    art.review_due = date.today() + timedelta(days=365)
    db.session.commit()
    flash(f'SOP vérifié (version v{latest.version_no}) — prochaine revue dans '
          '12 mois.', 'success')
    nxt = request.form.get('next') or ''
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect(url_for('help.article', slug=art.slug))


# --- Change & validation queues ---

def _verifiable_departments(brand):
    """Departments whose SOP changes the current user may validate."""
    return [d for d in _departments(brand)
            if user_can_verify(current_user, d, brand)]


@help_bp.route('/queue')
@login_required
def queue():
    """File de validation + historique des modifications. Approvers see what
    awaits them (pending moderated changes, unverified latest versions);
    contributors follow the status of their own changes."""
    brand = _brand()
    editable = _editable_departments(brand)
    verifiable = _verifiable_departments(brand)
    if not (editable or verifiable):
        abort(403)
    visible = {d.slug for d in editable} | {d.slug for d in verifiable}
    verifiable_slugs = {d.slug for d in verifiable}

    pending = (SopPendingChange.query.join(HelpArticle)
               .filter(HelpArticle.brand == brand,
                       HelpArticle.department.in_(visible),
                       SopPendingChange.status == 'pending')
               .order_by(SopPendingChange.created_at.asc()).all())

    # 'Publish now, verify after' flow: articles whose latest version awaits
    # its verification stamp.
    unverified = []
    for art in (HelpArticle.query
                .filter(HelpArticle.brand == brand,
                        HelpArticle.department.in_(visible)).all()):
        last = (SopVersion.query.filter_by(article_id=art.id)
                .order_by(SopVersion.version_no.desc()).first())
        if last and not last.verified_at:
            unverified.append((art, last))
    unverified.sort(key=lambda p: p[1].created_at or datetime.min, reverse=True)

    history = (SopVersion.query.join(HelpArticle)
               .filter(HelpArticle.brand == brand,
                       HelpArticle.department.in_(visible))
               .order_by(SopVersion.created_at.desc()).limit(50).all())
    reviewed = (SopPendingChange.query.join(HelpArticle)
                .filter(HelpArticle.brand == brand,
                        HelpArticle.department.in_(visible),
                        SopPendingChange.status != 'pending')
                .order_by(SopPendingChange.reviewed_at.desc()).limit(20).all())
    return render_template('help/queue.html', pending=pending,
                           unverified=unverified, history=history,
                           reviewed=reviewed,
                           dept_names={d.slug: d.name for d in _departments(brand)},
                           verifiable_slugs=verifiable_slugs,
                           publish_mode=_publish_mode(brand))


def _notify_submitter(ch, approved):
    """Tell the author of a pending change how it was reviewed."""
    from auth.email_sender import send_email
    if not (ch.submitted_by and ch.submitted_by.email
            and ch.submitted_by_id != current_user.id):
        return
    link = (request.url_root.rstrip('/')
            + url_for('help.article', slug=ch.article.slug))
    if approved:
        subject = f'SOP publié : {ch.title}'
        body = (f"Bonjour,\n\nVotre modification de « {ch.title} » a été "
                f"approuvée et publiée par {current_user.display_name}.\n"
                f"{link}\n\n— Espace SOP")
    else:
        note = f"\nMotif : {ch.review_note}" if ch.review_note else ''
        subject = f'SOP refusé : {ch.title}'
        body = (f"Bonjour,\n\nVotre modification de « {ch.title} » a été "
                f"refusée par {current_user.display_name}.{note}\n"
                f"{link}\n\n— Espace SOP")
    try:
        send_email([ch.submitted_by.email], subject, body, brand_id=_brand())
    except Exception:
        import logging
        logging.getLogger(__name__).exception('SOP review notification failed')


def _get_pending_change(change_id, brand):
    ch = db.session.get(SopPendingChange, change_id)
    if not ch or ch.article.brand != brand:
        abort(404)
    return ch


@help_bp.route('/queue/change/<int:change_id>')
@login_required
def queue_change(change_id):
    """Diff of a proposed change (current article vs proposal), with the
    approve/reject actions for approvers."""
    brand = _brand()
    ch = _get_pending_change(change_id, brand)
    dept = _get_department(ch.article.department, brand)
    if not (user_can_edit(current_user, brand, ch.article.department)
            or user_can_verify(current_user, dept, brand)):
        abort(403)
    old_text = html_to_text(ch.article.body_html) if ch.kind == 'update' else ''
    new_text = html_to_text(ch.body_html)
    segments = _diff_segments(re.sub(r'\s+', ' ', old_text).strip(),
                              re.sub(r'\s+', ' ', new_text).strip())
    changed = any(op in ('del', 'ins') for op, _ in segments)
    return render_template('help/queue_change.html', ch=ch, dept=dept,
                           segments=segments, changed=changed,
                           can_review=user_can_verify(current_user, dept, brand))


@help_bp.route('/queue/change/<int:change_id>/approve', methods=['POST'])
@login_required
def queue_change_approve(change_id):
    brand = _brand()
    ch = _get_pending_change(change_id, brand)
    art = ch.article
    if not user_can_verify(current_user,
                           _get_department(art.department, brand), brand):
        abort(403)
    if ch.status != 'pending':
        flash('Cette modification a déjà été traitée.', 'info')
        return redirect(url_for('help.queue'))
    # Apply the proposal, version it as the submitter's edit, publish, and
    # stamp the new version verified by the approver.
    has_versions = SopVersion.query.filter_by(article_id=art.id).count() > 0
    pre_edit = {'title': art.title, 'category': art.category,
                'body_html': art.body_html}
    art.title = ch.title
    art.category = ch.category
    art.body_html = ch.body_html
    art.search_text = re.sub(
        r'\s+', ' ', f'{ch.title} {html_to_text(ch.body_html)}').strip()[:60000]
    art.is_published = True
    if not has_versions and ch.kind == 'update':
        _snapshot(art, None, baseline=pre_edit)
        db.session.flush()
    _snapshot(art, ch.submitted_by_id)
    db.session.flush()
    latest = (SopVersion.query.filter_by(article_id=art.id)
              .order_by(SopVersion.version_no.desc()).first())
    latest.verified_at = datetime.utcnow()
    latest.verified_by_id = current_user.id
    art.last_reviewed_at = latest.verified_at
    art.last_reviewed_by_id = current_user.id
    art.review_due = date.today() + timedelta(days=365)
    ch.status = 'approved'
    ch.reviewed_by_id = current_user.id
    ch.reviewed_at = datetime.utcnow()
    db.session.commit()
    _notify_submitter(ch, approved=True)
    flash(f'Modification approuvée et publiée (v{latest.version_no}).',
          'success')
    return redirect(url_for('help.queue'))


@help_bp.route('/queue/change/<int:change_id>/reject', methods=['POST'])
@login_required
def queue_change_reject(change_id):
    brand = _brand()
    ch = _get_pending_change(change_id, brand)
    if not user_can_verify(current_user,
                           _get_department(ch.article.department, brand), brand):
        abort(403)
    if ch.status != 'pending':
        flash('Cette modification a déjà été traitée.', 'info')
        return redirect(url_for('help.queue'))
    ch.status = 'rejected'
    ch.reviewed_by_id = current_user.id
    ch.reviewed_at = datetime.utcnow()
    ch.review_note = (request.form.get('note') or '').strip()[:300] or None
    db.session.commit()
    _notify_submitter(ch, approved=False)
    flash('Modification refusée.', 'info')
    return redirect(url_for('help.queue'))


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
    """Candidate SOP owners: admins + the department's contributors and owner."""
    from auth.models import User
    out = {u.id: u for u in User.query.filter_by(role='admin').all()}
    dept = _get_department(dept_slug, brand)
    if dept:
        for u in dept.contributors:
            out[u.id] = u
        if dept.owner:
            out[dept.owner.id] = dept.owner
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
    brand_users = sorted(_brand_users(brand), key=lambda u: u.display_name.lower())
    return render_template('help/departments.html', departments=depts,
                           counts=counts, brand_users=brand_users)


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
    dept = SopDepartment(brand=brand, slug=slug, name=name, icon=icon,
                         sort_order=nxt)
    raw_owner = (request.form.get('owner_id') or '').strip()
    if raw_owner.isdigit():
        dept.owner_id = int(raw_owner)
    ids = {int(x) for x in request.form.getlist('contributor_ids')
           if str(x).isdigit()}
    if ids:
        from auth.models import User
        dept.contributors = User.query.filter(User.id.in_(ids)).all()
    db.session.add(dept)
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
    if 'owner_id' in request.form:
        raw = request.form.get('owner_id')
        try:
            dept.owner_id = int(raw) if raw else None
        except (ValueError, TypeError):
            pass
    if 'contributors_present' in request.form:
        from auth.models import User
        ids = {int(x) for x in request.form.getlist('contributor_ids')
               if str(x).isdigit()}
        dept.contributors = (User.query.filter(User.id.in_(ids)).all()
                             if ids else [])
    db.session.commit()
    flash('Département mis à jour.', 'success')
    return redirect(url_for('help.departments_manage'))


@help_bp.route('/departments/reorder', methods=['POST'])
@login_required
@admin_required
def departments_reorder():
    """Persist the drag-and-drop order of the departments list. Body:
    {"ids": [dept_id, ...]} in display order."""
    brand = _brand()
    ids = (request.get_json(silent=True) or {}).get('ids') or []
    depts = {d.id: d for d in SopDepartment.query.filter_by(brand=brand).all()}
    pos = 0
    for raw in ids:
        d = depts.get(int(raw)) if str(raw).isdigit() else None
        if d:
            d.sort_order = pos
            pos += 1
    db.session.commit()
    return jsonify(ok=True)


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


def _publish_mode(brand):
    """'immediate' (publish now, verify after — default) or 'moderated'
    (contributor edits wait in the validation queue). Admin Configuration."""
    from administration.models import AppSetting
    return AppSetting.get(brand, 'sop_publish_mode', 'immediate')


def _must_moderate(dept_slug, brand):
    """True when the current user's edits go through the validation queue:
    moderated publish mode and the user is not an approver (admins and the
    department's approver always publish directly)."""
    if _publish_mode(brand) != 'moderated':
        return False
    return not user_can_verify(current_user, _get_department(dept_slug, brand),
                               brand)


def _change_watchers(dept_slug, brand, exclude_id=None):
    """The department's validation chain, notified of SOP changes: the brand's
    designated approver (when Configuration names one), the department owner,
    and the department's contributors."""
    from administration.models import AppSetting
    from auth.models import User
    watchers = {}
    if AppSetting.get(brand, 'sop_approver_mode', 'owner') == 'user':
        uid = AppSetting.get(brand, 'sop_approver_user_id') or ''
        if str(uid).isdigit():
            u = db.session.get(User, int(uid))
            if u:
                watchers[u.id] = u
    dept = _get_department(dept_slug, brand)
    if dept:
        if dept.owner:
            watchers[dept.owner.id] = dept.owner
        for u in dept.contributors:
            watchers[u.id] = u
    watchers.pop(exclude_id, None)
    return [u.email for u in watchers.values() if u.email]


def _notify_change_watchers(art, editor, *, pending_change=None):
    """Email the validation chain that a SOP changed — or, with
    ``pending_change``, that a proposal awaits validation in the queue."""
    from auth.email_sender import send_email
    brand = _brand()
    recipients = _change_watchers(art.department, brand, exclude_id=editor.id)
    if not recipients:
        return 0
    if pending_change is not None:
        link = (request.url_root.rstrip('/')
                + url_for('help.queue_change', change_id=pending_change.id))
        subject = f'SOP à valider : {pending_change.title}'
        body = (f"Bonjour,\n\n"
                f"Une modification de « {pending_change.title} » proposée par "
                f"{editor.display_name} attend votre validation :\n{link}\n\n"
                f"— Espace SOP")
    else:
        link = request.url_root.rstrip('/') + url_for('help.article', slug=art.slug)
        subject = f'SOP modifié : {art.title}'
        body = (f"Bonjour,\n\n"
                f"La procédure « {art.title} » ({art.category}) a été modifiée "
                f"par {editor.display_name}.\n{link}\n\n"
                f"— Espace SOP")
    try:
        return send_email(recipients, subject, body, brand_id=brand)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('SOP watcher notification failed')
        return 0


def _submit_pending(art, *, kind, title, category, body_html):
    """Queue a proposed change for validation instead of applying it, and
    alert the department's validation chain."""
    change = SopPendingChange(
        article_id=art.id, kind=kind, title=title, category=category,
        body_html=body_html, submitted_by_id=current_user.id)
    db.session.add(change)
    db.session.commit()
    _notify_change_watchers(art, current_user, pending_change=change)


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
    moderated = _must_moderate(art.department, brand)
    if moderated:
        # Created unpublished; the validation queue publishes it on approval.
        art.is_published = False
    art.slug = _unique_slug(art.title)
    db.session.add(art)
    db.session.flush()
    _snapshot(art, current_user.id)
    if moderated:
        _submit_pending(art, kind='create', title=art.title,
                        category=art.category, body_html=art.body_html)
        flash('SOP soumis pour validation — il sera publié après approbation.',
              'info')
        return redirect(url_for('help.article', slug=art.slug))
    db.session.commit()
    _notify_change_watchers(art, current_user)
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
    if _must_moderate(art.department, brand):
        # The article is untouched; the proposal waits in the validation queue.
        title = (request.form.get('title') or '').strip()
        category = (request.form.get('category') or 'Général').strip() or 'Général'
        if not title:
            flash('Le titre est requis.', 'warning')
            return redirect(url_for('help.edit', art_id=art_id))
        _submit_pending(art, kind='update', title=title, category=category,
                        body_html=request.form.get('body_html') or '')
        flash('Modification soumise pour validation — elle sera publiée après '
              'approbation.', 'info')
        return redirect(url_for('help.article', slug=art.slug))
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
    _notify_change_watchers(art, current_user)
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
    if _must_moderate(art.department, brand):
        _submit_pending(art, kind='update', title=art.title,
                        category=art.category, body_html=cleaned)
        flash('Nettoyage soumis pour validation.', 'info')
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
DEPT_ATTACHMENT_PREFIX = 'sops/dept-attachments/'


def _attachment_folder_from_form():
    """Optional flat folder name typed/picked on the upload form."""
    folder = re.sub(r'\s+', ' ', request.form.get('folder') or '').strip()
    return folder[:160] or None


def _group_by_folder(attachments):
    """[(folder_or_None, [attachments])] — root files first, then folders in
    alphabetical order. Feeds the Fichiers panels."""
    groups = {}
    for att in attachments:
        groups.setdefault(att.folder or None, []).append(att)
    root = [(None, groups.pop(None))] if None in groups else []
    return root + sorted(groups.items(), key=lambda kv: kv[0].lower())


def _can_manage_dept_files(user, dept):
    """Department files are managed by the department's editors, its owner and
    admins."""
    return (user_can_edit(user, dept.brand, dept.slug)
            or user_owns_department(user, dept))


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
            s3_key=key, size=len(data), folder=_attachment_folder_from_form(),
            uploaded_by_id=current_user.id))
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


# --- Department attachments (documents not tied to one SOP) ---

@help_bp.route('/d/<dept_slug>/attachments', methods=['POST'])
@login_required
def dept_attachment_upload(dept_slug):
    brand = _brand()
    dept = _get_department(dept_slug, brand)
    if not dept:
        abort(404)
    if not _can_manage_dept_files(current_user, dept):
        abort(403)
    back = url_for('help.department', dept_slug=dept.slug) + '#fichiers'
    if not storage.is_configured():
        flash("Le stockage S3 n'est pas configuré.", 'warning')
        return redirect(back)
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        flash('Aucun fichier sélectionné.', 'warning')
        return redirect(back)
    saved = 0
    for f in files:
        data = f.read()
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'bin'
        key = f'{DEPT_ATTACHMENT_PREFIX}{dept.id}/{uuid.uuid4().hex}.{ext}'
        try:
            storage.put_object(key, data, f.mimetype or 'application/octet-stream')
        except Exception as e:
            flash(f"Échec de l'envoi de {f.filename} : {e}", 'danger')
            continue
        db.session.add(SopDeptAttachment(
            department_id=dept.id, filename=f.filename,
            content_type=f.mimetype or 'application/octet-stream',
            s3_key=key, size=len(data), folder=_attachment_folder_from_form(),
            uploaded_by_id=current_user.id))
        saved += 1
    db.session.commit()
    if saved:
        flash(f'{saved} fichier(s) ajouté(s).', 'success')
    return redirect(back)


@help_bp.route('/d/attachments/<int:att_id>/download')
@login_required
def dept_attachment_download(att_id):
    att = db.session.get(SopDeptAttachment, att_id)
    if not att or att.department.brand != _brand():
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


@help_bp.route('/d/attachments/<int:att_id>/delete', methods=['POST'])
@login_required
def dept_attachment_delete(att_id):
    att = db.session.get(SopDeptAttachment, att_id)
    if not att or att.department.brand != _brand():
        abort(404)
    if not _can_manage_dept_files(current_user, att.department):
        abort(403)
    dept_slug = att.department.slug
    storage.delete_object(att.s3_key)
    db.session.delete(att)
    db.session.commit()
    flash('Fichier supprimé.', 'success')
    return redirect(url_for('help.department', dept_slug=dept_slug) + '#fichiers')


# --- Versions ---

def _get_version(art_id, version_no, brand):
    art = HelpArticle.query.filter_by(id=art_id, brand=brand).first()
    if not art:
        abort(404)
    # Contributors of the department, the department owner, and admins.
    if not (user_can_edit(current_user, brand, art.department)
            or user_owns_department(current_user,
                                    _get_department(art.department, brand))):
        abort(403)
    ver = SopVersion.query.filter_by(article_id=art.id,
                                     version_no=version_no).first()
    if not ver:
        abort(404)
    return art, ver


@help_bp.route('/<int:art_id>/version/<int:version_no>')
@login_required
def version_view(art_id, version_no):
    art, ver = _get_version(art_id, version_no, _brand())
    is_latest = ver.version_no == _current_version_no(art)
    return render_template('help/version_view.html', art=art, ver=ver,
                           is_latest=is_latest,
                           can_restore=user_can_edit(current_user, _brand(),
                                                     art.department),
                           dept=_get_department(art.department))


@help_bp.route('/<int:art_id>/version/<int:version_no>/restore', methods=['POST'])
@login_required
def version_restore(art_id, version_no):
    art, ver = _get_version(art_id, version_no, _brand())
    _require_edit(art.department)  # restoring rewrites content: editors only
    if ver.version_no == _current_version_no(art):
        flash('Cette version est déjà la version actuelle.', 'info')
        return redirect(url_for('help.article', slug=art.slug) + '#versions')
    if _must_moderate(art.department, _brand()):
        _submit_pending(art, kind='update', title=ver.title,
                        category=ver.category, body_html=ver.body_html)
        flash(f'Restauration de la v{ver.version_no} soumise pour validation.',
              'info')
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

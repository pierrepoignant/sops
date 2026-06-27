from functools import wraps
from datetime import datetime, timedelta
from flask import render_template, jsonify, request, abort, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import func, case
from init_db import db
from auth.models import User, UserVisit, ROLES
from administration import administration_bp
from administration.models import Group, GroupModule
from administration.permissions import MODULES, user_modules


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# --- Users ---

@administration_bp.route('/users')
@login_required
@admin_required
def admin_users():
    groups = Group.query.order_by(Group.name).all()
    return render_template('administration/admin_users.html', groups=groups, roles=ROLES)


@administration_bp.route('/users/api/data')
@login_required
@admin_required
def admin_users_data():
    users = User.query.order_by(
        case((User.last_login.is_(None), 1), else_=0),
        User.last_login.desc(),
    ).all()
    return jsonify(users=[
        {
            'id': u.id,
            'username': u.username,
            'email': u.email,
            'display_name': u.display_name,
            'role': u.role,
            'is_admin': u.is_admin,
            'created_at': u.created_at.strftime('%d/%m/%Y %H:%M') if u.created_at else '-',
            'last_login': u.last_login.strftime('%d/%m/%Y %H:%M') if u.last_login else '-',
            'oauth_provider': u.oauth_provider or '-',
            'groups': [g.name for g in u.groups],
        }
        for u in users
    ])


@administration_bp.route('/users/create', methods=['POST'])
@login_required
@admin_required
def admin_user_create():
    data = request.get_json(silent=True) or request.form
    email = (data.get('email') or '').strip().lower()
    first_name = (data.get('first_name') or '').strip() or None
    last_name = (data.get('last_name') or '').strip() or None
    role = (data.get('role') or 'staff').strip().lower()

    if not email or '@' not in email:
        return jsonify(error='Email invalide.'), 400
    if role not in ROLES:
        return jsonify(error=f'Rôle invalide. Choix: {", ".join(ROLES)}.'), 400
    if User.query.filter(func.lower(User.email) == email).first():
        return jsonify(error='Un utilisateur avec cet email existe déjà.'), 400

    base = email.split('@', 1)[0].replace(' ', '').lower() or 'user'
    username = base
    suffix = 1
    while User.query.filter_by(username=username).first():
        suffix += 1
        username = f'{base}{suffix}'

    user = User(
        username=username,
        email=email,
        first_name=first_name,
        last_name=last_name,
        role=role,
    )
    db.session.add(user)
    db.session.flush()

    raw_ids = data.get('group_ids')
    if isinstance(raw_ids, str):
        ids = {int(x) for x in raw_ids.split(',') if x.strip().isdigit()}
    else:
        ids = {int(x) for x in (raw_ids or []) if str(x).isdigit()}
    if ids:
        user.groups = Group.query.filter(Group.id.in_(ids)).all()

    db.session.commit()
    return jsonify(ok=True, user={'id': user.id, 'email': user.email,
                                   'username': user.username,
                                   'display_name': user.display_name}), 201


@administration_bp.route('/users/<int:user_id>/set-role', methods=['POST'])
@login_required
@admin_required
def admin_set_role(user_id):
    if user_id == current_user.id:
        return jsonify(error='Vous ne pouvez pas modifier votre propre rôle'), 400
    user = db.session.get(User, user_id)
    if not user:
        return jsonify(error='Utilisateur introuvable'), 404
    data = request.get_json(silent=True) or request.form
    role = (data.get('role') or '').strip().lower()
    if role not in ROLES:
        return jsonify(error=f'Rôle invalide. Choix: {", ".join(ROLES)}.'), 400
    user.role = role
    db.session.commit()
    return jsonify(ok=True, role=user.role, is_admin=user.is_admin)


@administration_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_user_delete(user_id):
    if user_id == current_user.id:
        return jsonify(error='Vous ne pouvez pas supprimer votre propre compte'), 400
    user = db.session.get(User, user_id)
    if not user:
        return jsonify(error='Utilisateur introuvable'), 404
    # Cascade-delete the user's visits manually (no ondelete set on UserVisit FK).
    UserVisit.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    return jsonify(ok=True)


@administration_bp.route('/users/<int:user_id>/groups', methods=['GET'])
@login_required
@admin_required
def user_groups_get(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify(error='Utilisateur introuvable'), 404
    all_groups = Group.query.order_by(Group.name).all()
    member_ids = {g.id for g in user.groups}
    return jsonify(
        user={'id': user.id, 'display_name': user.display_name, 'email': user.email},
        groups=[{'id': g.id, 'name': g.name, 'description': g.description,
                 'is_member': g.id in member_ids} for g in all_groups],
    )


@administration_bp.route('/users/<int:user_id>/groups', methods=['POST'])
@login_required
@admin_required
def user_groups_save(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify(error='Utilisateur introuvable'), 404
    data = request.get_json() or request.form
    raw = data.get('group_ids')
    if isinstance(raw, str):
        ids = {int(x) for x in raw.split(',') if x.strip().isdigit()}
    else:
        ids = {int(x) for x in (raw or []) if str(x).isdigit()}
    user.groups = Group.query.filter(Group.id.in_(ids)).all() if ids else []
    db.session.commit()
    return jsonify(ok=True, groups=[g.name for g in user.groups])


# --- Visits dashboard ---

@administration_bp.route('/visits')
@login_required
@admin_required
def admin_dashboard():
    return render_template('administration/admin_dashboard.html')


@administration_bp.route('/visits/api/data')
@login_required
@admin_required
def admin_dashboard_data():
    today = datetime.utcnow().date()
    seven_days_ago = today - timedelta(days=6)

    daily_rows = db.session.query(
        func.date(UserVisit.visited_at).label('day'),
        func.count(UserVisit.id).label('cnt'),
    ).filter(
        func.date(UserVisit.visited_at) >= seven_days_ago,
    ).group_by(
        func.date(UserVisit.visited_at),
    ).order_by(
        func.date(UserVisit.visited_at),
    ).all()

    daily_map = {str(r.day): r.cnt for r in daily_rows}
    daily_labels = []
    daily_values = []
    for i in range(7):
        d = seven_days_ago + timedelta(days=i)
        daily_labels.append(d.strftime('%d/%m'))
        daily_values.append(daily_map.get(str(d), 0))

    user_rows_raw = db.session.query(
        User,
        func.count(UserVisit.id).label('cnt'),
    ).join(User, UserVisit.user_id == User.id).filter(
        func.date(UserVisit.visited_at) >= seven_days_ago,
    ).group_by(
        UserVisit.user_id,
    ).order_by(
        func.count(UserVisit.id).desc(),
    ).all()

    user_labels = [r[0].display_name for r in user_rows_raw]
    user_values = [r.cnt for r in user_rows_raw]

    return jsonify(
        daily_labels=daily_labels,
        daily_values=daily_values,
        user_labels=user_labels,
        user_values=user_values,
    )


# --- Groups ---

@administration_bp.route('/groups')
@login_required
@admin_required
def groups_list():
    groups = Group.query.order_by(Group.name).all()
    return render_template('administration/groups.html', groups=groups, modules=MODULES)


@administration_bp.route('/groups/new', methods=['POST'])
@login_required
@admin_required
def groups_create():
    name = (request.form.get('name') or '').strip()
    description = (request.form.get('description') or '').strip() or None
    if not name:
        flash('Nom requis.', 'warning')
        return redirect(url_for('administration.groups_list'))
    if Group.query.filter_by(name=name).first():
        flash('Ce groupe existe déjà.', 'warning')
        return redirect(url_for('administration.groups_list'))
    g = Group(name=name, description=description)
    db.session.add(g)
    db.session.commit()
    flash('Groupe créé.', 'success')
    return redirect(url_for('administration.groups_edit', group_id=g.id))


@administration_bp.route('/groups/<int:group_id>', methods=['GET'])
@login_required
@admin_required
def groups_edit(group_id):
    group = db.session.get(Group, group_id)
    if not group:
        abort(404)
    users = User.query.order_by(User.first_name, User.username).all()
    current_module_ids = {gm.module_id for gm in group.modules}
    member_ids = {u.id for u in group.members}
    return render_template('administration/group_edit.html',
                           group=group,
                           modules=MODULES,
                           users=users,
                           current_module_ids=current_module_ids,
                           member_ids=member_ids)


@administration_bp.route('/groups/<int:group_id>/update', methods=['POST'])
@login_required
@admin_required
def groups_update(group_id):
    group = db.session.get(Group, group_id)
    if not group:
        abort(404)
    group.name = (request.form.get('name') or group.name).strip()
    group.description = (request.form.get('description') or '').strip() or None

    selected_modules = set(request.form.getlist('modules'))
    valid_modules = selected_modules & set(MODULES.keys())
    current = {gm.module_id: gm for gm in group.modules}
    for mid in valid_modules - set(current.keys()):
        db.session.add(GroupModule(group_id=group.id, module_id=mid))
    for mid in set(current.keys()) - valid_modules:
        db.session.delete(current[mid])

    selected_users = {int(u) for u in request.form.getlist('members') if u.isdigit()}
    group.members = User.query.filter(User.id.in_(selected_users)).all() if selected_users else []

    db.session.commit()
    flash('Groupe mis à jour.', 'success')
    return redirect(url_for('administration.groups_edit', group_id=group.id))


@administration_bp.route('/groups/<int:group_id>/delete', methods=['POST'])
@login_required
@admin_required
def groups_delete(group_id):
    group = db.session.get(Group, group_id)
    if not group:
        abort(404)
    db.session.delete(group)
    db.session.commit()
    flash('Groupe supprimé.', 'success')
    return redirect(url_for('administration.groups_list'))


@administration_bp.route('/me/modules')
@login_required
def my_modules():
    return jsonify(modules=sorted(user_modules(current_user)))

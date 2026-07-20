from flask import Flask, render_template, redirect, url_for, jsonify, g, request
from flask_login import LoginManager, current_user
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import BuildError
from init_db import db
from init_cache import cache
from config import initialize_config
import os
from datetime import datetime

from brands import brand_for_host, get_brand, BRANDS


def _drop_legacy_tables():
    """Drop tables whose shape changed incompatibly, so create_all() can
    recreate them. Runs BEFORE create_all(). Only the short-lived article-level
    quiz tables (2026-07-07, replaced the same day by department-level ones)
    are handled; they carried no production data."""
    from sqlalchemy import inspect as sqla_inspect, text

    insp = sqla_inspect(db.engine)
    tables = insp.get_table_names()
    if 'sop_quizzes' in tables:
        cols = {c['name'] for c in insp.get_columns('sop_quizzes')}
        # Two short-lived shapes preceded the multi-quiz one: article-level
        # ('article_id') and single-quiz-per-department ('is_open').
        if 'article_id' in cols or 'is_open' in cols:
            for t in ('sop_quiz_attempts', 'sop_quiz_questions', 'sop_quizzes'):
                if t in tables:
                    db.session.execute(text(f'DROP TABLE {t}'))
            db.session.commit()
    # sop_editors (2026-07-07, same-day replacement): superseded by the
    # 'contributor' role + users.department allocation.
    if 'sop_editors' in tables:
        db.session.execute(text('DROP TABLE sop_editors'))
        db.session.commit()


def _upgrade_schema():
    """Additive schema upgrades for existing tables. db.create_all() creates
    missing tables but never adds columns, so columns introduced after launch
    are ALTERed in here (idempotent; SQLite + MySQL)."""
    from sqlalchemy import inspect as sqla_inspect, text

    wanted = {
        'help_articles': {
            'owner_id': 'INTEGER',
            'review_due': 'DATE',
            'last_reviewed_at': 'DATETIME',
            'last_reviewed_by_id': 'INTEGER',
        },
        'sop_departments': {
            'owner_id': 'INTEGER',
        },
        'users': {
            'department': 'VARCHAR(80)',
        },
        'sop_versions': {
            'verified_at': 'DATETIME',
            'verified_by_id': 'INTEGER',
        },
        'media_assets': {
            'brand': 'VARCHAR(40)',
            'updated_at': 'DATETIME',
        },
        'sop_attachments': {
            'folder': 'VARCHAR(160)',
        },
    }
    insp = sqla_inspect(db.engine)
    for table, columns in wanted.items():
        if table not in insp.get_table_names():
            continue
        existing = {c['name'] for c in insp.get_columns(table)}
        for col, ddl in columns.items():
            if col not in existing:
                db.session.execute(
                    text(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}'))
                if table == 'media_assets' and col == 'brand':
                    # Everything uploaded before brand scoping (incl. the
                    # seeded Sablésienne manual) belongs to sablesienne.
                    db.session.execute(text(
                        "UPDATE media_assets SET brand = 'sablesienne' "
                        'WHERE brand IS NULL'))
    db.session.commit()

    # 2026-07: the global 'contributor' role became per-department contributor
    # lists. Seed each old contributor into the list of the department(s)
    # matching their allocation, then fold them into 'staff'. Idempotent: no
    # contributor-role users remain after the first run.
    from auth.models import User
    from help.models import SopDepartment, sop_department_contributors
    legacy = User.query.filter_by(role='contributor').all()
    for user in legacy:
        if user.department:
            for dept in SopDepartment.query.filter_by(slug=user.department).all():
                if user not in dept.contributors:
                    dept.contributors.append(user)
        user.role = 'staff'
    if legacy:
        db.session.commit()


def create_app(db_name='ovh', redis_server='localhost'):
    from dotenv import load_dotenv
    load_dotenv()

    app = Flask(__name__, instance_relative_config=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'dev-change-me-in-production'),
        PREFERRED_URL_SCHEME='https',
    )

    # --- Cache (Redis in prod, SimpleCache locally) ---
    cache_type = os.environ.get('CACHE_TYPE', 'SimpleCache')
    app.config['CACHE_TYPE'] = cache_type
    if cache_type == 'RedisCache':
        app.config['CACHE_REDIS_HOST'] = os.environ.get('REDIS_HOST', redis_server)
        app.config['CACHE_REDIS_PORT'] = int(os.environ.get('REDIS_PORT', 6379))
        app.config['CACHE_REDIS_DB'] = 0
        app.config['CACHE_REDIS_URL'] = f"redis://{app.config['CACHE_REDIS_HOST']}:{app.config['CACHE_REDIS_PORT']}/0"
    cache.init_app(app)

    # --- Pull database-* sections out of the environment early ---
    from config.env_loader import get_env_var, ENV_VAR_KEYS
    for section, keys in ENV_VAR_KEYS.items():
        if not section.startswith('database-'):
            continue
        if section not in app.config:
            app.config[section] = {}
        for key in keys:
            env_val = get_env_var(section, key)
            if env_val is not None:
                app.config[section][key] = env_val

    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    # --- Database URI ---
    if db_name == 'sqlite':
        # Zero-config local development — no MySQL needed.
        app.config['SQLALCHEMY_DATABASE_URI'] = (
            'sqlite:///' + os.path.join(app.instance_path, 'sops.db')
        )
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    else:
        db_section = f'database-{db_name}'
        db_config = app.config.get(db_section, {})
        app.config['SQLALCHEMY_DATABASE_URI'] = (
            f"mysql+pymysql://{db_config['user']}:{db_config['password']}"
            f"@{db_config['host']}:{db_config['port']}/{db_config['name']}"
        )
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

        import ssl
        engine_options = {
            'pool_size': 10,
            'max_overflow': 10,
            'pool_recycle': 3600,
            'pool_pre_ping': True,
            'pool_timeout': 30,
        }
        if db_name != 'local':
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            engine_options['connect_args'] = {'ssl': ssl_ctx}
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = engine_options

    db.init_app(app)
    initialize_config(app)

    # Re-load every section now that config is initialized (OAuth, etc.).
    for section, keys in ENV_VAR_KEYS.items():
        if section not in app.config:
            app.config[section] = {}
        for key in keys:
            env_val = get_env_var(section, key)
            if env_val is not None:
                app.config[section][key] = env_val

    # --- Login ---
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'

    # --- Per-request brand (host-based). Must run before anything that needs it. ---
    @app.before_request
    def set_brand():
        g.brand = brand_for_host(request.host)

    # --- Blueprints ---
    from auth.models import User  # noqa: F401
    from auth import auth_bp, init_oauth
    app.register_blueprint(auth_bp)
    init_oauth(app)

    from administration import administration_bp
    app.register_blueprint(administration_bp)

    from media import media_bp
    app.register_blueprint(media_bp)

    from help import help_bp
    app.register_blueprint(help_bp)

    # --- Module access enforcement + template helpers ---
    from administration.permissions import (enforce_module_access,
                                            user_has_module_access, user_modules)
    app.before_request(enforce_module_access)

    @app.context_processor
    def inject_context():
        def safe_url_for(endpoint, fallback='#', **values):
            try:
                return url_for(endpoint, **values)
            except BuildError:
                return fallback

        brand_id = getattr(g, 'brand', None) or 'sablesienne'
        brand = get_brand(brand_id)

        # Departments for the active brand — drives the left sidebar menu.
        brand_departments = []
        try:
            if current_user.is_authenticated:
                from help.models import SopDepartment
                brand_departments = (
                    SopDepartment.query.filter_by(brand=brand_id)
                    .order_by(SopDepartment.sort_order, SopDepartment.name).all())
        except Exception:
            brand_departments = []

        def can_edit_sops(dept_slug=None):
            """True when the user may edit SOPs — in the given department, or
            in at least one department when dept_slug is None."""
            from help.routes import user_can_edit
            return user_can_edit(current_user, brand_id, dept_slug)

        def can_see_stats():
            """Admins and department owners get the Statistiques page."""
            from help.routes import owned_departments
            if not current_user.is_authenticated:
                return False
            return current_user.is_admin or bool(
                owned_departments(current_user, brand_id))

        return {
            'has_module': lambda mid: user_has_module_access(current_user, mid),
            'user_modules': lambda: user_modules(current_user),
            'can_edit_sops': can_edit_sops,
            'can_see_stats': can_see_stats,
            'safe_url_for': safe_url_for,
            'current_brand': brand_id,
            'brand': brand,
            'brand_name': brand['name'],
            'brand_logo': url_for('static', filename=f"brand/{brand_id}/logo.png"),
            'brand_departments': brand_departments,
        }

    with app.app_context():
        _drop_legacy_tables()
        db.create_all()
        _upgrade_schema()

    # Idempotent one-time seeding of the media library + SOP center.
    from help.seed import run_seed
    run_seed(app)

    @app.before_request
    def log_user_visit():
        from flask_login import current_user as cu
        from flask import request as req
        if cu.is_authenticated and req.endpoint and not req.endpoint.startswith('static'):
            from auth.models import UserVisit
            visit = UserVisit(
                user_id=cu.id,
                endpoint=req.endpoint,
                ip_address=req.remote_addr,
            )
            db.session.add(visit)
            db.session.commit()

    @app.route('/')
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        return redirect(url_for('home'))

    @app.route('/home')
    def home():
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        return render_template('home.html')

    @app.route('/healthz')
    def health_check():
        try:
            from sqlalchemy import text
            db.session.execute(text('SELECT 1'))
            return jsonify({
                'status': 'healthy',
                'timestamp': datetime.now().isoformat(),
            }), 200
        except Exception as e:
            return jsonify({
                'status': 'unhealthy',
                'error': str(e),
            }), 503

    @login_manager.user_loader
    def load_user(user_id):
        from auth.models import User
        return db.session.get(User, user_id)

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        try:
            db.session.remove()
        except Exception:
            pass

    return app

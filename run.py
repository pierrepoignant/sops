import argparse
import os
from __init__ import create_app

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SOP - Les Bonnes Choses')
    parser.add_argument('-d', '--db', help='Database name or environment (ovh | local | sqlite)', default='ovh')
    parser.add_argument('-p', '--port', help='Port', default=5008)
    parser.add_argument('-r', '--redis', help='Redis host', default='localhost')
    args = parser.parse_args()

    app = create_app(db_name=args.db, redis_server=args.redis)
    app.config['DB_NAME'] = args.db

    # Debug + the Werkzeug reloader are opt-in via FLASK_DEBUG (default off).
    # In production the reloader is disabled so the app runs as a single process
    # — two processes would race the one-time startup seeding — and the
    # interactive debugger is never exposed publicly.
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes', 'on')

    app.run(
        host='0.0.0.0',
        port=int(args.port),
        debug=debug,
        use_reloader=debug,
    )

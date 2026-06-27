import argparse
from __init__ import create_app

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SOP - Les Bonnes Choses')
    parser.add_argument('-d', '--db', help='Database name or environment (ovh | local | sqlite)', default='ovh')
    parser.add_argument('-p', '--port', help='Port', default=5008)
    parser.add_argument('-r', '--redis', help='Redis host', default='localhost')
    args = parser.parse_args()

    app = create_app(db_name=args.db, redis_server=args.redis)
    app.config['DB_NAME'] = args.db

    app.run(
        host='0.0.0.0',
        port=int(args.port),
        debug=True
    )

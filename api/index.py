import os
import sys

# Ensure project root is in python path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from urllib.parse import parse_qs
from app import app


class VercelPathFix:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        qs = parse_qs(environ.get('QUERY_STRING', ''))
        if 'path' in qs and qs['path']:
            target = qs['path'][0]
            if not target.startswith('/'):
                target = '/' + target
            while '//' in target:
                target = target.replace('//', '/')
            environ['PATH_INFO'] = target
        elif environ.get('PATH_INFO') in ('/api/index', '/api/index.py', '/api', ''):
            environ['PATH_INFO'] = '/'
        return self.wsgi_app(environ, start_response)


# Apply middleware for Vercel Serverless environment
app.wsgi_app = VercelPathFix(app.wsgi_app)
handler = app

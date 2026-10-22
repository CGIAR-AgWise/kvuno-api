"""Authentication middleware for protecting endpoints."""

import os
from functools import wraps

from flask import request, redirect


from app.api.user import get_current_user


def _is_html_request():
    accept = request.headers.get('Accept', '')
    return 'text/html' in accept


def _login_path():
    """Where an unauthenticated browser is sent.

    The UI is a separate React SPA, so this is a client-side route rather than
    a server-rendered page. Overridable because a deployment may mount the SPA
    under a prefix (e.g. /app/login) behind a reverse proxy.
    """
    return os.getenv('SPA_LOGIN_PATH', '/login')


def require_auth(f):
    """Decorator that requires a valid Bearer token in the Authorization header
    or a ``token`` cookie.  Browsers that lack a valid token are redirected to
    the SPA's login route; API clients receive a 401 JSON response."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if user is None:
            if _is_html_request():
                return redirect(f"{_login_path()}?next={request.path}")
            return {"error": "Authentication required"}, 401
        return f(*args, **kwargs)
    return decorated

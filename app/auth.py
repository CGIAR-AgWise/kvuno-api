"""Authentication middleware for protecting endpoints."""

from functools import wraps

from flask import request, redirect


from app.api.user import get_current_user


def _is_html_request():
    accept = request.headers.get('Accept', '')
    return 'text/html' in accept


def require_auth(f):
    """Decorator that requires a valid Bearer token in the Authorization header
    or a ``token`` cookie.  Browsers that lack a valid token are redirected to
    ``/ui/login``; API clients receive a 401 JSON response."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if user is None:
            if _is_html_request():
                return redirect(f"/ui/login?next={request.path}")
            return {"error": "Authentication required"}, 401
        return f(*args, **kwargs)
    return decorated

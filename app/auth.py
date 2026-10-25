"""Authentication middleware for protecting endpoints."""

from functools import wraps

from flask import request, redirect


from app.api.user import get_current_user
from app.config import get_settings


def _is_html_request():
    accept = request.headers.get('Accept', '')
    return 'text/html' in accept


def _login_path():
    """Where an unauthenticated browser is sent.

    The UI is a separate React SPA, so this is a client-side route rather than
    a server-rendered page. Overridable because a deployment may mount the SPA
    under a prefix (e.g. /app/login) behind a reverse proxy.
    """
    return get_settings().SPA_LOGIN_PATH


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


def require_admin(f):
    """Like require_auth, but the account must also be listed in ADMIN_EMAILS.

    Signed in but not an administrator is a 403, not a 401: the token is fine,
    the account just may not do this.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        from app.api.user import is_admin
        user = get_current_user()
        if user is None:
            return {"error": "Authentication required"}, 401
        if not is_admin(user):
            return {"error": "Administrator access required"}, 403
        return f(*args, **kwargs)
    return decorated

"""Account approval and roles, for administrators only.

An administrator is an account with role 'admin', or an "owner" listed in
ADMIN_EMAILS. Everyone else gets 403 here and never sees these screens.

New registrations start `pending` and can neither sign in nor use a token until
approved here. Approving sends the activation email; rejecting can email a
reason. Disabling revokes every token the account holds. Admins can promote an
approved account to admin or back to user. Owners cannot be changed from here,
and nobody can disable or demote themselves, so access cannot be lost by mistake.
"""
from flask import request
from flask_openapi3 import APIBlueprint, Tag
from pydantic import ValidationError
from sqlalchemy import func, or_

from app.api.user import (
    ROLE_USER, STATUS_APPROVED, STATUS_DISABLED, STATUS_PENDING, STATUS_REJECTED,
    _utcnow, _z, get_current_user, is_admin, is_owner,
)
from app.auth import require_admin
from app.config import API_PREFIX, API_VERSION
from app.dto.auth import (
    AdminReviewRequest, AdminRoleRequest, AdminUser, AdminUserList, AdminUserPath, AdminUserQuery,
)
from app.dto.pagination import get_pagination
from app.models.database_conn import MyDb
from app.models.kvuno import User, UserToken
from app.services import mailer
from app.utils.logging import get_logger

logger = get_logger(__name__)

__bp__ = "/admin"
url_prefix = API_PREFIX + API_VERSION + __bp__

tag = Tag(name='Admin', description="Account approval (administrators only)")
api = APIBlueprint(__bp__, __name__, url_prefix=url_prefix, abp_tags=[tag], abp_security=[{"jwt": []}])

# Which status each action may start from. Approving a rejected or disabled
# account is allowed (a mistake, or access restored); the rest is not.
_TRANSITIONS = {
    'approve': ({STATUS_PENDING, STATUS_REJECTED, STATUS_DISABLED}, STATUS_APPROVED),
    'reject': ({STATUS_PENDING}, STATUS_REJECTED),
    'disable': ({STATUS_APPROVED}, STATUS_DISABLED),
}


def _to_admin_user(user: User) -> AdminUser:
    return AdminUser(
        id=user.id, username=user.username, email=user.email,
        status=user.status or STATUS_APPROVED, role=user.role or ROLE_USER,
        is_admin=is_admin(user), is_owner=is_owner(user),
        created_at=_z(user.created_at) if user.created_at else None,
        reviewed_at=_z(user.reviewed_at) if user.reviewed_at else None,
        reviewed_by=user.reviewed_by,
    )


@api.get('/users',
         responses={200: AdminUserList, 401: {"description": "Authentication required"},
                    403: {"description": "Administrator access required"}},
         summary="List accounts for review",
         description="Newest first. Filter with `status` (pending, approved, rejected, disabled) and "
                     "`q` (username or email). `counts` gives the total per status.")
@require_admin
def list_users(query: AdminUserQuery):
    session = MyDb.get_db().session
    base = session.query(User)
    if query.q:
        like = f"%{query.q.strip()}%"
        base = base.filter(or_(User.username.ilike(like), User.email.ilike(like)))

    counts = {s: 0 for s in (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_DISABLED)}
    for status, n in base.with_entities(User.status, func.count()).group_by(User.status).all():
        counts[status or STATUS_APPROVED] = counts.get(status or STATUS_APPROVED, 0) + n

    listed = base.filter(User.status == query.status) if query.status else base
    pagination = get_pagination()
    total = listed.count()
    users = (listed.order_by(User.created_at.desc(), User.id.desc())
             .limit(pagination.per_page).offset(pagination.offset).all())
    return AdminUserList(users=[_to_admin_user(u) for u in users], counts=counts,
                         **pagination.envelope(total)).model_dump(), 200


def _review(user_id: int, action: str):
    try:
        review = AdminReviewRequest.model_validate(request.get_json(silent=True) or {})
    except ValidationError as e:
        return {"error": e.errors()[0].get('msg', 'invalid request')}, 422

    session = MyDb.get_db().session
    actor = get_current_user()
    user = session.get(User, user_id)
    if user is None:
        return {"error": "User not found"}, 404
    if is_owner(user):
        # Their access comes from ADMIN_EMAILS, which no status change here can
        # remove — refusing makes that explicit instead of silently doing nothing.
        return {"error": "Owners are set by ADMIN_EMAILS and cannot be changed here"}, 400
    if actor is not None and actor.id == user.id:
        return {"error": "You cannot change your own account here"}, 400

    allowed_from, new_status = _TRANSITIONS[action]
    current = user.status or STATUS_APPROVED
    if current not in allowed_from:
        return {"error": f"Cannot {action} an account that is {current}"}, 409

    user.status = new_status
    user.reviewed_at = _utcnow()
    user.reviewed_by = actor.id if actor else None
    revoked = 0
    if new_status != STATUS_APPROVED:
        # Tokens already stop working (is_active), but deleting them keeps a
        # later re-approval from reviving sessions the user forgot about.
        revoked = session.query(UserToken).filter(UserToken.user_id == user.id).delete()
    session.commit()
    # IDs only — no usernames or email addresses in the logs.
    logger.info("account status changed", user_id=user.id, from_status=current,
                to_status=new_status, actor_id=actor.id if actor else None, tokens_revoked=revoked)

    emailed = False
    if review.notify:
        if new_status == STATUS_APPROVED:
            emailed = mailer.send_account_activated(user.email, user.username, user_id=user.id)
        elif new_status == STATUS_REJECTED:
            emailed = mailer.send_account_rejected(user.email, user.username, review.reason, user_id=user.id)
    return {"user": _to_admin_user(user).model_dump(), "emailed": emailed}, 200


@api.post('/users/<int:user_id>/approve',
          responses={200: {"description": "Approved; activation email sent when configured"},
                     404: {"description": "User not found"}, 409: {"description": "Not pending/rejected/disabled"}},
          summary="Approve (activate) an account",
          description="Body (optional): `{\"notify\": true}` — set false to skip the activation email.")
@require_admin
def approve_user(path: AdminUserPath):
    return _review(path.user_id, 'approve')


@api.post('/users/<int:user_id>/reject',
          responses={200: {"description": "Rejected"}, 404: {"description": "User not found"},
                     409: {"description": "Not pending"}},
          summary="Reject a pending account",
          description="Body (optional): `{\"reason\": \"...\", \"notify\": true}` — the reason is "
                      "included in the email to the user.")
@require_admin
def reject_user(path: AdminUserPath):
    return _review(path.user_id, 'reject')


@api.post('/users/<int:user_id>/disable',
          responses={200: {"description": "Disabled; all its tokens revoked"},
                     404: {"description": "User not found"}, 409: {"description": "Not approved"}},
          summary="Disable an approved account",
          description="Blocks sign-in and revokes every token the account holds. Approve it again to restore.")
@require_admin
def disable_user(path: AdminUserPath):
    return _review(path.user_id, 'disable')


@api.post('/users/<int:user_id>/role',
          responses={200: {"description": "Role changed"}, 400: {"description": "Owner or yourself"},
                     404: {"description": "User not found"}, 409: {"description": "Account is not approved"}},
          summary="Make an account an admin, or a regular user",
          description="Body: `{\"role\": \"admin\"}` or `{\"role\": \"user\"}`. Only approved accounts; "
                      "owners (ADMIN_EMAILS) and your own account cannot be changed.")
@require_admin
def set_role(path: AdminUserPath, body: AdminRoleRequest):
    session = MyDb.get_db().session
    actor = get_current_user()
    user = session.get(User, path.user_id)
    if user is None:
        return {"error": "User not found"}, 404
    if is_owner(user):
        return {"error": "Owners are set by ADMIN_EMAILS and cannot be changed here"}, 400
    if actor is not None and actor.id == user.id:
        # Demoting yourself is the classic way to leave nobody able to undo it.
        return {"error": "You cannot change your own role"}, 400
    if (user.status or STATUS_APPROVED) != STATUS_APPROVED:
        return {"error": "Only approved accounts can be given a role"}, 409

    previous = user.role or ROLE_USER
    user.role = body.role
    user.reviewed_at = _utcnow()
    user.reviewed_by = actor.id if actor else None
    session.commit()
    logger.info("account role changed", user_id=user.id, from_role=previous, to_role=body.role,
                actor_id=actor.id if actor else None)
    return {"user": _to_admin_user(user).model_dump()}, 200
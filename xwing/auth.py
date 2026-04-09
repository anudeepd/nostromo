from fastapi import HTTPException, Request

from .config import Settings


def get_user(request: Request, settings: Settings) -> str:
    user = request.headers.get(settings.user_header)
    if user is None:
        if settings.require_auth:
            raise HTTPException(status_code=403, detail="Authentication required")
        return "anonymous"
    return user.lower()


def require_perm(user: str, perm: str, settings: Settings) -> None:
    if not getattr(settings.perms_for(user), perm):
        raise HTTPException(status_code=403, detail=f"{perm.capitalize()} permission denied")

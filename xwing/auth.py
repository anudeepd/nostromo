from fastapi import HTTPException, Request

from .config import Settings


def get_user(request: Request, settings: Settings) -> str:
    state_user = getattr(request.state, "user", None)
    if state_user:
        return str(state_user).lower()

    user = request.headers.get(settings.user_header)
    if user is None:
        if settings.require_auth:
            raise HTTPException(status_code=403, detail="Authentication required")
        return "anonymous"
    client_ip = request.client.host if request.client else ""
    if not settings.is_trusted_auth_proxy(client_ip):
        if settings.require_auth:
            raise HTTPException(
                status_code=403, detail="Untrusted authentication header"
            )
        return "anonymous"
    return user.lower()


def require_perm(user: str, perm: str, settings: Settings) -> None:
    if not getattr(settings.perms_for(user), perm):
        raise HTTPException(
            status_code=403, detail=f"{perm.capitalize()} permission denied"
        )

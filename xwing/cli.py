"""CLI entry point for X-wing."""

import logging
import os
from pathlib import Path

import click
import uvicorn

from .config import Settings
from . import audit_store


@click.group()
@click.version_option(package_name="xwing")
def main():
    """X-wing — simple file sharing server with WebDAV support."""


def _configure_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


@main.command()
@click.option(
    "--audit-db",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="SQLite audit database path (also enables auditing).",
)
@click.option(
    "--log-file",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Append application logs to this file.",
)
@click.option(
    "--root",
    required=True,
    type=click.Path(exists=True, file_okay=False),
    help="Root directory to serve.",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
@click.option("--port", default=8989, show_default=True, help="Bind port.")
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    show_default=True,
    help="Open browser on startup.",
)
@click.option(
    "--max-upload-gb",
    default=None,
    type=float,
    help="Max upload size in GB [default: 10].",
)
@click.option(
    "--max-chunk-mb",
    default=None,
    type=int,
    help="Max chunk size in MB [default: 100].",
)
@click.option(
    "--max-chunks",
    default=None,
    type=int,
    help="Max number of chunks per upload [default: 10000].",
)
@click.option(
    "--session-ttl-minutes",
    default=None,
    type=int,
    help="Session TTL in minutes [default: 60].",
)
@click.option(
    "--require-auth",
    is_flag=True,
    default=False,
    help="Require authentication header (403 if missing).",
)
@click.option(
    "--users-config",
    "users_config",
    default=None,
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    help="Path to YAML file with per-user read/write/delete permissions. "
    "Unlisted users are denied unless '*' is configured.",
)
@click.option(
    "--admin-user",
    "admin_users",
    multiple=True,
    help="LDAP username allowed to use admin console. Repeat for multiple users.",
)
@click.option("--admin-users", "legacy_admin_users", default=None, hidden=True)
@click.option("--read-users", default=None, hidden=True)
@click.option("--write-users", default=None, hidden=True)
@click.option(
    "--user-header",
    default=None,
    help="Header to read username from [default: X-Forwarded-User].",
)
@click.option(
    "--trusted-auth-proxy",
    "trusted_auth_proxies",
    multiple=True,
    help="Trusted proxy IP/CIDR allowed to supply --user-header. "
    "Repeat for multiple proxies. Required for standalone LDAPGate proxy mode.",
)
@click.option(
    "--reload", is_flag=True, default=False, help="Auto-reload on code changes (dev)."
)
@click.option(
    "--ldap-config",
    "ldap_config",
    default=None,
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    help="Path to ldapgate YAML config to enable LDAP authentication.",
)
def serve(
    root,
    host,
    port,
    open_browser,
    max_upload_gb,
    max_chunk_mb,
    max_chunks,
    session_ttl_minutes,
    require_auth,
    users_config,
    read_users,
    write_users,
    legacy_admin_users,
    admin_users,
    user_header,
    trusted_auth_proxies,
    reload,
    ldap_config,
    audit_db,
    log_file,
):
    """Start the X-wing web server."""
    for flag, val in (
        ("--read-users", read_users),
        ("--write-users", write_users),
        ("--admin-users", legacy_admin_users),
    ):
        if val is not None:
            replacement = (
                "--admin-user" if flag == "--admin-users" else "--users-config"
            )
            raise click.UsageError(
                f"{flag} was removed in 0.2.2. Use {replacement} instead."
            )
    _configure_logging(log_file)

    url = f"http://{host}:{port}"

    if ldap_config:
        os.environ["XWING_LDAP_CONFIG"] = ldap_config
        click.echo(f"LDAP authentication enabled ({ldap_config})")
    if audit_db:
        os.environ["XWING_AUDIT_DB"] = str(audit_db)
    if log_file:
        os.environ["XWING_LOG_FILE"] = str(log_file)

    click.echo(f"Starting X-wing at {url}")
    if not users_config:
        click.echo(
            "WARNING: No --users-config provided — all users are read-only.",
            err=True,
        )

    if open_browser:
        import threading
        import time
        import webbrowser

        def _open():
            time.sleep(1.5)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    kwargs = {
        "root_dir": root,
        "require_auth": require_auth,
        "listen_host": host,
        "listen_port": port,
    }
    if max_upload_gb is not None:
        kwargs["max_upload_bytes"] = int(max_upload_gb * 1024**3)
    if max_chunk_mb is not None:
        kwargs["max_chunk_bytes"] = max_chunk_mb * 1024**2
    if max_chunks is not None:
        kwargs["max_chunks"] = max_chunks
    if session_ttl_minutes is not None:
        kwargs["session_ttl_seconds"] = session_ttl_minutes * 60
    if user_header:
        kwargs["user_header"] = user_header
    if trusted_auth_proxies:
        kwargs["trusted_auth_proxies"] = list(trusted_auth_proxies)
    if users_config is not None:
        kwargs["users_config"] = users_config
    if ldap_config is not None:
        kwargs["ldap_config"] = ldap_config
    if admin_users:
        kwargs["admin_users"] = list(admin_users)
    if audit_db is not None:
        kwargs["audit_db"] = audit_db

    if reload:
        os.environ["XWING_ROOT"] = root
        os.environ["XWING_REQUIRE_AUTH"] = str(require_auth)
        os.environ["XWING_LISTEN_HOST"] = host
        os.environ["XWING_LISTEN_PORT"] = str(port)
        if max_upload_gb is not None:
            os.environ["XWING_MAX_UPLOAD_GB"] = str(max_upload_gb)
        if max_chunk_mb is not None:
            os.environ["XWING_MAX_CHUNK_MB"] = str(max_chunk_mb)
        if max_chunks is not None:
            os.environ["XWING_MAX_CHUNKS"] = str(max_chunks)
        if session_ttl_minutes is not None:
            os.environ["XWING_SESSION_TTL_MINUTES"] = str(session_ttl_minutes)
        if users_config is not None:
            os.environ["XWING_USERS_CONFIG"] = users_config
        if user_header:
            os.environ["XWING_USER_HEADER"] = user_header
        if admin_users:
            os.environ["XWING_ADMIN_USERS"] = ",".join(admin_users)
        if trusted_auth_proxies:
            os.environ["XWING_TRUSTED_AUTH_PROXIES"] = ",".join(trusted_auth_proxies)
        if ldap_config is not None:
            os.environ["XWING_LDAP_CONFIG"] = ldap_config
        if audit_db is not None:
            os.environ["XWING_AUDIT_DB"] = str(audit_db)
        if log_file is not None:
            os.environ["XWING_LOG_FILE"] = str(log_file)

        uvicorn.run(
            "xwing.app:create_app_reload",
            host=host,
            port=port,
            factory=True,
            reload=reload,
            log_level="info",
            log_config=None,
        )
    else:
        settings = Settings(**kwargs)
        from .app import create_app

        app = create_app(settings)
        uvicorn.run(
            app,
            host=host,
            port=port,
            reload=False,
            log_level="info",
            log_config=None,
        )


def _audit_db_path(value: Path | None) -> Path:
    if value:
        return value
    configured = os.getenv("XWING_AUDIT_DB")
    if configured:
        return Path(configured).expanduser()
    data_home = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / "xwing" / "audit.db"


def _print_audit_events(events: list[dict]) -> None:
    for event in events:
        click.echo(
            f"{event['occurred_at']} {event['username']} {event['method']} {event['path']} status={event['status_code']} {event['duration_ms']}ms"
        )
        if event["details"]:
            click.echo(f"  {event['details']}")


@main.group(invoke_without_command=True)
@click.option(
    "--audit-db", type=click.Path(dir_okay=False, path_type=Path), default=None
)
@click.option("--user", "username", default=None)
@click.option("--since", default=None, help="ISO date/time, for example 2026-06-21.")
@click.option("--limit", default=100, show_default=True, type=click.IntRange(1, 10000))
@click.pass_context
def audit(
    ctx: click.Context,
    audit_db: Path | None,
    username: str | None,
    since: str | None,
    limit: int,
):
    """Read or purge authenticated X-wing activity."""
    if ctx.invoked_subcommand is None:
        db_path = _audit_db_path(audit_db)
        audit_store.init_db(db_path)
        _print_audit_events(
            audit_store.list_events(
                db_path, username=username, since=since, limit=limit
            )
        )


@audit.command("purge")
@click.option(
    "--audit-db", type=click.Path(dir_okay=False, path_type=Path), default=None
)
@click.option("--older-than", default=90, show_default=True, type=click.IntRange(1))
def audit_purge(audit_db: Path | None, older_than: int):
    """Purge old audit rows."""
    db_path = _audit_db_path(audit_db)
    audit_store.init_db(db_path)
    click.echo(f"Purged {audit_store.purge_events(db_path, older_than)} audit events")

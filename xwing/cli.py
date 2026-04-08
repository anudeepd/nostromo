"""CLI entry point for Nostromo."""

import logging
import os

import click
import uvicorn

from .config import Settings


@click.group()
@click.version_option(package_name="xwing")
def main():
    """Nostromo — simple file sharing server with WebDAV support."""


@main.command()
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
    "--require-auth",
    is_flag=True,
    default=False,
    help="Require X-Forwarded-User header (403 if missing).",
)
@click.option(
    "--user-header",
    default=None,
    help="Header to read username from [default: X-Forwarded-User].",
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
    require_auth,
    user_header,
    reload,
    ldap_config,
):
    """Start the Nostromo web server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    url = f"http://{host}:{port}"

    if ldap_config:
        os.environ["XWING_LDAP_CONFIG"] = ldap_config
        click.echo(f"LDAP authentication enabled ({ldap_config})")

    click.echo(f"Starting Nostromo at {url}")

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
    if user_header:
        kwargs["user_header"] = user_header

    if reload:
        os.environ["XWING_ROOT"] = root
        os.environ["XWING_REQUIRE_AUTH"] = str(require_auth)
        os.environ["XWING_LISTEN_HOST"] = host
        os.environ["XWING_LISTEN_PORT"] = str(port)
        if max_upload_gb is not None:
            os.environ["XWING_MAX_UPLOAD_GB"] = str(max_upload_gb)
        if user_header:
            os.environ["XWING_USER_HEADER"] = user_header

        uvicorn.run(
            "xwing.app:create_app_reload",
            host=host,
            port=port,
            factory=True,
            reload=reload,
            log_level="info",
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
        )

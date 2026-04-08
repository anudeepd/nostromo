"""WebDAV method handlers (PROPFIND, MKCOL, COPY, MOVE, LOCK, UNLOCK)."""

import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import anyio
from fastapi import Request, Response

DAV_NS = "DAV:"

ET.register_namespace("D", DAV_NS)


def _dav(tag: str) -> str:
    return f"{{{DAV_NS}}}{tag}"


def _prop_response(href: str, path: Path) -> ET.Element:
    response = ET.Element(_dav("response"))
    ET.SubElement(response, _dav("href")).text = href

    propstat = ET.SubElement(response, _dav("propstat"))
    prop = ET.SubElement(propstat, _dav("prop"))

    if path.is_dir():
        ET.SubElement(prop, _dav("resourcetype")).append(ET.Element(_dav("collection")))
        ET.SubElement(prop, _dav("getcontenttype")).text = "httpd/unix-directory"
        ET.SubElement(prop, _dav("getcontentlength")).text = "0"
    else:
        ET.SubElement(prop, _dav("resourcetype"))
        ET.SubElement(prop, _dav("getcontenttype")).text = "application/octet-stream"
        ET.SubElement(prop, _dav("getcontentlength")).text = str(path.stat().st_size)

    try:
        mtime = path.stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        ET.SubElement(prop, _dav("getlastmodified")).text = dt.strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
    except OSError:
        pass

    ET.SubElement(propstat, _dav("status")).text = "HTTP/1.1 200 OK"
    return response


def propfind_response(request: Request, path: Path, root: Path) -> Response:
    depth = request.headers.get("depth", "1")

    # Reject Depth: infinity — not supported, per RFC 4918 §9.1
    if depth == "infinity":
        return Response(status_code=403, content="Depth: infinity not supported")

    rel = "/" + str(path.relative_to(root)).replace("\\", "/")
    if path.is_dir() and not rel.endswith("/"):
        rel += "/"

    multistatus = ET.Element(_dav("multistatus"))
    multistatus.append(_prop_response(rel, path))

    if depth != "0" and path.is_dir():
        for child in sorted(path.iterdir()):
            child_rel = rel + child.name
            if child.is_dir():
                child_rel += "/"
            multistatus.append(_prop_response(child_rel, child))

    xml_bytes = ET.tostring(multistatus, encoding="utf-8", xml_declaration=True)
    return Response(
        content=xml_bytes,
        status_code=207,
        media_type="application/xml; charset=utf-8",
        headers={"DAV": "1, 2"},
    )


def mkcol_response(path: Path) -> Response:
    if path.exists():
        return Response(status_code=405, content="Already exists")
    try:
        path.mkdir(parents=False)
    except FileNotFoundError:
        return Response(status_code=409, content="Parent does not exist")
    return Response(status_code=201)


def _clear_destination(dest: Path, overwrite: bool) -> Response | None:
    """Remove dest if it exists and overwrite is True; return error Response otherwise.

    Note: synchronous — callers must wrap in run_sync if called from async context.
    """
    if dest.exists():
        if not overwrite:
            return Response(status_code=412, content="Destination exists")
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    return None


async def copy_response(src: Path, dest: Path, overwrite: bool) -> Response:
    if not src.exists():
        return Response(status_code=404)
    if (
        err := await anyio.to_thread.run_sync(_clear_destination, dest, overwrite)  # type: ignore[reportAttributeAccessIssue]
    ) is not None:
        return err
    if src.is_dir():
        await anyio.to_thread.run_sync(lambda: shutil.copytree(src, dest))  # type: ignore[reportAttributeAccessIssue]
    else:
        await anyio.to_thread.run_sync(lambda: shutil.copy2(src, dest))  # type: ignore[reportAttributeAccessIssue]
    return Response(status_code=201)


async def move_response(src: Path, dest: Path, overwrite: bool) -> Response:
    if not src.exists():
        return Response(status_code=404)
    if (
        err := await anyio.to_thread.run_sync(_clear_destination, dest, overwrite)  # type: ignore[reportAttributeAccessIssue]
    ) is not None:
        return err
    await anyio.to_thread.run_sync(lambda: shutil.move(str(src), dest))  # type: ignore[reportAttributeAccessIssue]
    return Response(status_code=201)


def lock_response(path: Path) -> Response:
    """LOCK is not implemented — return 501 so clients fall back gracefully."""
    return Response(status_code=501, content="LOCK not implemented")


def unlock_response() -> Response:
    return Response(status_code=204)

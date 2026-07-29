"""WebDAV method handlers (PROPFIND, MKCOL, COPY, MOVE, LOCK, UNLOCK)."""

import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

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


def _href_for_path(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    if rel == Path("."):
        return "/"
    href = "/" + "/".join(quote(part, safe="") for part in rel.parts)
    if path.is_dir():
        href += "/"
    return href


def propfind_response(request: Request, path: Path, root: Path) -> Response:
    depth_header = request.headers.get("depth", "1")

    # Sanitize depth header - only accept "0", "1", or "infinity"
    if depth_header not in ("0", "1", "infinity"):
        depth_header = "1"

    # Reject Depth: infinity — not supported, per RFC 4918 §9.1
    if depth_header == "infinity":
        return Response(status_code=403, content="Depth: infinity not supported")

    rel = _href_for_path(path, root)

    multistatus = ET.Element(_dav("multistatus"))
    multistatus.append(_prop_response(rel, path))

    if depth_header != "0" and path.is_dir():
        for child in sorted(path.iterdir()):
            multistatus.append(_prop_response(_href_for_path(child, root), child))

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


def _cleanup_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _unique_hidden_path(parent: Path, name: str, suffix: str) -> Path:
    return parent / f".{name}.{uuid.uuid4().hex}{suffix}"


def _install_staged_path(staged: Path, dest: Path) -> None:
    backup = None
    try:
        if dest.exists():
            backup = _unique_hidden_path(dest.parent, dest.name, ".bak")
            dest.replace(backup)
        try:
            staged.replace(dest)
        except OSError:
            shutil.move(str(staged), str(dest))
    except Exception:
        if backup is not None and backup.exists():
            if dest.exists():
                _cleanup_path(dest)
            backup.replace(dest)
        raise
    finally:
        if backup is not None and backup.exists():
            _cleanup_path(backup)


async def copy_response(src: Path, dest: Path, overwrite: bool) -> Response:
    if not src.exists():
        return Response(status_code=404)
    if dest.exists():
        if not overwrite:
            return Response(status_code=412, content="Destination exists")

    # Copy to a unique temp path first, then rename into place.
    if src.is_dir():
        temp_dest = Path(
            tempfile.mkdtemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
        )
        shutil.rmtree(temp_dest)
    else:
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=f".{dest.name}.",
            suffix=".tmp",
            dir=dest.parent,
            delete=False,
        )
        temp_dest = Path(temp_handle.name)
        temp_handle.close()
    try:
        if src.is_dir():
            await anyio.to_thread.run_sync(
                lambda: shutil.copytree(src, temp_dest, symlinks=True)
            )  # type: ignore[reportAttributeAccessIssue]
        else:
            await anyio.to_thread.run_sync(lambda: shutil.copy2(src, temp_dest))  # type: ignore[reportAttributeAccessIssue]
        await anyio.to_thread.run_sync(_install_staged_path, temp_dest, dest)  # type: ignore[reportAttributeAccessIssue]
    except OSError:
        try:
            _cleanup_path(temp_dest)
        except Exception:
            pass
        return Response(status_code=500, content="Copy failed")
    return Response(status_code=201)


async def move_response(src: Path, dest: Path, overwrite: bool) -> Response:
    if not src.exists():
        return Response(status_code=404)
    if dest.exists():
        if not overwrite:
            return Response(status_code=412, content="Destination exists")

    try:
        await anyio.to_thread.run_sync(_install_staged_path, src, dest)  # type: ignore[reportAttributeAccessIssue]
    except OSError:
        return Response(status_code=500, content="Move failed")
    return Response(status_code=201)


def lock_response(path: Path) -> Response:
    """Return a minimal exclusive write lock response for Finder-style clients."""
    token = f"opaquelocktoken:{uuid.uuid4()}"
    prop = ET.Element(_dav("prop"))
    lockdiscovery = ET.SubElement(prop, _dav("lockdiscovery"))
    activelock = ET.SubElement(lockdiscovery, _dav("activelock"))
    ET.SubElement(activelock, _dav("locktype")).append(ET.Element(_dav("write")))
    ET.SubElement(activelock, _dav("lockscope")).append(ET.Element(_dav("exclusive")))
    ET.SubElement(activelock, _dav("depth")).text = "infinity"
    ET.SubElement(activelock, _dav("timeout")).text = "Second-3600"
    locktoken = ET.SubElement(activelock, _dav("locktoken"))
    ET.SubElement(locktoken, _dav("href")).text = token
    xml_bytes = ET.tostring(prop, encoding="utf-8", xml_declaration=True)
    return Response(
        content=xml_bytes,
        status_code=200,
        media_type="application/xml; charset=utf-8",
        headers={
            "DAV": "1, 2",
            "Lock-Token": f"<{token}>",
        },
    )


def unlock_response() -> Response:
    return Response(status_code=204)

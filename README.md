<p align="center">
  <img src="https://raw.githubusercontent.com/anudeepd/xwing/main/assets/logo.svg" alt="X-wing" width="120"/>
</p>

<h1 align="center">X-wing</h1>

<p align="center">A self-contained file sharing server with WebDAV support. Works out of the box or integrates with LDAPGate for corporate LDAP/AD authentication.</p>

## Features

- **WebDAV server** — mount as a drive on Windows, macOS, and Linux using native WebDAV clients
- **Resumable uploads** — chunked uploads with session recovery; supports large files (up to 10 GB by default)
- **Browser-based file browser** — drag-and-drop upload, directory creation, zip download, file delete
- **In-browser text editor** — CodeMirror-powered editor for common text and code file types
- **WebDAV COPY / MOVE** — server-side file and directory copy/move via `Destination` header
- **Per-user access control** — YAML config grants each user independent `read`, `write`, and `delete` permissions; reloaded at runtime without restart
- **Optional LDAP / AD authentication** — via [LDAPGate](https://github.com/anudeepd/ldapgate)
- **Single self-contained wheel** — no external CDN dependencies; fonts embedded as base64 WOFF2

## Install

```bash
pip install xwing
```

For LDAP/AD authentication:

```bash
pip install 'xwing[ldap]'
```

## Usage

```bash
xwing serve --root /path/to/serve
```

Opens the file browser at `http://127.0.0.1:8989` and launches your default browser.

### Options

```
--root PATH                Root directory to serve. [required]
--host TEXT                Bind host. [default: 127.0.0.1]
--port INTEGER             Bind port. [default: 8989]
--open / --no-open         Open browser on startup. [default: open]
--max-upload-gb FLOAT      Max upload size in GB. [default: 10]
--max-chunk-mb INTEGER     Max size per chunk in MB. [default: 100]
--max-chunks INTEGER       Max chunks per upload session. [default: 10000]
--session-ttl-minutes INT  Upload session expiry in minutes. [default: 60]
--require-auth             Require authentication header (403 if missing).
--users-config FILE        Path to YAML file with per-user permissions.
--user-header TEXT         Header to read username from. [default: X-Forwarded-User]
--reload                   Auto-reload on code changes (dev only).
--ldap-config FILE         Path to LDAPGate YAML config to enable LDAP authentication.
```

### WebDAV Mount Examples

**Linux (DAVfs2):**
```bash
sudo mount.davfs http://localhost:8989 /mnt/xwing -o username=<user>
```

**macOS:**
```bash
open http://localhost:8989
# Or mount: Finder → Go → Connect to Server → http://localhost:8989
```

**Windows (native WebDAV):**
```
net use Z: \\localhost@8989\DavWWWRoot /persistent:yes
```

### Resumable Upload (Chunked)

For large files, use the chunked upload API:

```bash
# 1. Init session
curl -X POST http://localhost:8989/_upload/init \
  -H "Content-Type: application/json" \
  -d '{"filename": "big.iso", "total_chunks": 100, "dir": "/"}'

# 2. Upload each chunk
curl -X PUT http://localhost:8989/_upload/<session_id>/<chunk_index> \
  --data-binary @chunk.part

# 3. Complete
curl -X POST http://localhost:8989/_upload/<session_id>/complete
```

Chunk size and session limits are configurable via `--max-chunk-mb`, `--max-chunks`, and `--session-ttl-minutes`.

## Access Control

By default all users are **read-only**. Pass `--users-config` to grant write or delete access per user.

```bash
xwing serve --root /data --users-config users.yaml
```

**`users.yaml` — compact format:**

```yaml
users:
  alice: rwd    # read + write + delete
  bob: rw       # read + write, no delete
  charlie: r    # read only
  "*": r        # fallback for any unlisted user (omit to deny unlisted users)
```

**`users.yaml` — verbose format:**

```yaml
users:
  alice:
    read: true
    write: true
    delete: true
```

Verbose field defaults when omitted: `read: true`, `write: false`, `delete: false`. Values must be `true` or `false`.

Permission levels:

| Flag | Grants |
|------|--------|
| `r`  | Browse directories, download files (GET, HEAD, PROPFIND) |
| `w`  | Upload files, create directories, copy (PUT, MKCOL, COPY) |
| `d`  | Delete and move files (DELETE, MOVE) |

The config file is reloaded automatically when it changes on disk — no restart needed.

## LDAP / Active Directory Authentication

X-wing supports two modes for LDAP/AD auth:

**Mode 1 — Standalone proxy:** Run LDAPGate as a reverse proxy in front of xwing. Authenticated requests get an `X-Forwarded-User` header that xwing reads.

```
Browser → LDAPGate → xwing
```

```bash
ldapgate serve --config ldapgate.yaml
xwing serve --root /data --require-auth --users-config users.yaml
```

**Mode 2 — Built-in middleware:** Inject LDAPGate directly into xwing as FastAPI middleware:

```bash
pip install 'xwing[ldap]'
xwing serve --root /data --ldap-config ldapgate.yaml --users-config users.yaml
```

See the [LDAPGate README](https://github.com/anudeepd/ldapgate) for config file documentation.

## Development

Requires [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/anudeepd/xwing
cd xwing
uv sync
uv run xwing serve --root .
```

## License

MIT

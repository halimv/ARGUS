"""Same-origin bridge to ARGUS's own managed research service.

The destination is always the managed loopback server. Browser origins are
checked before forwarding, and only the proxied UI entry can be framed by ARGUS.
The independent research service retains its original security policy.
"""
from __future__ import annotations

import asyncio
from http.client import HTTPConnection, HTTPException as UpstreamHTTPError
from urllib.parse import quote, unquote, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

MAX_BODY = 2_000_000
FORWARDED_REQUEST_HEADERS = {"accept", "content-type", "range", "if-none-match", "if-modified-since", "last-event-id"}
FORWARDED_RESPONSE_HEADERS = {
    "content-type", "content-length", "cache-control", "x-content-type-options",
    "referrer-policy", "content-security-policy", "content-disposition",
    "content-range", "accept-ranges", "etag", "last-modified",
}


def _origin(value: str, *, allow_path=False):
    try:
        parsed = urlsplit(value)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or (not allow_path and (parsed.path or parsed.query or parsed.fragment))):
        return None
    return parsed.scheme, parsed.hostname.lower(), port


def validate_request(request: Request):
    hosts = request.headers.getlist("host")
    origin = _origin(request.url.scheme + "://" + hosts[0]) if len(hosts) == 1 else None
    if not origin or origin[1] not in {"127.0.0.1", "localhost"}:
        raise HTTPException(403, "Research requests require a local ARGUS host.")
    origins = request.headers.getlist("origin")
    if len(origins) > 1 or (origins and _origin(origins[0]) != origin):
        raise HTTPException(403, "Research requests must come from the same ARGUS origin.")
    referers = request.headers.getlist("referer")
    if len(referers) > 1 or (referers and _origin(referers[0], allow_path=True) != origin):
        raise HTTPException(403, "Research requests must come from the same ARGUS origin.")
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(403, "Cross-site research requests are refused.")


def target_path(request: Request):
    raw = request.scope.get("raw_path") or quote(request.url.path, safe="/%").encode("ascii")
    if not raw.startswith(b"/research/"):
        raise HTTPException(400, "Invalid research route.")
    try:
        path = "/" + raw[len(b"/research/"):].decode("ascii")
        decoded = unquote(path)
        query = request.scope.get("query_string", b"").decode("ascii")
    except UnicodeError as error:
        raise HTTPException(400, "Invalid research path encoding.") from error
    if (decoded.startswith("//") or "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/"))
            or any(character in path + query for character in "\r\n\x00")):
        raise HTTPException(400, "Invalid research path.")
    return path + ("?" + query if query else ""), decoded


async def _body(request: Request):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) < 0:
                raise ValueError
            if int(length) > MAX_BODY:
                raise HTTPException(413, "Research request exceeds 2 MB.")
        except ValueError as error:
            raise HTTPException(400, "Invalid request length.") from error
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise HTTPException(413, "Research request exceeds 2 MB.")
        chunks.append(chunk)
    return b"".join(chunks)


async def proxy_research(request: Request, workspace):
    validate_request(request)
    path, decoded = target_path(request)
    if workspace is None or workspace.server is None:
        raise HTTPException(503, "The research workspace is not running.")
    port = workspace.server.server_port
    body = await _body(request) if request.method in {"POST", "PATCH"} else None
    headers = {name: value for name, value in request.headers.items() if name.lower() in FORWARDED_REQUEST_HEADERS}
    # The caller's origin was validated above; the original server validates this
    # hop against its own Host. No caller-supplied forwarding headers are trusted.
    headers["Origin"] = f"http://127.0.0.1:{port}"
    connection = HTTPConnection("127.0.0.1", port, timeout=60)

    def open_upstream():
        connection.request(request.method, path, body=body, headers=headers)
        return connection.getresponse()

    try:
        response = await asyncio.to_thread(open_upstream)
    except (OSError, UpstreamHTTPError) as error:
        connection.close()
        raise HTTPException(502, "The managed research service did not respond.") from error
    forwarded = {name: value for name, value in response.getheaders() if name.lower() in FORWARDED_RESPONSE_HEADERS}
    if request.method == "GET" and decoded in {"/", "/index.html"} and response.status == 200:
        for name in list(forwarded):
            if name.lower() == "content-security-policy":
                forwarded[name] = forwarded[name].replace("frame-ancestors 'none'", "frame-ancestors 'self'")
    if response.getheader("Content-Type", "").startswith("text/event-stream"):
        forwarded["X-Accel-Buffering"] = "no"

    async def chunks():
        try:
            while True:
                # read1 returns available bytes promptly; read(n) can buffer SSE
                # until n bytes or EOF and hide progress for a long-running turn.
                chunk = await asyncio.to_thread(response.read1, 65536)
                if not chunk:
                    break
                yield chunk
        finally:
            response.close()
            connection.close()

    return StreamingResponse(chunks(), status_code=response.status, headers=forwarded)

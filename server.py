# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp==1.30.0", "httpx==0.28.1", "jsonschema>=4.20,<5"]
# ///
"""
Silo MCP server: exposes every operation in the Silo v2 OpenAPI spec to MCP clients.

The tool surface is generated from the live spec at startup, so new Silo endpoints
appear on restart with no code changes.

Modes (SILO_MODE):
  compact (default)  4 meta tools: search / describe / call / list tags. Reaches all operations
                     without putting ~800 tool schemas into the model's context.
  full               one MCP tool per operation, named by operationId. Narrow with SILO_TAGS.

See README.md for all environment variables.
"""
import asyncio
import base64
import contextlib
import fnmatch
import functools
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import jsonschema
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server


def log(*a):
    print("[silo-server-mcp]", *a, file=sys.stderr, flush=True)


BASE = os.getenv("SILO_BASE_URL", "").rstrip("/")
base_url = urlsplit(BASE)
if (base_url.scheme not in ("http", "https") or not base_url.hostname
        or base_url.username or base_url.password or base_url.query or base_url.fragment
        or base_url.path not in ("", "/")):
    raise SystemExit("Set SILO_BASE_URL to your Silo server root, e.g. https://silo.example.com")
TOKEN = os.getenv("SILO_TOKEN", "")
MODE = os.getenv("SILO_MODE", "compact").lower()
if MODE not in ("compact", "full"):
    raise SystemExit("SILO_MODE must be compact or full")
readonly = os.getenv("SILO_READONLY", "1").lower()
if readonly not in ("1", "true", "yes", "0", "false", "no"):
    raise SystemExit("SILO_READONLY must be 1 or 0 (true/false, yes/no also accepted)")
READONLY = readonly in ("1", "true", "yes")
TAGS = [t.strip() for t in os.getenv("SILO_TAGS", "").split(",") if t.strip()]
EXCLUDE_TAGS = [t.strip() for t in os.getenv("SILO_EXCLUDE_TAGS", "").split(",") if t.strip()]
TIMEOUT = float(os.getenv("SILO_TIMEOUT", "60"))
if not (math.isfinite(TIMEOUT) and TIMEOUT > 0):
    raise SystemExit("SILO_TIMEOUT must be positive and finite")
MAX_CHARS = 60000
SSE_SECONDS = 5
MAX_REF_DEPTH = 6
FILES_DIR = Path(os.getenv("SILO_FILES_DIR")).expanduser().resolve() if os.getenv("SILO_FILES_DIR") else None
MAX_FILE_BYTES = 64 * 1024 * 1024

DEFAULT_HEADERS = {}
if os.getenv("SILO_PROFILE_ID"):
    DEFAULT_HEADERS["X-Profile-Id"] = os.getenv("SILO_PROFILE_ID")
if os.getenv("SILO_PROFILE_TOKEN"):
    DEFAULT_HEADERS["X-Profile-Token"] = os.getenv("SILO_PROFILE_TOKEN")

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
RESERVED = {"body", "headers", "save_to"}


# --------------------------------------------------------------------------- spec

def load_spec():
    f = os.getenv("SILO_OPENAPI_FILE")
    if f:
        return json.loads(Path(f).read_text())
    url = os.getenv("SILO_OPENAPI_URL", BASE + "/api/v2/openapi.json")
    cache = Path(os.getenv("SILO_CACHE_DIR", str(Path.home() / ".cache" / "silo-server-mcp"))) / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    try:
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
            raise ValueError("spec must contain a paths object")
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(r.text)
        except OSError:
            pass
        return data
    except Exception as e:  # noqa: BLE001
        if cache.exists():
            log(f"could not fetch spec ({e}); using cached copy {cache}")
            return json.loads(cache.read_text())
        raise SystemExit(f"cannot load OpenAPI spec from {url}: {e}")


SPEC = load_spec()


def resolve_ref(ref):
    if not ref.startswith("#/"):
        raise ValueError("Only local OpenAPI references are supported")
    node = SPEC
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def shallow(node):
    """Resolve a top-level $ref only."""
    seen = set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen:
            raise ValueError(f"Circular OpenAPI reference: {ref}")
        seen.add(ref)
        node = resolve_ref(ref)
    return node


def deref(s, depth=0, stack=()):
    """Inline $refs (bounded) and drop noise so the schema is self-contained."""
    if isinstance(s, list):
        return [deref(x, depth, stack) for x in s]
    if not isinstance(s, dict):
        return s
    if "$ref" in s:
        ref = s["$ref"]
        if ref in stack or depth >= MAX_REF_DEPTH:
            return {"type": "object", "description": f"(nested {ref.split('/')[-1]}; see silo_describe_operation or the spec)"}
        extra = {k: v for k, v in s.items() if k != "$ref"}
        out = deref(resolve_ref(ref), depth + 1, stack + (ref,))
        if isinstance(out, dict) and extra:
            out = {**out, **deref(extra, depth, stack)}
        return out
    out = {}
    for k, v in s.items():
        if k in ("examples", "example", "$schema", "externalDocs", "xml", "discriminator") or k.startswith("x-"):
            continue
        if k in ("properties", "patternProperties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {pk: deref(pv, depth, stack) for pk, pv in v.items()}
        else:
            out[k] = deref(v, depth, stack)
    return out


class Op:
    def __init__(self, path, method, o, common_params):
        self.path = path
        self.method = method
        raw = o.get("operationId") or f"{method}_{re.sub(r'[^A-Za-z0-9]+', '_', path).strip('_')}"
        self.id = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64]
        self.tag = (o.get("tags") or ["untagged"])[0]
        self.summary = o.get("summary") or ""
        self.description = o.get("description") or ""
        params = {}
        for p in [*common_params, *o.get("parameters", [])]:
            p = shallow(p)
            params[(p["in"], p["name"])] = p
        self.path_params = [p for (loc, _), p in params.items() if loc == "path"]
        self.query_params = [p for (loc, _), p in params.items() if loc == "query"]
        self.header_params = [p for (loc, _), p in params.items() if loc == "header"]
        self.body_schema = None
        self.body_required = False
        self.body_ctype = None
        rb = shallow(o.get("requestBody") or {})
        content = rb.get("content") or {}
        if content:
            ctype = next((c for c in ("application/json", "multipart/form-data", "application/octet-stream") if c in content), next(iter(content)))
            self.body_ctype = ctype
            self.body_schema = content[ctype].get("schema", {})
            self.body_required = bool(rb.get("required"))
        self.response_types = set()
        for code, resp in (o.get("responses") or {}).items():
            if str(code).startswith("2"):
                self.response_types.update((shallow(resp).get("content") or {}).keys())
        self.public = not o.get("security", SPEC.get("security", []))
        self.argmap = {}
        params = self.path_params + self.query_params
        for p in params:
            key = p["name"]
            if key in RESERVED or sum(q["name"] == key for q in params) > 1:
                key = f"{p['in']}_{key}"
            while key in self.argmap or key in RESERVED:
                key = "param_" + key
            self.argmap[key] = (p["in"], p["name"], p)

    @property
    def read_only(self):
        return self.method in ("get", "head", "options")

    @functools.cached_property
    def input_schema(self):
        props, req = {}, []
        for key, (loc, name, p) in self.argmap.items():
            s = deref(p.get("schema", {}))
            if isinstance(s, dict) and p.get("description") and "description" not in s:
                s["description"] = p["description"]
            props[key] = s
            if p.get("required") or loc == "path":
                req.append(key)
        if self.body_schema is not None:
            b = deref(self.body_schema)
            if self.body_ctype == "multipart/form-data":
                b = {**b, "description": (b.get("description", "") + " multipart/form-data fields; for a file use {\"content_base64\": \"...\", \"filename\": \"x\", \"content_type\": \"...\"}").strip()}
                for name, schema in b.get("properties", {}).items():
                    if schema.get("format") == "binary":
                        b["properties"][name] = {"type": "object", "properties": {
                            "content_base64": {"type": "string"}, "filename": {"type": "string"},
                            "content_type": {"type": "string"},
                        }, "required": ["content_base64"], "additionalProperties": False}
            elif self.body_ctype == "application/octet-stream":
                b = {"oneOf": [{"type": "string"},
                    {"type": "object", "properties": {"base64": {"type": "string"}}, "required": ["base64"], "additionalProperties": False},
                    {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}],
                    "description": "Raw bytes: {\"base64\": \"...\"}, a plain string, or {\"path\": \"relative/file\"} within SILO_FILES_DIR (if enabled)."}
            props["body"] = b
            if self.body_required:
                req.append("body")
        props["headers"] = {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Optional extra request headers, e.g. X-Profile-Id, X-Profile-Token, If-Match, Range. "
            + ("Declared here: " + ", ".join(sorted({p['name'] for p in self.header_params if p['name'] != 'Authorization'})) if self.header_params else ""),
        }
        props["save_to"] = {"type": "string", "description": "Save to a new relative file within SILO_FILES_DIR (must be enabled). Never overwrites existing files; maximum 64 MiB."}
        return {"type": "object", "properties": props, "required": req, "additionalProperties": False}

    def describe(self):
        head = f"[{self.tag}] {self.method.upper()} {self.path}"
        text = "\n".join(x for x in (head, self.summary, self.description) if x)
        return text[:1500]


def build_ops():
    ops = {}
    for path, item in SPEC["paths"].items():
        if (not path.startswith("/") or path.startswith("//") or any(c in path for c in "?#\\")
                or any(p in (".", "..") for p in path.split("/"))):
            raise ValueError(f"Invalid OpenAPI path: {path}")
        item = shallow(item)
        common = item.get("parameters", [])
        for m in METHODS:
            o = item.get(m)
            if not o:
                continue
            op = Op(path, m, o, common)
            base, n = op.id, 2
            while op.id in ops:
                op.id = f"{base[:60]}_{n}"
                n += 1
            ops[op.id] = op
    return ops


ALL_OPS = build_ops()


def allowed(op):
    if READONLY and not op.read_only:
        return False
    if TAGS and not any(fnmatch.fnmatch(op.tag, t) for t in TAGS):
        return False
    if any(fnmatch.fnmatch(op.tag, t) for t in EXCLUDE_TAGS):
        return False
    return True


OPS = {k: v for k, v in ALL_OPS.items() if allowed(v)}


# --------------------------------------------------------------------------- execution

client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)


def local_file(value):
    if FILES_DIR is None:
        raise ValueError("Local file access is disabled; set SILO_FILES_DIR to enable it")
    p = Path(value)
    if p.is_absolute():
        raise ValueError("File paths must be relative to SILO_FILES_DIR")
    p = (FILES_DIR / p).resolve()
    if not p.is_relative_to(FILES_DIR) or p == FILES_DIR:
        raise ValueError("File path must stay within SILO_FILES_DIR")
    return p


def to_query(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return [to_query(x) for x in v]
    if isinstance(v, dict):
        return json.dumps(v)
    return v


def is_textual(ct):
    return (
        not ct
        or ct.startswith("text/")
        or ct.endswith("json")
        or ct.endswith("xml")
        or ct in ("application/javascript", "application/x-subrip", "application/vnd.apple.mpegurl")
    )


async def read_limited(r, limit):
    buf = bytearray()
    async for chunk in r.aiter_bytes(chunk_size=65536):
        remaining = limit - len(buf)
        buf.extend(chunk[:remaining])
        if len(chunk) > remaining:
            return bytes(buf), False
    return bytes(buf), True


def clip(text):
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n\n[truncated {len(text) - MAX_CHARS} characters; narrow the request with query parameters such as limit/offset/fields]"
    return text


async def render(r, save_to):
    # Error bodies must never create or replace a download.
    if not r.is_success:
        save_to = None
    ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
    meta = [f"HTTP {r.status_code} {r.reason_phrase}"]
    for h in ("etag", "location", "link", "content-range", "retry-after"):
        if h in r.headers:
            meta.append(f"{h}: {r.headers[h]}")
    out = []
    if r.status_code in (204, 205, 304) or r.request.method == "HEAD":
        out.append(types.TextContent(type="text", text="\n".join(meta)))
    elif ct == "text/event-stream":
        raw = bytearray()

        async def pump():
            async for chunk in r.aiter_bytes():
                raw.extend(chunk[:max(0, MAX_CHARS * 4 - len(raw))])
                if len(raw) >= MAX_CHARS * 4:
                    break

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(pump(), SSE_SECONDS)
        meta.append(f"(event stream sampled for up to {SSE_SECONDS:g}s)")
        out.append(types.TextContent(type="text", text=clip("\n".join(meta) + "\n" + raw.decode("utf-8", "replace"))))
    elif is_textual(ct) and not save_to:
        raw, complete = await read_limited(r, 2_000_000)
        text = raw.decode("utf-8", "replace")
        if ct.endswith("json"):
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
            except ValueError:
                pass
        if not complete:
            text += "\n[response exceeded 2MB and was cut off]"
        out.append(types.TextContent(type="text", text=clip("\n".join(meta) + ("\n" + text if text else ""))))
    elif save_to:
        p = local_file(save_to)
        p.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        f = p.open("xb")
        try:
            with f:
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    n += len(chunk)
                    if n > MAX_FILE_BYTES:
                        raise ValueError("Download exceeds the 64 MiB file limit")
                    f.write(chunk)
        except BaseException:
            p.unlink(missing_ok=True)
            raise
        meta.append(f"saved {n} bytes ({ct or 'unknown type'}) to {p}")
        out.append(types.TextContent(type="text", text="\n".join(meta)))
    elif ct.startswith("image/") and "svg" not in ct:
        raw, complete = await read_limited(r, 4_000_000)
        if complete:
            out.append(types.TextContent(type="text", text="\n".join(meta)))
            out.append(types.ImageContent(type="image", data=base64.b64encode(raw).decode(), mimeType=ct))
        else:
            meta.append(f"image over 4MB ({ct}); pass save_to to download it")
            out.append(types.TextContent(type="text", text="\n".join(meta)))
    else:
        meta.append(f"binary response: {ct or 'unknown type'}, content-length {r.headers.get('content-length', 'unknown')}. Not downloaded; pass save_to (local path) to save it, or a Range header to fetch a slice.")
        out.append(types.TextContent(type="text", text="\n".join(meta)))
    return out, r.status_code


async def execute(op, args):
    if not allowed(op):
        raise ValueError(f"Operation {op.id} is disabled")
    args = {} if args is None else args
    try:
        jsonschema.validate(args, op.input_schema)
    except jsonschema.ValidationError as e:
        raise ValueError(f"Invalid arguments at {'.'.join(map(str, e.absolute_path)) or 'root'}: {e.message}") from None
    args = dict(args)
    headers = httpx.Headers(DEFAULT_HEADERS)
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    extra = args.pop("headers", None) or {}
    permitted_headers = {p["name"].lower() for p in op.header_params} | {
        "x-profile-id", "x-profile-token", "if-match", "if-none-match", "if-range", "range",
        "if-modified-since", "if-unmodified-since",
    }
    for k, v in extra.items():
        if k.lower() not in permitted_headers or k.lower() in {
            "authorization", "host", "connection", "content-length", "transfer-encoding", "proxy-authorization",
        } or "\r" in v or "\n" in v:
            raise ValueError(f"Header is not permitted: {k}")
        headers[k] = v
    for p in op.header_params:
        if p.get("required") and p["name"] not in headers:
            raise ValueError(f"Missing required header: {p['name']}")
    body_supplied = "body" in args
    body = args.pop("body", None)
    save_to = args.pop("save_to", None)
    if save_to is not None:
        local_file(save_to)
    path, query = op.path, {}
    for key, (loc, name, _p) in op.argmap.items():
        if key not in args:
            continue
        v = args.pop(key)
        if v is None:
            continue
        if loc == "path":
            if str(v) in (".", ".."):
                raise ValueError(f"Invalid path parameter: {name}")
            path = path.replace("{" + name + "}", quote(str(to_query(v)), safe=""))
        else:
            query[name] = to_query(v)
    unfilled = re.findall(r"\{([^{}]+)\}", path)
    if unfilled:
        raise ValueError(f"missing path parameters: {unfilled}")

    kw = {}
    if body_supplied:
        if op.body_ctype == "multipart/form-data":
            files = {}
            for k, v in (body or {}).items():
                if isinstance(v, dict) and "content_base64" in v:
                    data = base64.b64decode(v["content_base64"], validate=True)
                    if len(data) > MAX_FILE_BYTES:
                        raise ValueError("Upload exceeds the 64 MiB file limit")
                    files[k] = (v.get("filename", k), data, v.get("content_type", "application/octet-stream"))
                else:
                    files[k] = (None, v if isinstance(v, str) else json.dumps(v))
            kw["files"] = files
        elif op.body_ctype == "application/octet-stream":
            if isinstance(body, dict) and "path" in body:
                with local_file(body["path"]).open("rb") as f:
                    kw["content"] = f.read(MAX_FILE_BYTES + 1)
            elif isinstance(body, dict) and "base64" in body:
                kw["content"] = base64.b64decode(body["base64"], validate=True)
            else:
                kw["content"] = body if isinstance(body, bytes) else str(body).encode()
            if len(kw["content"]) > MAX_FILE_BYTES:
                raise ValueError("Upload exceeds the 64 MiB file limit")
            headers.setdefault("Content-Type", "application/octet-stream")
        elif op.body_ctype and (op.body_ctype == "application/json" or op.body_ctype.endswith("+json")):
            if body is None:
                kw["content"] = b"null"
            else:
                kw["json"] = body
            headers.setdefault("Content-Type", op.body_ctype)
        else:
            raise ValueError(f"Unsupported request content type: {op.body_ctype}")
    async with client.stream(op.method.upper(), BASE + path, params=query, headers=headers, **kw) as r:
        content, status = await render(r, save_to)
    if status >= 300 and status != 304:
        text = "\n".join(c.text for c in content if isinstance(c, types.TextContent))
        if status == 401 and not TOKEN:
            text += "\n(SILO_TOKEN is not set; create an API key in Silo and add it to the server environment.)"
        raise RuntimeError(text)
    return content


# --------------------------------------------------------------------------- MCP surface

INSTRUCTIONS = (
    f"Silo media server API ({len(OPS)} operations). "
    + (
        "Use silo_search_operations to find an operation, silo_describe_operation for its parameters, then silo_call_operation to run it. "
        "Path and query parameters go in `arguments` by name; JSON request bodies go in `arguments.body`."
        if MODE != "full"
        else "Each tool is one API operation; path and query parameters are top-level arguments, JSON bodies go in `body`."
    )
    + " Operations are grouped by tag (admin*, libraries, playback, ...). DELETE and admin operations change real data; confirm intent first."
)

server = Server("silo-server-mcp", instructions=INSTRUCTIONS)


def annotations(op):
    return types.ToolAnnotations(
        title=op.summary or op.id,
        readOnlyHint=op.read_only and FILES_DIR is None,
        destructiveHint=not op.read_only,
        idempotentHint=op.method in ("get", "head", "options", "put", "delete"),
        openWorldHint=True,
    )


def helper_tools():
    return [
        types.Tool(
            name="silo_search_operations",
            description="Search Silo API operations by keyword (matches operationId, path, summary, tag). Returns operationId, method, path, summary. Empty query lists everything (paged).",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string"},
                "tag": {"type": "string", "description": "Exact tag, see silo_list_tags"},
                "method": {"type": "string", "enum": [m.upper() for m in METHODS]},
                "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
            }, "additionalProperties": False},
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        ),
        types.Tool(
            name="silo_list_tags",
            description="List API tag groups with operation counts.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        ),
        types.Tool(
            name="silo_describe_operation",
            description="Full description and input schema (path/query/body/headers) of one operation.",
            inputSchema={"type": "object", "properties": {"operation_id": {"type": "string"}}, "required": ["operation_id"], "additionalProperties": False},
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        ),
        types.Tool(
            name="silo_call_operation",
            description="Call any Silo API operation by operationId. `arguments` holds path/query parameters by name, plus optional `body`, `headers`, `save_to`. Use silo_describe_operation first if unsure.",
            inputSchema={"type": "object", "properties": {
                "operation_id": {"type": "string"},
                "arguments": {"type": "object", "additionalProperties": True},
            }, "required": ["operation_id"], "additionalProperties": False},
            annotations=types.ToolAnnotations(readOnlyHint=READONLY and FILES_DIR is None, destructiveHint=not READONLY, openWorldHint=True),
        ),
    ]


@server.list_tools()
async def list_tools():
    if MODE == "full":
        return [
            types.Tool(name=op.id, description=op.describe(), inputSchema=op.input_schema, annotations=annotations(op))
            for op in OPS.values()
        ]
    return helper_tools()


def text(s):
    return [types.TextContent(type="text", text=s)]


def get_op(name):
    op = OPS.get(name)
    if not op:
        hint = [k for k in OPS if name.lower() in k.lower()][:8]
        raise ValueError(f"unknown or disabled operation '{name}'" + (f"; similar: {hint}" if hint else ""))
    return op


@server.call_tool(validate_input=False)
async def call_tool_impl(name, arguments):
    arguments = {} if arguments is None else arguments
    if MODE != "full":
        tool = next((t for t in helper_tools() if t.name == name), None)
        if tool is None:
            raise ValueError(f"unknown tool {name}")
        try:
            jsonschema.validate(arguments, tool.inputSchema)
        except jsonschema.ValidationError as e:
            raise ValueError(f"Invalid tool arguments: {e.message}") from None
        if name == "silo_list_tags":
            counts = Counter(op.tag for op in OPS.values())
            return text("\n".join(f"{t}: {n}" for t, n in sorted(counts.items())))
        if name == "silo_search_operations":
            words = (arguments.get("query") or "").lower().split()
            tag, method = arguments.get("tag"), (arguments.get("method") or "").lower()
            limit, offset = int(arguments.get("limit", 25)), int(arguments.get("offset", 0))
            hits = []
            for op in OPS.values():
                if tag and op.tag != tag:
                    continue
                if method and op.method != method:
                    continue
                hay = f"{op.id} {op.path} {op.summary} {op.tag}".lower()
                if all(w in hay for w in words):
                    hits.append(op)
            page = hits[offset:offset + limit]
            lines = [f"{op.id} | {op.method.upper()} {op.path} | [{op.tag}] {op.summary.splitlines()[0][:140] if op.summary else ''}" for op in page]
            return text(f"{len(hits)} match(es), showing {offset + 1 if page else 0}-{offset + len(page)}\n" + "\n".join(lines))
        if name == "silo_describe_operation":
            op = get_op(arguments.get("operation_id", ""))
            return text(json.dumps({
                "operation_id": op.id, "method": op.method.upper(), "path": op.path, "tag": op.tag,
                "summary": op.summary, "description": op.description,
                "requires_auth": not op.public, "response_types": sorted(op.response_types),
                "input_schema": op.input_schema,
            }, ensure_ascii=False))
        if name == "silo_call_operation":
            return await execute(get_op(arguments.get("operation_id", "")), arguments.get("arguments"))
        raise ValueError(f"unknown tool {name}")
    return await execute(get_op(name), arguments)


async def amain():
    try:
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
    finally:
        await client.aclose()


def http_app():
    """Authenticated, stateless Streamable HTTP application."""
    import hmac

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Mount, Route

    gate = os.getenv("SILO_MCP_AUTH_TOKEN", "")
    if len(gate) < 32 or not gate.isascii() or any(c.isspace() for c in gate):
        raise ValueError("HTTP requires SILO_MCP_AUTH_TOKEN (at least 32 ASCII characters without whitespace)")
    path = os.getenv("SILO_MCP_PATH", "mcp").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", path) or path in ("auto", "healthz"):
        raise ValueError("SILO_MCP_PATH must be a single URL segment; auto and healthz are reserved")
    hosts = [h.strip() for h in os.getenv("SILO_ALLOWED_HOSTS", "localhost,localhost:*,127.0.0.1,127.0.0.1:*,[::1],[::1]:*").split(",") if h.strip()]
    origins = [o.strip() for o in os.getenv("SILO_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    manager = StreamableHTTPSessionManager(app=server, stateless=True, security_settings=TransportSecuritySettings(
        allowed_hosts=hosts, allowed_origins=origins,
    ))

    async def mcp_app(scope, receive, send):
        credentials = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
        if len(credentials) != 1 or not hmac.compare_digest(credentials[0], f"Bearer {gate}".encode()):
            await JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    async def health(_request):
        return PlainTextResponse("ok")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        try:
            async with manager.run():
                yield
        finally:
            await client.aclose()

    return Starlette(
        routes=[Route("/healthz", health), Mount(f"/{path}", app=mcp_app)],
        lifespan=lifespan,
    )


def serve_http():
    import uvicorn

    uvicorn.run(http_app(), host=os.getenv("SILO_HOST", "127.0.0.1"), port=int(os.getenv("SILO_PORT", "8000")), log_level="info")


if __name__ == "__main__":
    if "--check" in sys.argv:
        tags = Counter(op.tag for op in OPS.values())
        print(f"spec: {SPEC['info']['title']} v{SPEC['info']['version']}  base: {BASE}")
        print(f"operations total={len(ALL_OPS)} exposed={len(OPS)} mode={MODE} readonly={READONLY} token={'set' if TOKEN else 'unset'}")
        print(f"tags: {len(tags)}")
        for op in OPS.values():
            jsonschema.Draft202012Validator.check_schema(op.input_schema)
        size = sum(len(json.dumps(op.input_schema)) for op in OPS.values())
        print(f"full-mode schema payload: {size/1e6:.2f} MB")
        sys.exit(0)
    log(f"{len(OPS)} operations, mode={MODE}, base={BASE}, token={'set' if TOKEN else 'unset'}")
    transport = os.getenv("SILO_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http"):
        serve_http()
    elif transport == "stdio":
        asyncio.run(amain())
    else:
        raise SystemExit("SILO_TRANSPORT must be stdio or http")

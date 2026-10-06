# Silo MCP server

Use a Silo media server from an MCP client. The server builds tools from your instance's `/api/v2/openapi.json` at startup and caches the spec per URL. Restart to pick up API changes.

Python 3.10+ and [uv](https://docs.astral.sh/uv/) are required. This is a single-user bridge: every connected client shares the configured Silo credentials.

## Quick start

Clone this repository, then set your own server URL and a Silo API key:

```sh
export SILO_BASE_URL=https://silo.example.com
export SILO_TOKEN=your-api-key
uv run server.py --check
uv run server.py
```

Dependencies install automatically from the script header. `--check` loads the spec and checks tool generation without starting MCP or calling API operations. Logs go to stderr; stdout is reserved for the stdio protocol.

Read-only mode is on by default. Set `SILO_READONLY=0` to expose write operations. Silo API keys are not scoped. Use `SILO_READONLY`, `SILO_TAGS`, and `SILO_EXCLUDE_TAGS` to restrict operations through this MCP server. These filters do not restrict the key itself; anyone with the Silo key can use it directly outside this wrapper. The read-only filter checks HTTP methods, not endpoint behavior.

## MCP client configuration

For a client that supports stdio MCP servers:

```json
{
  "mcpServers": {
    "silo": {
      "command": "uv",
      "args": ["run", "/absolute/path/to/silo-server-mcp/server.py"],
      "env": {
        "SILO_BASE_URL": "https://silo.example.com",
        "SILO_TOKEN": "your-api-key"
      }
    }
  }
}
```

Keep client configuration containing credentials private. Client-specific configuration formats may differ.

## Docker / HTTP

The **MCP auth token** is a shared password that controls who can connect to this server's HTTP endpoint. You generate it yourself; it is not issued by Silo or your AI provider.

There are two separate credentials:

- `SILO_TOKEN` is your Silo API key. This server uses it to make requests to Silo.
- `SILO_MCP_AUTH_TOKEN` is the secret your MCP client sends to connect to this server. Use a different value from your Silo API key. It is required for HTTP connections; stdio does not use it.

```sh
cp stack.env.example stack.env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# Edit stack.env with your Silo URL, Silo API key, and the generated MCP token.
docker compose --env-file stack.env -f compose.example.yml up --build -d
```

Paste the generated token into `SILO_MCP_AUTH_TOKEN` in `stack.env`, without a `Bearer ` prefix. Put the same token in your MCP client's `Authorization` header as `Bearer <token>`, as shown below. The server checks that the values match before accepting a connection. Keep the token private: anyone holding it can call the operations enabled by your MCP configuration.

Compose does not resolve password-manager references such as `op://` automatically; supply resolved values through your own secret-management workflow.

The example binds to `127.0.0.1:8123`, runs as a non-root user with a read-only filesystem, and exposes `/healthz` for health checks. Configure a client that supports HTTP and custom headers:

```json
{
  "mcpServers": {
    "silo": {
      "type": "http",
      "url": "http://127.0.0.1:8123/mcp/",
      "headers": {
        "Authorization": "Bearer your-separate-mcp-token"
      }
    }
  }
}
```

HTTP refuses to start without an MCP bearer token of at least 32 ASCII characters. Generate a random token; length alone does not make a token secure. Secret endpoint URLs and `SILO_MCP_PATH=auto` are no longer supported. HTTP uses a static bearer token, not OAuth discovery, so clients must support supplying the header.

For remote access, use a TLS reverse proxy or private tunnel. Add the public hostname to `SILO_ALLOWED_HOSTS` (including the port when nonstandard). Browser clients also need an explicit `SILO_ALLOWED_ORIGINS` entry and proxy CORS configuration. Host and Origin validation stays enabled; do not disable it at the proxy. Anyone holding the MCP token can use the configured Silo key and any enabled local file directory. This is not a multi-user public service.

## Tools

`SILO_MODE=compact` exposes four tools:

- `silo_search_operations`: find operations by keyword, tag, or method; paginated, up to 100 results.
- `silo_list_tags`: list available tag groups and counts.
- `silo_describe_operation`: inspect an operation's input schema.
- `silo_call_operation`: execute it with `operation_id` and `arguments`.

`SILO_MODE=full` exposes one tool per operation. A large spec can overwhelm client context; narrow it with `SILO_TAGS` or `SILO_EXCLUDE_TAGS` (comma-separated globs, matched against each operation's first tag). Filters apply to execution as well as discovery.

Path and query parameters go in `arguments` in compact mode, or at the top level in full mode. JSON request bodies go in `body`; declared headers and common conditional/profile headers go in `headers`. Authorization and transport headers cannot be overridden by tool arguments. Inspect the generated schema for parameter names that collide with `body`, `headers`, `save_to`, or one another.

Binary responses return metadata, except small images, which return image content. Multipart file fields take `{"content_base64": "...", "filename": "file.bin", "content_type": "application/octet-stream"}`. Raw octet-stream bodies accept `{"base64": "..."}` or a plain string.

### Optional local files

Local file access is disabled unless you set `SILO_FILES_DIR` to a dedicated directory. Once enabled:

- `save_to` saves a successful response to a **new relative path** inside that directory. Existing files are never overwritten; failed or interrupted downloads are removed.
- Raw octet-stream bodies can use `{"path": "relative/file.bin"}` to upload a file from that directory.
- File uploads and downloads are capped at 64 MiB. Paths that escape the directory, including symlinks pointing outside it, are rejected.

The directory is on the MCP server's machine, not the client's. Do not use a home directory, credential directory, or a directory writable by untrusted local users. For Docker, mount a dedicated writable directory and set `SILO_FILES_DIR` to its container path.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `SILO_BASE_URL` | required | Silo server root, e.g. `https://silo.example.com`; no path, query, or embedded credentials |
| `SILO_TOKEN` | none | Silo bearer token or API key; needed for protected operations |
| `SILO_PROFILE_ID`, `SILO_PROFILE_TOKEN` | none | Default profile headers |
| `SILO_MODE` | `compact` | `compact` or `full` |
| `SILO_READONLY` | `1` | Only GET, HEAD, and OPTIONS; `0` enables writes |
| `SILO_TAGS`, `SILO_EXCLUDE_TAGS` | none | First-tag filters; comma-separated globs |
| `SILO_OPENAPI_FILE` | none | Use a trusted local JSON spec instead of fetching it |
| `SILO_OPENAPI_URL` | `<SILO_BASE_URL>/api/v2/openapi.json` | Alternate trusted spec URL; fetched without the Silo token |
| `SILO_CACHE_DIR` | `~/.cache/silo-server-mcp` | Spec cache; `/tmp/silo-server-mcp` in Docker |
| `SILO_TIMEOUT` | `60` | Upstream HTTP timeout, seconds per network phase |
| `SILO_FILES_DIR` | disabled | Dedicated root for optional local file access |
| `SILO_TRANSPORT` | `stdio` | `stdio` or `http`; Docker uses `http` |
| `SILO_MCP_AUTH_TOKEN` | required for HTTP | Separate MCP bearer token, at least 32 characters |
| `SILO_MCP_PATH` | `mcp` | HTTP endpoint segment; clients use `/mcp/` |
| `SILO_HOST`, `SILO_PORT` | `127.0.0.1`, `8000` | Bind address; Docker binds `0.0.0.0` inside the container |
| `SILO_ALLOWED_HOSTS` | localhost and loopback addresses, any port | Comma-separated Host header allowlist; supports `host:*` |
| `SILO_ALLOWED_ORIGINS` | none | Comma-separated browser Origin allowlist; absent Origin is accepted |

## Limits and security

Use HTTPS for Silo and remote MCP connections. API redirects are returned as tool errors instead of being followed, so credentials cannot be forwarded to a redirect target. Silo error bodies are visible to the MCP client and may contain private data. Treat API content and OpenAPI descriptions as untrusted input to your assistant.

The wrapper supports JSON, multipart, and raw octet-stream request bodies, primitive path/query parameters, and repeated query arrays. Other OpenAPI serialization styles, cookie parameters, external `$ref` files, and WebSockets are not supported. Recursive schemas are bounded; Silo remains the final validator. Text responses are capped at 2 MB before character truncation; inline images at 4 MB. HTTP MCP requests are capped at 4 MiB, including base64 upload data.

Use the local spec option if Silo's spec endpoint requires authentication. A failed spec fetch falls back to that URL's cached copy; Docker's default cache is temporary and does not survive container recreation. Review newly exposed operations after server upgrades, especially when writes are enabled.

Security issues: use this repository's private vulnerability reporting if enabled. Do not include credentials or private server responses in public issues.

## Development

Run the offline regression check with the server's runtime dependencies:

```sh
uv run --with 'mcp==1.30.0' --with 'httpx==0.28.1' python test_server.py
```

It uses a synthetic spec and mock API responses, and exercises the real stdio and HTTP MCP interfaces. It never contacts a Silo instance.

## License

[GNU General Public License v2.0](LICENSE), as provided in the upstream repository.

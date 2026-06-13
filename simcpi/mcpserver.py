"""
mcpserver.py — simcpi server
============================
Single @app.create_tool_api() decorator → REST route + MCP tool.

Routes added automatically:
  GET  /docs          Swagger UI
  GET  /mcpark        MCPark — visual explorer + LLM tester
  POST /mcpark/tools  List registered MCP tools (internal)
  POST /mcpark/run    Run LLM+MCP loop (internal)
  POST /mcp/mcp       MCP streamable-http transport
"""

from __future__ import annotations

import json
import inspect
import sys
from inspect import cleandoc
import functools
from contextlib import asynccontextmanager
from typing import Callable, Literal, get_type_hints

# ── Dependency check ──────────────────────────────────────────────────────────
_REQUIRED = {
    "fastapi": "fastapi>=0.100.0",
    "fastmcp": "fastmcp>=3.2",
    "pydantic": "pydantic>=2.0.0",
    "uvicorn": "uvicorn>=0.20.0",
}
_missing = []
for _pkg, _spec in _REQUIRED.items():
    try:
        __import__(_pkg)
    except ImportError:
        _missing.append(_spec)
if _missing:
    print("[simcpi] Missing dependencies. Run: pip install " + " ".join(_missing))
    sys.exit(1)
# ─────────────────────────────────────────────────────────────────────────────

import os
import ast
import uuid
import time
import pathlib
import tempfile
import contextlib

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastmcp import FastMCP
from fastmcp import Client as _MCPInternalClient
from fastmcp.client import StreamableHttpTransport
from pydantic import BaseModel


# ─────────────────────────────────────────────────────────────────────────────
# MIME type helper
# ─────────────────────────────────────────────────────────────────────────────

def _mime_type(filename: str) -> str:
    ext = pathlib.Path(filename).suffix.lower()
    return {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls":  "application/vnd.ms-excel",
        ".csv":  "text/csv",
        ".pdf":  "application/pdf",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif":  "image/gif",
        ".webp": "image/webp",
        ".json": "application/json",
        ".txt":  "text/plain",
        ".zip":  "application/zip",
    }.get(ext, "application/octet-stream")


# ─────────────────────────────────────────────────────────────────────────────
# MCPark HTML — served at /mcpark
# ─────────────────────────────────────────────────────────────────────────────

def _load_mcpark_html() -> str:
    """Load mcpark.html from static/ folder alongside mcpserver.py."""
    import pathlib as _p, os as _o
    here = _p.Path(_o.path.abspath(__file__)).parent
    f = here / "static" / "mcpark.html"
    if f.exists():
        return f.read_text(encoding="utf-8")
    raise FileNotFoundError(f"mcpark.html not found at {f}")


def _skill_md_path() -> pathlib.Path:
    """Path to the bundled simcpi SKILL.md (shipped as package data)."""
    return pathlib.Path(os.path.abspath(__file__)).parent / "SKILL.md"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    prompt:          str
    api_key:         str | None = None
    provider:        Literal["openai", "anthropic"] | None = None
    model:           str = "gpt-4o"
    base_url:        str | None = None
    default_headers: dict | None = None
    assistant:       bool = True   # True: LLM explains result. False: raw tool output only.
    memory:          bool = True   # use feedback memory for retrieval injection (if enabled)
    session_id:      str | None = None
    mcp_url:         str | None = None   # target a REMOTE MCP server instead of this one
    mcp_auth:        str | None = None   # bearer token for that remote server


class ToolsRequest(BaseModel):
    mcp_url:  str | None = None
    mcp_auth: str | None = None


class TestGenRequest(BaseModel):
    api_key:         str | None = None
    provider:        Literal["openai", "anthropic"] | None = None
    model:           str = "gpt-4o"
    base_url:        str | None = None
    default_headers: dict | None = None
    mcp_url:         str | None = None
    mcp_auth:        str | None = None


class RateRequest(BaseModel):
    call_id: str
    rating:  int   # 0 or 1
    note:    str | None = None   # optional free-text reason → optimizer evidence


class OptimizeRequest(BaseModel):
    tool_name:       str | None = None   # None — optimize every tool with enough data
    api_key:         str | None = None
    provider:        Literal["openai", "anthropic"] | None = None
    model:           str = "gpt-4o"
    base_url:        str | None = None
    default_headers: dict | None = None


class ApplyDocstringRequest(BaseModel):
    tool:        str
    description: str


# ─────────────────────────────────────────────────────────────────────────────
# MCP function helper — strips FastAPI Depends() from tool schema
# ─────────────────────────────────────────────────────────────────────────────

def _make_mcp_fn(fn: Callable) -> Callable:
    """
    Return a wrapper of fn with FastAPI Depends() parameters removed from its
    __signature__. FastMCP reads __signature__ to build the tool inputSchema,
    so the LLM never sees Depends params. When called via MCP, those params
    are injected as None so the original function signature still works.
    """
    try:
        from fastapi.params import Depends as _Depends
    except ImportError:
        return fn  # fastapi not available, nothing to strip

    sig = inspect.signature(fn)
    depends_names = {
        name for name, param in sig.parameters.items()
        if isinstance(param.default, _Depends)
    }

    if not depends_names:
        return fn  # nothing to strip

    mcp_params = [
        p for name, p in sig.parameters.items()
        if name not in depends_names
    ]
    mcp_sig = sig.replace(parameters=mcp_params)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def mcp_wrapper(**kwargs):
            for dep_name in depends_names:
                kwargs.setdefault(dep_name, None)
            return await fn(**kwargs)
    else:
        @functools.wraps(fn)
        def mcp_wrapper(**kwargs):
            for dep_name in depends_names:
                kwargs.setdefault(dep_name, None)
            return fn(**kwargs)

    mcp_wrapper.__signature__ = mcp_sig
    return mcp_wrapper


def _split_result(r) -> tuple[str, list[dict]]:
    """
    Split an MCP tool result into (text, images) for the trace.
    Image blocks arrive base64-encoded; the UI renders them inline.
    """
    texts, images = [], []
    for b in (r.content or []):
        btype = getattr(b, "type", None)
        if btype == "text":
            texts.append(b.text)
        elif btype == "image":
            images.append({"mimeType": getattr(b, "mimeType", "image/png"),
                           "data": getattr(b, "data", "")})
    text = "\n".join(texts) if texts else \
           (f"[{len(images)} image(s) returned]" if images else str(r))
    return text, images


def _rewrite_docstring_in_source(
    source: str, func_name: str, new_doc: str, near_line: int = 1,
) -> str:
    """
    Return `source` with `func_name`'s docstring replaced (or inserted) using
    AST positions — never text-matching. Raises ValueError if the function
    can't be found or the new text can't be embedded safely.

    `near_line` disambiguates same-named functions: the def at or after it wins.
    """
    if '"""' in new_doc:
        raise ValueError('description contains """ — paste it in by hand instead')

    tree = ast.parse(source)
    candidates = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
    ]
    if not candidates:
        raise ValueError(f"couldn't find function '{func_name}' in the source file")

    after = [n for n in candidates if n.lineno >= near_line]
    target = min(after or candidates, key=lambda n: n.lineno)

    lines = source.splitlines(keepends=True)
    def _offset(lineno: int, col: int) -> int:
        return sum(len(l) for l in lines[:lineno - 1]) + col

    body = target.body
    existing = body[0] if body and isinstance(body[0], ast.Expr) \
        and isinstance(getattr(body[0], "value", None), ast.Constant) \
        and isinstance(body[0].value.value, str) else None

    if existing is not None:
        indent = " " * existing.col_offset
        literal = f'"""{new_doc}"""'
        start = _offset(existing.lineno, existing.col_offset)
        end   = _offset(existing.end_lineno, existing.end_col_offset)
        return source[:start] + literal + source[end:]

    # No docstring — insert one as the first line of the body.
    first = body[0]
    indent = " " * first.col_offset
    insert_at = _offset(first.lineno, 0)
    return source[:insert_at] + f'{indent}"""{new_doc}"""\n' + source[insert_at:]


# ─────────────────────────────────────────────────────────────────────────────
# MCPApi — the server class
# ─────────────────────────────────────────────────────────────────────────────

class MCPApi:
    """
    Merges FastAPI and FastMCP into a single server.

    Parameters
    ----------
    title:          API / MCP server name.
    mcp_path:       MCP transport prefix (default: "/mcp").
    version:        Version string.
    description:    Shown in OpenAPI docs and MCP server instructions.
    mcpark_path:    Path for MCPark UI (default: "/mcpark"). None to disable.
    provider:       Default LLM provider — "openai" or "anthropic".
    api_key:        Default LLM API key. Inherited by MCPark UI + get_client().
    base_url:       Default LLM base URL (for proxies like AICredits, OpenRouter).
    model:          Default model name. Shown pre-selected in MCPark.
    auth_token:     Optional shared secret. When set, every route requires
                    Authorization: Bearer <token>, ?key=<token>, or the
                    simcpi_key cookie. Default None — auth off, nothing changes.
    file_ttl:       Seconds a serve_file() link stays valid (default 3600).
                    None — links never expire (size cap still applies).
    file_store_max: Max number of live serve_file() entries (default 256).
                    Oldest entries (and their temp files) are evicted beyond it.
    """

    def __init__(
        self,
        title:        str = "simcpi",
        mcp_path:     str = "/mcp",
        version:      str = "0.1.0",
        description:  str | None = None,
        mcpark_path:  str | None = "/mcpark",
        provider:         Literal["openai", "anthropic"] | None = None,
        api_key:          str | None = None,
        base_url:         str | None = None,
        model:            str | None = None,
        default_headers:  dict | None = None,
        feedback:           bool = False,
        feedback_db:        str | None = None,
        feedback_only_positive: bool = False,
        feedback_k:         int = 5,
        feedback_threshold: float = 0.6,
        embedding_model:    str = "text-embedding-3-small",
        embedding_api_key:  str | None = None,
        embedding_base_url: str | None = None,
        auth_token:         str | None = None,
        file_ttl:           float | None = 3600,
        file_store_max:     int = 256,
    ):
        self.title           = title
        self.mcp_path        = mcp_path.rstrip("/")
        self.mcpark_path     = mcpark_path
        self.provider        = provider
        self.api_key         = api_key
        self.base_url        = base_url
        self.model           = model
        self.default_headers = default_headers

        # ── Feedback memory (MCPark only, opt-in, default OFF) ────────────────
        self.feedback_enabled       = feedback
        self.feedback_only_positive = feedback_only_positive
        self.feedback_k             = feedback_k
        self.feedback_threshold     = feedback_threshold
        self.embedding_model        = embedding_model
        self.embedding_api_key      = embedding_api_key   # falls back to api_key
        self.embedding_base_url     = embedding_base_url   # falls back to base_url
        self._feedback = None
        if feedback:
            from .feedback import FeedbackMemory
            db = feedback_db or str(pathlib.Path.cwd() / "simcpi_feedback.db")
            self._feedback = FeedbackMemory(db)
            print(f"[simcpi] Feedback memory ON  ->  {db}")

        # ── FastMCP ──────────────────────────────────────────────────────────
        self.mcp     = FastMCP(name=title, version=version, instructions=description)
        self._mcp_app = self.mcp.http_app()

        # ── FastAPI + wired MCP lifespan ─────────────────────────────────────
        mcp_app = self._mcp_app

        @asynccontextmanager
        async def combined_lifespan(app: FastAPI):
            async with mcp_app.lifespan(mcp_app):
                yield

        self.api = FastAPI(
            title=title,
            version=version,
            description=description or "",
            lifespan=combined_lifespan,
        )

        self.api.mount(self.mcp_path, self._mcp_app)

        # ── Optional auth (default OFF — nothing changes unless set) ─────────
        self.auth_token = auth_token
        if auth_token:
            self._register_auth()
            print(f"[simcpi] Auth ON -- clients need 'Authorization: Bearer <token>'; "
                  f"open MCPark as {mcpark_path or '/mcpark'}?key=<token>")

        # ── Default remote target for MCPark (set by `python -m simcpi <url>`) ─
        self.default_remote: dict | None = None

        # ── Routes ───────────────────────────────────────────────────────────
        self._register_mcpark()

        # ── Registry ─────────────────────────────────────────────────────────
        self._registry:          dict[str, Callable] = {}
        self._resource_registry: dict[str, Callable] = {}

        # ── File store — token → (path, created_ts, is_temp) ─────────────────
        # TTL + size cap so long-running servers don't grow without bound.
        # Temp copies under simcpi_files are deleted on eviction; user-supplied
        # paths are never touched.
        self.file_ttl       = file_ttl
        self.file_store_max = max(1, file_store_max)
        self._file_store: dict[str, tuple[pathlib.Path, float, bool]] = {}
        self._sweep_temp_dir()
        self._register_files_route()

    # ── Auth middleware (opt-in via auth_token) ───────────────────────────────

    def _register_auth(self):
        """
        Protect every route (REST, MCP, MCPark, /files) behind self.auth_token.

        Accepted credentials, checked in order:
          - Authorization: Bearer <token>   (MCP clients, curl, scripts)
          - ?key=<token> query param        (first browser visit — sets a cookie)
          - simcpi_key cookie               (subsequent browser requests)
        """
        import secrets

        token = self.auth_token

        def _ok(value) -> bool:
            return bool(value) and secrets.compare_digest(str(value), token)

        @self.api.middleware("http")
        async def _auth_middleware(request: Request, call_next):
            auth   = request.headers.get("authorization", "")
            bearer = auth[7:] if auth.lower().startswith("bearer ") else None
            q_key  = request.query_params.get("key")
            cookie = request.cookies.get("simcpi_key")

            if not (_ok(bearer) or _ok(q_key) or _ok(cookie)):
                return JSONResponse(
                    {"error": "Unauthorized — pass 'Authorization: Bearer <auth_token>', "
                              "?key=<auth_token>, or the simcpi_key cookie"},
                    status_code=401,
                )

            response = await call_next(request)
            if _ok(q_key) and not _ok(cookie):
                # Browser came in via ?key= — remember it so MCPark's
                # follow-up fetches (no query param) keep working.
                response.set_cookie("simcpi_key", token, httponly=True, samesite="lax")
            return response

    async def _set_tool_description(self, name: str, description: str) -> bool:
        """
        Best-effort update of a live tool's description (lasts until restart).
        Source code is the durable home — this exists so the optimizer's
        rewrite can be re-evaluated immediately without editing files.
        """
        try:
            got = self.mcp.get_tool(name)
            tool = await got if inspect.isawaitable(got) else got
            tool.description = description
            return True
        except Exception as e:
            print(f"[simcpi] live docstring update failed for '{name}': {e}")
            return False

    @staticmethod
    def _is_local_request(request: Request) -> bool:
        """True only for loopback callers — gates the source-editing endpoint."""
        host = (request.client.host if request.client else "") or ""
        return host in {"127.0.0.1", "::1", "localhost", "testclient"}

    def _write_docstring_to_source(self, name: str, description: str) -> dict:
        """
        Permanently write `description` into the tool's function docstring in its
        .py file (AST-located), then sync the live description. Returns a dict
        with ok/file/line or an error — never raises.
        """
        fn = self._registry.get(name)
        if fn is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            src_file = inspect.getsourcefile(fn) or inspect.getfile(fn)
            _, start_line = inspect.getsourcelines(fn)
        except (TypeError, OSError):
            return {"ok": False, "error": "couldn't locate source (defined dynamically?) — copy it in by hand"}
        if not src_file or not os.path.exists(src_file):
            return {"ok": False, "error": "source file not found on disk — copy it in by hand"}

        path = pathlib.Path(src_file)
        try:
            original = path.read_text(encoding="utf-8")
            updated  = _rewrite_docstring_in_source(
                original, fn.__name__, description, near_line=start_line)
            ast.parse(updated)  # never write a file that won't parse
            path.write_text(updated, encoding="utf-8")
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        return {"ok": True, "file": str(path), "line": start_line}

    @staticmethod
    def _transport_with_auth(mcp_url: str, token: str | None) -> StreamableHttpTransport:
        """HTTP transport with an optional bearer token."""
        if token:
            return StreamableHttpTransport(
                mcp_url, headers={"Authorization": f"Bearer {token}"}
            )
        return StreamableHttpTransport(mcp_url)

    def _internal_transport(self, mcp_url: str) -> StreamableHttpTransport:
        """Transport for MCPark's self-calls — carries this server's own auth."""
        return self._transport_with_auth(mcp_url, self.auth_token)

    # ── Embeddings helper (OpenAI-compatible only) ────────────────────────────

    def _embed_creds(
        self,
        provider: str | None,
        api_key:  str | None,
        base_url: str | None,
    ) -> tuple[str | None, str | None]:
        """
        Resolve which key/base_url to use for embeddings.

        - An explicit embedding_api_key always wins (for e.g. an Anthropic chat
          user who supplies a separate OpenAI key just for embeddings).
        - Otherwise reuse the LLM key/base_url, but only when the provider is
          OpenAI-compatible — embeddings always go through an OpenAI-style API.
        - Returns (None, None) when no usable embeddings key exists → caller
          degrades to log-only (no retrieval).
        """
        if self.embedding_api_key:
            return self.embedding_api_key, self.embedding_base_url
        if provider == "openai" and api_key:
            return api_key, base_url
        return None, None

    async def _embed_text(
        self,
        text: str,
        api_key: str,
        base_url: str | None = None,
        headers: dict | None = None,
        model: str = "text-embedding-3-small",
    ) -> list[float] | None:
        """
        Embed `text` via an OpenAI-compatible embeddings endpoint.
        Returns None on any failure (caller degrades to log-only, no retrieval).
        """
        try:
            from openai import AsyncOpenAI
            kw = dict(api_key=api_key)
            if base_url:
                kw["base_url"] = base_url
            if headers:
                kw["default_headers"] = headers
            oai = AsyncOpenAI(**kw)
            resp = await oai.embeddings.create(model=model, input=text)
            return resp.data[0].embedding
        except Exception as e:
            print(f"[simcpi] embedding skipped: {e}")
            return None

    # ── MCPark routes ─────────────────────────────────────────────────────────

    def _register_mcpark(self):
        if not self.mcpark_path:
            return

        path = self.mcpark_path

        # Build server config dict — injected into the HTML so UI pre-fills
        def _server_config(self=self) -> dict:
            return {
                "provider":          self.provider or "openai",
                "api_key_configured": bool(self.api_key),  # never expose the actual key in HTML
                "base_url":          self.base_url,
                "model":             self.model or "gpt-4o",
                "mcp_path":          self.mcp_path,
                "title":             self.title,
                "feedback":          self.feedback_enabled,
                "remote":            self.default_remote,
            }

        @self.api.get(path, response_class=HTMLResponse, include_in_schema=False)
        async def mcpark_ui():
            import json as _json
            config_json = _json.dumps(_server_config())
            html = _load_mcpark_html().replace("__SERVER_CONFIG__", config_json)
            return HTMLResponse(html)

        @self.api.get(f"{path}/skill", include_in_schema=False)
        async def mcpark_skill():
            """Download the simcpi SKILL.md so the user can teach their Claude about simcpi."""
            f = _skill_md_path()
            if not f.exists():
                return JSONResponse({"error": "SKILL.md not bundled with this build"},
                                    status_code=404)
            return FileResponse(path=str(f), filename="SKILL.md", media_type="text/markdown")

        @self.api.post(f"{path}/tools", include_in_schema=False)
        async def mcpark_tools(req: ToolsRequest | None = None):
            try:
                if req and req.mcp_url:
                    # Remote MCP server — fetch over real HTTP
                    transport = self._transport_with_auth(req.mcp_url, req.mcp_auth)
                    async with _MCPInternalClient(transport) as client:
                        tools = await client.list_tools()
                else:
                    async with _MCPInternalClient(self.mcp) as client:
                        tools = await client.list_tools()
            except Exception as e:
                return JSONResponse({"error": f"couldn't reach MCP server: {e}", "tools": []})
            return JSONResponse({
                "tools": [
                    {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                    for t in tools
                ]
            })

        @self.api.post(f"{path}/run", include_in_schema=False)
        async def mcpark_run(req: RunRequest, request: Request):
            # Resolve — request body wins, instance defaults as fallback
            resolved_provider = req.provider or self.provider
            resolved_api_key  = req.api_key  or self.api_key
            resolved_base_url = req.base_url or self.base_url
            resolved_model    = req.model    or self.model or "gpt-4o"

            resolved_headers  = req.default_headers or self.default_headers

            if not resolved_provider or not resolved_api_key:
                return JSONResponse({
                    "error": "provider and api_key are required — set on MCPApi() or pass in the UI",
                    "trace": [], "tools_called": False
                })

            if req.mcp_url:
                # Remote MCP server — never leak OUR auth token to a third party
                mcp_url    = req.mcp_url
                mcp_token  = req.mcp_auth
            else:
                base       = str(request.base_url).rstrip("/")
                mcp_url    = f"{base}{self.mcp_path}/mcp"
                mcp_token  = self.auth_token

            # Fetch tools via real HTTP transport
            try:
                transport = self._transport_with_auth(mcp_url, mcp_token)
                async with _MCPInternalClient(transport) as client:
                    raw_tools = await client.list_tools()
            except Exception as e:
                return JSONResponse({
                    "error": f"couldn't reach MCP server at {mcp_url}: {e}",
                    "trace": [], "tools_called": False
                })

            mcp_tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in raw_tools
            ]

            trace:        list[dict] = []
            tools_called: bool      = False

            # ── Feedback memory: embed prompt + retrieve rated examples ───────
            prompt_embedding = None
            memory_injection = None
            if self._feedback and req.memory:
                emb_key, emb_base = self._embed_creds(
                    resolved_provider, resolved_api_key, resolved_base_url
                )
                if emb_key:
                    prompt_embedding = await self._embed_text(
                        req.prompt, emb_key, emb_base, resolved_headers,
                        model=self.embedding_model,
                    )
                if prompt_embedding:
                    from .feedback import format_memory
                    examples = self._feedback.retrieve(
                        prompt_embedding,
                        k=self.feedback_k,
                        threshold=self.feedback_threshold,
                        only_positive=self.feedback_only_positive,
                    )
                    if examples:
                        memory_injection = format_memory(examples)
                        trace.append({
                            "type": "memory",
                            "examples": [
                                {"prompt": e["prompt"], "rating": e["rating"],
                                 "similarity": e["similarity"]}
                                for e in examples
                            ],
                        })

            try:
                if resolved_provider == "openai":
                    from openai import AsyncOpenAI
                    kw = dict(api_key=resolved_api_key)
                    if resolved_base_url:
                        kw["base_url"] = resolved_base_url
                    if resolved_headers:
                        kw["default_headers"] = resolved_headers
                    oai = AsyncOpenAI(**kw)

                    oai_tools = [
                        {"type": "function", "function": {
                            "name":        t["name"],
                            "description": t.get("description", ""),
                            "parameters":  t.get("inputSchema", {"type": "object", "properties": {}}),
                        }}
                        for t in mcp_tools
                    ]
                    messages = []
                    if memory_injection:
                        messages.append({"role": "system", "content": memory_injection})
                    messages.append({"role": "user", "content": req.prompt})

                    while True:
                        resp = await oai.chat.completions.create(
                            model=resolved_model, messages=messages,
                            tools=oai_tools, tool_choice="auto",
                        )
                        msg = resp.choices[0].message
                        messages.append(msg)

                        if msg.tool_calls:
                            tools_called = True
                            results = []
                            for tc in msg.tool_calls:
                                import json as _j
                                args = _j.loads(tc.function.arguments)
                                trace.append({"type": "tool_call", "name": tc.function.name, "arguments": args})
                                async with _MCPInternalClient(self._transport_with_auth(mcp_url, mcp_token)) as c:
                                    r = await c.call_tool(tc.function.name, args)
                                txt, images = _split_result(r)
                                step = {"type": "tool_result", "name": tc.function.name, "result": txt}
                                if images:
                                    step["images"] = images
                                trace.append(step)
                                results.append({"tool_call_id": tc.id, "role": "tool", "content": txt})
                            messages.extend(results)
                            if not req.assistant:
                                break   # raw tool output only — skip LLM interpretation
                        else:
                            if req.assistant and msg.content:
                                trace.append({"type": "message", "content": msg.content})
                            break

                else:  # anthropic
                    import anthropic as _sdk
                    kw = dict(api_key=resolved_api_key)
                    if resolved_base_url:
                        kw["base_url"] = resolved_base_url
                    if resolved_headers:
                        kw["default_headers"] = resolved_headers
                    ant = _sdk.AsyncAnthropic(**kw)

                    ant_tools = [
                        {"name": t["name"], "description": t.get("description", ""),
                         "input_schema": t.get("inputSchema", {"type": "object", "properties": {}})}
                        for t in mcp_tools
                    ]
                    messages = [{"role": "user", "content": req.prompt}]

                    while True:
                        resp = await ant.messages.create(
                            model=resolved_model, max_tokens=1024,
                            tools=ant_tools, messages=messages,
                            **({"system": memory_injection} if memory_injection else {}),
                        )
                        asst_content, tool_blocks = [], []
                        for block in resp.content:
                            if block.type == "text":
                                asst_content.append({"type": "text", "text": block.text})
                                if req.assistant and block.text:
                                    trace.append({"type": "message", "content": block.text})
                            elif block.type == "tool_use":
                                tools_called = True
                                asst_content.append({"type": "tool_use", "id": block.id,
                                                     "name": block.name, "input": block.input})
                                trace.append({"type": "tool_call", "name": block.name, "arguments": block.input})
                                tool_blocks.append(block)

                        messages.append({"role": "assistant", "content": asst_content})

                        if tool_blocks:
                            results = []
                            for block in tool_blocks:
                                async with _MCPInternalClient(self._transport_with_auth(mcp_url, mcp_token)) as c:
                                    r = await c.call_tool(block.name, block.input)
                                txt, images = _split_result(r)
                                step = {"type": "tool_result", "name": block.name, "result": txt}
                                if images:
                                    step["images"] = images
                                trace.append(step)
                                results.append({"type": "tool_result", "tool_use_id": block.id, "content": txt})
                            messages.append({"role": "user", "content": results})
                            if not req.assistant:
                                break   # raw tool output only — skip LLM interpretation
                        else:
                            break

            except Exception as e:
                return JSONResponse({"error": str(e), "trace": trace, "tools_called": tools_called})

            # ── Feedback memory: log this call (returns the rating handle) ────
            call_id = None
            if self._feedback:
                logged_tools = [
                    {"name": s["name"], "arguments": s.get("arguments")}
                    for s in trace if s.get("type") == "tool_call"
                ]
                call_id = self._feedback.log_call(
                    req.prompt, logged_tools,
                    session_id=req.session_id, embedding=prompt_embedding,
                )

            return JSONResponse({"trace": trace, "tools_called": tools_called, "call_id": call_id})

        @self.api.post(f"{path}/generate-tests", include_in_schema=False)
        async def mcpark_generate_tests(req: TestGenRequest):
            resolved_provider = req.provider or self.provider
            resolved_api_key  = req.api_key  or self.api_key
            resolved_base_url = req.base_url or self.base_url
            resolved_model    = req.model    or self.model or "gpt-4o"
            resolved_headers  = req.default_headers or self.default_headers

            if not resolved_provider or not resolved_api_key:
                return JSONResponse({"error": "provider and api_key required", "tests": []})

            try:
                if req.mcp_url:
                    transport = self._transport_with_auth(req.mcp_url, req.mcp_auth)
                    async with _MCPInternalClient(transport) as client:
                        raw_tools = await client.list_tools()
                else:
                    async with _MCPInternalClient(self.mcp) as client:
                        raw_tools = await client.list_tools()
            except Exception as e:
                return JSONResponse({"error": f"couldn't reach MCP server: {e}", "tests": []})

            tools_info = [
                {
                    "name":        t.name,
                    "description": t.description or "",
                    "params":      list((t.inputSchema or {}).get("properties", {}).keys()),
                }
                for t in raw_tools
            ]

            system_msg = "You generate test prompts for MCP tools. Return only valid JSON."
            user_msg   = (
                "Given these MCP tools, generate test prompts in two categories:\n"
                "1. Single-tool: 2 natural language prompts per tool that would invoke just that tool.\n"
                "2. Multi-tool: always generate exactly 2 prompts that combine multiple tools in one request, "
                "even if the tools are similar (e.g. 'Greet Mohan in both Hindi and Telugu' calls two tools).\n"
                "Prompts should be natural sentences a real user would type, not technical commands.\n\n"
                f"Tools:\n{json.dumps(tools_info, indent=2)}\n\n"
                'Return JSON: {"tests": [{"tool": "tool_name", "prompt": "..."}, {"tool": "multi", "prompt": "..."}, ...]}'
            )

            try:
                if resolved_provider == "openai":
                    from openai import AsyncOpenAI
                    kw = dict(api_key=resolved_api_key)
                    if resolved_base_url: kw["base_url"] = resolved_base_url
                    if resolved_headers:  kw["default_headers"] = resolved_headers
                    oai  = AsyncOpenAI(**kw)
                    resp = await oai.chat.completions.create(
                        model=resolved_model,
                        messages=[
                            {"role": "system", "content": system_msg},
                            {"role": "user",   "content": user_msg},
                        ],
                    )
                    raw = resp.choices[0].message.content
                else:
                    import anthropic as _sdk
                    kw = dict(api_key=resolved_api_key)
                    if resolved_base_url: kw["base_url"] = resolved_base_url
                    if resolved_headers:  kw["default_headers"] = resolved_headers
                    ant  = _sdk.AsyncAnthropic(**kw)
                    resp = await ant.messages.create(
                        model=resolved_model,
                        max_tokens=1024,
                        system=system_msg,
                        messages=[{"role": "user", "content": user_msg}],
                    )
                    raw = resp.content[0].text

                tests = json.loads(raw)["tests"]
                return JSONResponse({"tests": tests})

            except Exception as e:
                return JSONResponse({"error": str(e), "tests": []})

        # ── Docstring optimizer ──────────────────────────────────────────────

        @self.api.post(f"{path}/optimize", include_in_schema=False)
        async def mcpark_optimize(req: OptimizeRequest):
            if not self._feedback:
                return JSONResponse(
                    {"error": "feedback memory is disabled — the optimizer scores "
                              "docstrings against your 👍/👎 ratings. Start with "
                              "MCPApi(feedback=True) and rate some runs first."},
                    status_code=400,
                )

            resolved_provider = req.provider or self.provider
            resolved_api_key  = req.api_key  or self.api_key
            resolved_base_url = req.base_url or self.base_url
            resolved_model    = req.model    or self.model or "gpt-4o"
            resolved_headers  = req.default_headers or self.default_headers

            if not resolved_provider or not resolved_api_key:
                return JSONResponse({"error": "provider and api_key required", "results": []})

            rated = self._feedback.rated_calls()
            if not rated:
                return JSONResponse(
                    {"error": "no rated runs yet — run prompts and rate them 👍/👎 first",
                     "results": []})

            async with _MCPInternalClient(self.mcp) as client:
                raw_tools = await client.list_tools()
            tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in raw_tools
            ]

            from .optimize import make_selector, make_generator, optimize_tool
            select_fn   = make_selector(resolved_provider, resolved_api_key,
                                        resolved_model, resolved_base_url, resolved_headers)
            generate_fn = make_generator(resolved_provider, resolved_api_key,
                                         resolved_model, resolved_base_url, resolved_headers)

            targets = [t for t in tools if t["name"] == req.tool_name] if req.tool_name else tools
            if not targets:
                return JSONResponse({"error": f"unknown tool: {req.tool_name}", "results": []})

            results = []
            try:
                for t in targets:
                    results.append(await optimize_tool(t, tools, rated, select_fn, generate_fn))
            except Exception as e:
                return JSONResponse({"error": str(e), "results": results})
            return JSONResponse({"results": results})

        @self.api.post(f"{path}/apply-docstring", include_in_schema=False)
        async def mcpark_apply_docstring(req: ApplyDocstringRequest):
            ok = await self._set_tool_description(req.tool, req.description)
            if not ok:
                return JSONResponse(
                    {"ok": False,
                     "error": "couldn't update the live tool — copy the docstring "
                              "into your function instead"},
                    status_code=400,
                )
            return JSONResponse({"ok": True,
                                 "note": "live for this server session — paste it into "
                                         "your function's docstring to keep it"})

        @self.api.post(f"{path}/write-docstring", include_in_schema=False)
        async def mcpark_write_docstring(req: ApplyDocstringRequest, request: Request):
            # Editing files on disk is a localhost-only dev convenience — never
            # let a remote/exposed MCPark rewrite the author's source.
            if not self._is_local_request(request):
                return JSONResponse(
                    {"ok": False,
                     "error": "writing to source is allowed only from localhost"},
                    status_code=403,
                )
            result = self._write_docstring_to_source(req.tool, req.description)
            if result["ok"]:
                await self._set_tool_description(req.tool, req.description)  # sync live too
                return JSONResponse(result)
            return JSONResponse(result, status_code=400)

        # ── Feedback memory routes ───────────────────────────────────────────

        @self.api.post(f"{path}/rate", include_in_schema=False)
        async def mcpark_rate(req: RateRequest):
            if not self._feedback:
                return JSONResponse({"error": "feedback memory is disabled"}, status_code=400)
            ok = self._feedback.set_rating(req.call_id, req.rating, req.note)
            if not ok:
                return JSONResponse({"error": "unknown call_id", "ok": False}, status_code=404)
            return JSONResponse({"ok": True, **self._feedback.stats()})

        @self.api.get(f"{path}/feedback-stats", include_in_schema=False)
        async def mcpark_feedback_stats():
            if not self._feedback:
                return JSONResponse({"enabled": False})
            return JSONResponse({"enabled": True, **self._feedback.stats()})

        @self.api.get(f"{path}/feedback-rows", include_in_schema=False)
        async def mcpark_feedback_rows(limit: int = 100):
            if not self._feedback:
                return JSONResponse({"error": "feedback memory is disabled"}, status_code=400)
            return JSONResponse({
                "stats": self._feedback.stats(),
                "db":    self._feedback.db_path,
                "rows":  self._feedback.recent_calls(limit),
            })

        @self.api.post(f"{path}/feedback-clear", include_in_schema=False)
        async def mcpark_feedback_clear(request: Request):
            if not self._feedback:
                return JSONResponse({"error": "feedback memory is disabled"}, status_code=400)
            # Destructive — never let a remote/exposed MCPark wipe collected data.
            if not self._is_local_request(request):
                return JSONResponse(
                    {"error": "clearing the feedback DB is allowed only from localhost"},
                    status_code=403,
                )
            removed = self._feedback.clear()
            return JSONResponse({"ok": True, "removed": removed, **self._feedback.stats()})

        @self.api.post(f"{path}/embed-backfill", include_in_schema=False)
        async def mcpark_embed_backfill(req: TestGenRequest):
            if not self._feedback:
                return JSONResponse({"error": "feedback memory is disabled"}, status_code=400)
            resolved_provider = req.provider or self.provider
            resolved_api_key  = req.api_key  or self.api_key
            resolved_base_url = req.base_url or self.base_url
            resolved_headers  = req.default_headers or self.default_headers

            emb_key, emb_base = self._embed_creds(
                resolved_provider, resolved_api_key, resolved_base_url
            )
            if not emb_key:
                return JSONResponse(
                    {"error": "backfill needs an embeddings key — set embedding_api_key "
                              "on MCPApi(), or use an OpenAI key"},
                    status_code=400,
                )

            async def _embed(text: str):
                return await self._embed_text(
                    text, emb_key, emb_base, resolved_headers,
                    model=self.embedding_model,
                )

            # backfill_embeddings is sync + expects a sync embed_fn; collect rows
            # and embed them here so we can await the async embedder.
            embedded = 0
            with self._feedback._conn() as c:
                rows = c.execute(
                    "SELECT id, prompt FROM calls WHERE embedding IS NULL AND prompt IS NOT NULL"
                ).fetchall()
            for cid, prompt in rows:
                vec = await _embed(prompt)
                if not vec:
                    continue
                from .feedback import _pack
                with self._feedback._lock, self._feedback._conn() as c:
                    c.execute("UPDATE calls SET embedding=? WHERE id=?", (_pack(vec), cid))
                embedded += 1

            return JSONResponse({"embedded": embedded, **self._feedback.stats()})

    # ── File serving ─────────────────────────────────────────────────────────

    @staticmethod
    def _temp_dir() -> pathlib.Path:
        d = pathlib.Path(tempfile.gettempdir()) / "simcpi_files"
        d.mkdir(exist_ok=True)
        return d

    @staticmethod
    def _sweep_temp_dir(max_age: float = 86400):
        """Best-effort removal of simcpi_files temp files left by earlier runs."""
        d = pathlib.Path(tempfile.gettempdir()) / "simcpi_files"
        if not d.is_dir():
            return
        cutoff = time.time() - max_age
        for f in d.iterdir():
            with contextlib.suppress(OSError):
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()

    def _evict_file(self, token: str):
        entry = self._file_store.pop(token, None)
        if entry is not None and entry[2]:  # is_temp — only delete files we created
            with contextlib.suppress(OSError):
                entry[0].unlink()

    def _prune_file_store(self):
        now = time.time()
        if self.file_ttl is not None:
            for token, (_, created, _) in list(self._file_store.items()):
                if now - created > self.file_ttl:
                    self._evict_file(token)
        while len(self._file_store) > self.file_store_max:
            self._evict_file(next(iter(self._file_store)))

    def _store_path(self, path: pathlib.Path, is_temp: bool) -> str:
        """Register a path in the file store; returns its access token."""
        token = uuid.uuid4().hex
        self._file_store[token] = (path, time.time(), is_temp)
        self._prune_file_store()
        return token

    def _stash_bytes(self, filename: str, data: bytes) -> str:
        """Write bytes to the simcpi temp dir and register them; returns token."""
        token     = uuid.uuid4().hex
        file_path = self._temp_dir() / f"{token}_{filename}"
        file_path.write_bytes(data)
        self._file_store[token] = (file_path, time.time(), True)
        self._prune_file_store()
        return token

    def _register_files_route(self):
        """Register GET /files/{token}/{filename} for served files."""

        @self.api.get("/files/{token}/{filename}", include_in_schema=False)
        async def serve_file_route(token: str, filename: str):
            entry = self._file_store.get(token)
            if entry is not None and self.file_ttl is not None \
                    and time.time() - entry[1] > self.file_ttl:
                self._evict_file(token)
                entry = None
            if entry is None or not entry[0].exists():
                return JSONResponse({"error": "File not found or expired"}, status_code=404)
            return FileResponse(
                path=str(entry[0]),
                filename=filename,
                media_type=_mime_type(filename),
            )

    def serve_file(
        self,
        filename: str,
        content,
        host:     str = "localhost",
        port:     int = 8000,
    ) -> str:
        """
        Serve a file through FastAPI and return its URL.
        Pass your object directly — simcpi handles serialization automatically.
        MCPark will preview images/PDFs inline and show a download button for others.

        Parameters
        ----------
        filename: Name of the file e.g. "report.xlsx", "chart.png"
        content:  Your object — pandas DataFrame, matplotlib Figure,
                  raw bytes, or a file path string.
        host:     Host where this server runs (default: localhost).
        port:     Port where this server runs (default: 8000).

        Returns
        -------
        str — URL the client/MCPark can open or download.

        Usage:
            # pandas DataFrame → Excel
            return app.serve_file("report.xlsx", df)

            # pandas DataFrame → CSV
            return app.serve_file("report.csv", df)

            # matplotlib Figure → PNG/PDF
            return app.serve_file("chart.png", fig)

            # existing file on disk
            return app.serve_file("report.pdf", "/path/to/report.pdf")

            # raw bytes
            return app.serve_file("data.zip", zip_bytes)
        """
        import io as _io

        ext = pathlib.Path(filename).suffix.lower()

        # ── Serialise based on object type + extension ────────────────────────
        if isinstance(content, (str, pathlib.Path)):
            path_str = str(content)
            if path_str.startswith("http://") or path_str.startswith("https://"):
                # Remote URL — download and serve
                import urllib.request as _req
                with _req.urlopen(path_str) as r:
                    data = r.read()
                token = self._stash_bytes(filename, data)
                return f"http://{host}:{port}/files/{token}/{filename}"
            else:
                # Local file path — serve directly, no copy needed
                token = self._store_path(pathlib.Path(content), is_temp=False)
                return f"http://{host}:{port}/files/{token}/{filename}"

        elif isinstance(content, bytes):
            data = content

        else:
            # Try pandas DataFrame
            try:
                import pandas as _pd
                if isinstance(content, _pd.DataFrame):
                    buf = _io.BytesIO()
                    if ext in (".xlsx", ".xls"):
                        content.to_excel(buf, index=False)
                    elif ext == ".csv":
                        buf.write(content.to_csv(index=False).encode())
                    elif ext == ".json":
                        buf.write(content.to_json(orient="records").encode())
                    else:
                        content.to_excel(buf, index=False)
                    token = self._stash_bytes(filename, buf.getvalue())
                    return f"http://{host}:{port}/files/{token}/{filename}"
            except ImportError:
                pass

            # Try matplotlib Figure
            try:
                import matplotlib.pyplot as _plt
                from matplotlib.figure import Figure as _Figure
                if isinstance(content, _Figure):
                    buf = _io.BytesIO()
                    fmt = ext.lstrip(".") or "png"
                    content.savefig(buf, format=fmt, bbox_inches="tight")
                    token = self._stash_bytes(filename, buf.getvalue())
                    return f"http://{host}:{port}/files/{token}/{filename}"
            except ImportError:
                pass

            raise TypeError(
                f"[simcpi] serve_file() doesn't know how to serialize {type(content).__name__}. "
                "Pass a DataFrame, matplotlib Figure, bytes, or a file path."
            )

        # ── Write raw bytes ───────────────────────────────────────────────────
        token = self._stash_bytes(filename, data)
        return f"http://{host}:{port}/files/{token}/{filename}"

    # ── @create_tool_api decorator ────────────────────────────────────────────

    def create_tool_api(
        self,
        path: str,
        *,
        method:    Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST",
        tool_name: str | None = None,
        tags:      list[str] | None = None,
        summary:   str | None = None,
    ) -> Callable:
        """
        Register a function as both a FastAPI REST endpoint and a FastMCP tool.

        The function's docstring becomes the MCP tool description —
        write it for the LLM, not just for humans.

        FastAPI Depends() parameters are automatically stripped from the MCP tool
        schema — the LLM never sees them. They will be None when called via MCP,
        so handle that case if your function uses them for more than auth.

        Usage:
            @app.create_tool_api("/add", method="POST")
            def add(a: int, b: int) -> int:
                \"\"\"
                Add two numbers.
                Use this when the user asks to sum or total numbers.
                \"\"\"
                return a + b
        """
        def decorator(fn: Callable) -> Callable:
            name = tool_name or fn.__name__
            doc  = cleandoc(fn.__doc__) if fn.__doc__ else ""

            # 1. FastMCP tool — strip Depends() params so they don't appear in schema
            self.mcp.tool(name=name, description=doc)(_make_mcp_fn(fn))

            # 2. FastAPI route — original function, Depends() intact
            @functools.wraps(fn)
            async def route_handler(*args, **kwargs):
                if inspect.iscoroutinefunction(fn):
                    return await fn(*args, **kwargs)
                return fn(*args, **kwargs)

            route_handler.__annotations__ = get_type_hints(fn)
            route_handler.__doc__ = doc

            getattr(self.api, method.lower())(
                path, summary=summary or name,
                description=doc, tags=tags, name=name,
            )(route_handler)

            self._registry[name] = fn
            return fn

        return decorator

    # ── @create_resource decorator ────────────────────────────────────────────

    def create_resource(self, uri: str) -> Callable:
        """
        Register a function as a FastMCP resource (MCP only — no REST route).

        Resources provide context and data that LLM clients can read.
        They are not callable tools — the LLM reads them for information.
        Use them to expose documentation, usage guides, or dynamic data
        the LLM should have access to when working with this server.

        Supports static and template URIs:
            "docs://usage-guide"          — static resource
            "data://{user_id}/profile"    — dynamic resource (URI template)

        Usage:
            @app.create_resource("docs://usage-guide")
            def usage_guide() -> str:
                \"\"\"How to use this MCP server and its tools.\"\"\"
                return "This server has greet and report tools..."

            @app.create_resource("users://{user_id}/profile")
            def user_profile(user_id: str) -> str:
                \"\"\"Get a user profile by ID.\"\"\"
                return f"Profile data for {user_id}"
        """
        def decorator(fn: Callable) -> Callable:
            self.mcp.resource(uri)(fn)
            self._resource_registry[uri] = fn
            return fn

        return decorator

    # ── ASGI passthrough ──────────────────────────────────────────────────────

    async def __call__(self, scope, receive, send):
        await self.api(scope, receive, send)

    # ── get_client — returns a wired MCPClient ────────────────────────────────

    def get_client(
        self,
        provider: Literal["openai", "anthropic"] | None = None,
        api_key:  str | None = None,
        model:    str | None = None,
        system:   str | None = None,
        timeout:  int = 30,
        base_url: str | None = None,
    ) -> "MCPClient":
        """
        Return an MCPClient pre-wired to this server's MCP instance (in-process).

        Falls back to provider/api_key/base_url/model set on MCPApi().

        Usage:
            app = MCPApi(provider="openai", api_key="sk-...")
            client = app.get_client()
            result = await client.run("Add 42 and 58")
        """
        from .mcpclient import MCPClient

        resolved_provider = provider or self.provider
        resolved_api_key  = api_key  or self.api_key
        resolved_base_url = base_url or self.base_url
        resolved_model    = model    or self.model

        if not resolved_provider:
            raise ValueError("provider required — set on MCPApi() or pass to get_client()")
        if not resolved_api_key:
            raise ValueError("api_key required — set on MCPApi() or pass to get_client()")

        return MCPClient(
            mcp_server=self.mcp,
            provider=resolved_provider,
            api_key=resolved_api_key,
            model=resolved_model,
            system=system,
            timeout=timeout,
            base_url=resolved_base_url,
            default_headers=self.default_headers,
        )

    def list_tools(self) -> list[str]:
        """Return names of all registered tools."""
        return list(self._registry.keys())

    def list_resources(self) -> list[str]:
        """Return URIs of all registered resources."""
        return list(self._resource_registry.keys())

    def add_resource(self, resource) -> None:
        """
        Add a static resource to the MCP server.

        Accepts any FastMCP resource object — TextResource, FileResource,
        DirectoryResource, BinaryResource, or HttpResource.

        Usage:
            from simcpi import TextResource, FileResource, DirectoryResource

            app.add_resource(TextResource(
                uri="docs://usage-guide",
                name="Usage Guide",
                text="This server has greet and report tools...",
            ))

            app.add_resource(FileResource(
                uri="file://readme",
                path="README.md",
                mime_type="text/markdown",
            ))

            app.add_resource(DirectoryResource(
                uri="resource://data-files",
                path="./data",
                recursive=False,
            ))
        """
        self.mcp.add_resource(resource)
        self._resource_registry[str(resource.uri)] = resource

    def connect_claude(
        self,
        host:    str  = "localhost",
        port:    int  = 8000,
        launch:  bool = False,
        force:   bool = False,
        native:  bool = False,
        url:     str | None = None,
    ) -> str:
        """
        Register this MCP server in Claude Desktop config automatically.
        Restart Claude Desktop after calling this.

        Parameters
        ----------
        url:     Full MCP URL — use this for remote servers e.g. "https://myapi.com/mcp/mcp".
                 If provided, host and port are ignored.
        host:    Host where this server runs (default: localhost).
        port:    Port where this server runs (default: 8000).
        force:   Overwrite existing entry with same name (default: False).
        native:  True  — write {"type":"streamableHttp","url":...} (newer Claude Desktop builds).
                 False — write {"command":"npx","args":["mcp-remote",...]} (default, works everywhere).
        launch:  True — open Claude Desktop and show a Ctrl+R reminder notification.
        """
        return connect_claude(
            title=self.title, port=port, launch=launch, url=url, force=force,
            native=native, mcp_path=self.mcp_path, host=host,
            auth_token=self.auth_token,
        )


    def export_mcpb(
        self,
        output_path: str | None = None,
        host: str = "localhost",
        port: int = 8000,
        version: str = "0.1.0",
        description: str | None = None,
    ) -> str:
        """
        Export this MCP server as a .mcpb bundle for one-click install in Claude Desktop.

        The generated .mcpb file can be:
          - Double-clicked to install in Claude Desktop
          - Dragged into the Claude Desktop window
          - Shared with your team

        Parameters
        ----------
        output_path:  Where to save the .mcpb file. Defaults to ./<title>.mcpb
        host:         Host where this HTTP server runs (for the manifest URL).
        port:         Port where this HTTP server runs.
        version:      Bundle version string.
        description:  Bundle description shown during install.

        Returns
        -------
        Path to the generated .mcpb file.

        Usage:
            app = MCPApi(title="My API")

            @app.create_tool_api("/add", method="POST")
            def add(a: int, b: int) -> int:
                \"\"\"Add two numbers.\"\"\"
                return a + b

            app.export_mcpb()  # generates My API.mcpb
        """
        import json, zipfile, textwrap, pathlib as _pl

        title       = self.title
        safe_title  = title.replace(" ", "_")
        out         = _pl.Path(output_path or f"{safe_title}.mcpb")
        mcp_url     = f"http://{host}:{port}{self.mcp_path}/mcp"
        desc        = description or f"{title} — generated by simcpi"

        # ── Build tool list for manifest ──────────────────────────────────────
        tools_manifest = []
        for name, fn in self._registry.items():
            from inspect import cleandoc, signature
            import typing
            doc = cleandoc(fn.__doc__) if fn.__doc__ else ""
            sig = signature(fn)
            props = {}
            required = []
            hints = typing.get_type_hints(fn)
            for pname, param in sig.parameters.items():
                ptype = hints.get(pname, str)
                type_map = {int: "integer", float: "number", str: "string", bool: "boolean"}
                props[pname] = {"type": type_map.get(ptype, "string")}
                if param.default is param.empty:
                    required.append(pname)
            tools_manifest.append({
                "name":        name,
                "description": doc,
                "inputSchema": {
                    "type":       "object",
                    "properties": props,
                    "required":   required,
                }
            })

        # ── manifest.json ─────────────────────────────────────────────────────
        manifest = {
            "schema_version": "v1",
            "name":           safe_title,
            "display_name":   title,
            "version":        version,
            "description":    desc,
            "compatibility":  {"platforms": ["darwin", "win32"]},
            "server": {
                "type":    "http",
                "url":     mcp_url,
                "transport": "streamable-http",
            },
            "tools": tools_manifest,
        }

        # ── README inside the bundle ──────────────────────────────────────────
        readme = textwrap.dedent(f"""
            # {title}

            Generated by simcpi.

            ## Install
            Double-click {safe_title}.mcpb in Claude Desktop.

            ## MCP Server URL
            {mcp_url}

            ## Tools
            {chr(10).join(f"- {t['name']}: {t['description'].splitlines()[0] if t['description'] else ''}" for t in tools_manifest)}
        """).strip()

        # ── Write .mcpb (ZIP) ─────────────────────────────────────────────────
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("README.md",     readme)

        print(f"[simcpi] Exported {out}")
        print(f"[simcpi] Double-click {out} to install in Claude Desktop")
        print(f"[simcpi] Make sure your server is running at {mcp_url}")
        return str(out)
    def connect_cursor(
        self,
        host:    str  = "localhost",
        port:    int  = 8000,
        launch:  bool = False,
        force:   bool = False,
        scope:   str  = "global",
        url:     str | None = None,
    ) -> str:
        """
        Register this MCP server in Cursor's MCP config automatically.
        Restart Cursor after calling this.

        Parameters
        ----------
        url:    Full MCP URL — use this for remote servers e.g. "https://myapi.com/mcp/mcp".
                If provided, host and port are ignored.
        host:   Host where this server runs (default: localhost).
        port:   Port where this server runs (default: 8000).
        launch: True — open Cursor and show a restart reminder notification.
        force:  Overwrite existing entry with same key (default: False).
        scope:  "global"  — writes to ~/.cursor/mcp.json (default).
                "project" — writes to .cursor/mcp.json in current directory.
        """
        return connect_cursor(
            title=self.title, port=port, launch=launch, url=url, force=force,
            scope=scope, mcp_path=self.mcp_path, host=host,
            auth_token=self.auth_token,
        )

    def get_claude_config(self, host: str = "localhost", port: int = 8000, native: bool = False) -> dict:  # noqa: E501
        """
        Return the Claude Desktop config snippet for this server.

        Parameters
        ----------
        native: True — {"type":"streamableHttp","url":...} format.
                False — {"command":"npx","args":["mcp-remote",...]} format (default).
        """
        mcp_url = f"http://{host}:{port}{self.mcp_path}/mcp"
        entry   = _mcp_config_entry(mcp_url, native, self.auth_token)
        return {"mcpServers": {self.title: entry}}


# ─────────────────────────────────────────────────────────────────────────────
# Claude Desktop launch + notify helpers
# ─────────────────────────────────────────────────────────────────────────────

def _launch_claude_desktop() -> bool:
    import platform, subprocess, pathlib, os
    system = platform.system()
    try:
        if system == "Darwin":
            claude_app = pathlib.Path("/Applications/Claude.app")
            if not claude_app.exists():
                print("[simcpi] Claude Desktop not found — install it from claude.ai")
                return False
            subprocess.Popen(["open", "-a", "Claude"])
            return True
        elif system == "Windows":
            # 1) Classic standalone installer location.
            exe = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "AnthropicClaude" / "claude.exe"
            if exe.exists():
                subprocess.Popen([str(exe)])
                return True
            # 2) Microsoft Store / Start-menu install — can't launch by exe path
            #    (WindowsApps is ACL-locked); launch via its Start app ID instead.
            ps = (
                "$a = Get-StartApps | Where-Object { $_.Name -like '*Claude*' } | "
                "Select-Object -First 1; "
                "if ($a) { Start-Process \"shell:AppsFolder\\$($a.AppID)\"; exit 0 } else { exit 1 }"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
            )
            if r.returncode == 0:
                return True
            print("[simcpi] Claude Desktop not found — install it from claude.ai")
            return False
        else:
            print("[simcpi] Auto-launch not supported on this platform.")
            return False
    except Exception as e:
        print(f"[simcpi] Could not launch Claude Desktop: {e}")
        return False


def _launch_cursor() -> bool:
    import platform, subprocess, pathlib, os
    system = platform.system()
    try:
        if system == "Darwin":
            cursor_app = pathlib.Path("/Applications/Cursor.app")
            if not cursor_app.exists():
                print("[simcpi] Cursor not found — install it from cursor.com")
                return False
            subprocess.Popen(["open", "-a", "Cursor"])
            return True
        elif system == "Windows":
            exe = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "cursor" / "Cursor.exe"
            if not exe.exists():
                print("[simcpi] Cursor not found — install it from cursor.com")
                return False
            subprocess.Popen([str(exe)])
            return True
        else:
            print("[simcpi] Auto-launch not supported on this platform.")
            return False
    except Exception as e:
        print(f"[simcpi] Could not launch Cursor: {e}")
        return False


def _restart_popup(title: str, body: str) -> None:
    """
    Show a small 'restart to apply' popup in a DETACHED subprocess, so it stays
    on screen even if the calling script exits a moment later. Uses a tiny
    self-contained tkinter script (stdlib, cross-platform). Falls back to a
    printed line if no GUI can be created.
    """
    import sys, os, json, subprocess

    code = (
        "import tkinter as tk\n"
        "r=tk.Tk();r.title('simcpi');r.resizable(False,False);r.attributes('-topmost',True)\n"
        "W,H=440,210;sw=r.winfo_screenwidth();sh=r.winfo_screenheight()\n"
        "r.geometry(f'{W}x{H}+{(sw-W)//2}+{(sh-H)//2}');BG='#0f0f1a';r.configure(bg=BG)\n"
        "fr=tk.Frame(r,bg=BG,padx=28,pady=24);fr.pack(fill='both',expand=True)\n"
        f"tk.Label(fr,text={json.dumps(title)},font=('Arial',17,'bold'),fg='#ffffff',bg=BG).pack(anchor='w')\n"
        f"tk.Label(fr,text={json.dumps(body)},font=('Arial',12),fg='#94a3b8',bg=BG,justify='left').pack(anchor='w',pady=(10,18))\n"
        "tk.Button(fr,text='Got it',command=r.destroy,font=('Arial',11,'bold'),bg='#4f46e5',fg='#ffffff',"
        "activebackground='#6366f1',activeforeground='#ffffff',relief='flat',padx=22,pady=6,cursor='hand2',bd=0).pack(anchor='w')\n"
        "r.lift();r.after(50,lambda:(r.attributes('-topmost',True),r.focus_force()))\n"
        "r.mainloop()\n"
    )

    exe = sys.executable
    flags = 0
    if os.name == "nt":
        pythonw = exe.replace("python.exe", "pythonw.exe")  # avoid a console flash
        if os.path.exists(pythonw):
            exe = pythonw
        flags = 0x00000008  # DETACHED_PROCESS — outlives the parent script

    try:
        subprocess.Popen([exe, "-c", code], creationflags=flags, close_fds=True)
    except Exception:
        print(f"[simcpi] {title} — {body}".replace("\n", " "))


def _notify_restart_claude() -> None:
    _restart_popup(
        "Claude Desktop updated",
        "Press  Ctrl + R  inside Claude Desktop\nto reload and apply the new MCP server.",
    )


def _notify_restart_cursor() -> None:
    _restart_popup(
        "Cursor updated",
        "Restart Cursor to apply\nthe new MCP server.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Standalone connect helpers — no MCPApi instance needed
# ─────────────────────────────────────────────────────────────────────────────

def _mcp_config_entry(mcp_url: str, native: bool, auth_token: str | None) -> dict:
    """Build a client-config entry for an MCP server (mcp-remote or native)."""
    if native:
        entry = {"type": "streamableHttp", "url": mcp_url}
        if auth_token:
            entry["headers"] = {"Authorization": f"Bearer {auth_token}"}
        return entry
    args = ["mcp-remote", mcp_url]
    if auth_token:
        args += ["--header", f"Authorization: Bearer {auth_token}"]
    return {"command": "npx", "args": args}


def connect_claude(
    title:    str,
    port:     int  = 8000,
    launch:   bool = False,
    url:      str | None = None,
    force:    bool = False,
    native:   bool = False,
    mcp_path: str  = "/mcp",
    host:     str  = "localhost",
    auth_token: str | None = None,
) -> str:
    """
    Register an MCP server in Claude Desktop config without an MCPApi instance.

    Parameters
    ----------
    title:    Server name — used as key in config e.g. "my-api".
    port:     Local port (ignored if url is given).
    url:      Full remote URL e.g. "https://myapi.com/mcp/mcp".
    force:    Overwrite existing entry (default: False).
    native:   True — streamableHttp format. False — mcp-remote (default).
    mcp_path: MCP path prefix (default: "/mcp").
    host:     Host where the server runs (default: localhost).
    auth_token: Bearer token if the server uses MCPApi(auth_token=...).
    launch:   True — open Claude Desktop and show a Ctrl+R reminder notification.
    """
    import json, pathlib, platform, os

    system = platform.system()
    if system == "Darwin":
        config_path = pathlib.Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    elif system == "Windows":
        config_path = pathlib.Path(os.environ["APPDATA"]) / "Claude/claude_desktop_config.json"
    else:
        config_path = pathlib.Path.home() / ".config/Claude/claude_desktop_config.json"

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # NEVER overwrite a config we can't parse — that would wipe the
            # user's other MCP servers and preferences. Abort, leave it intact.
            print(f"[simcpi] {config_path} is not valid JSON — leaving it "
                  f"untouched. Fix the file and re-run.")
            return str(config_path)
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)

    if not isinstance(config, dict):
        print(f"[simcpi] {config_path} is not a JSON object — leaving it untouched.")
        return str(config_path)
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    mcp_url   = url if url else f"http://{host}:{port}{mcp_path}/mcp"
    entry_key = title if url else f"{title}:{port}"

    entry = _mcp_config_entry(mcp_url, native, auth_token)

    if force or config["mcpServers"].get(entry_key) != entry:
        config["mcpServers"][entry_key] = entry
        # atomic write: temp file + replace, so an interruption can't corrupt
        # or truncate the user's existing config.
        tmp = config_path.with_name(config_path.name + ".simcpi-tmp")
        tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
        tmp.replace(config_path)
        print(f"[simcpi] Connected '{entry_key}' -> {mcp_url}")
        print(f"[simcpi] Config  : {config_path}")
    else:
        print(f"[simcpi] '{entry_key}' already up to date in Claude config.")

    # launch/notify always honoured when requested — even if the config was
    # already current (otherwise launch=True silently does nothing on re-runs).
    print(f"[simcpi] Restart Claude Desktop to apply.")
    if launch and _launch_claude_desktop():
        _notify_restart_claude()
    return str(config_path)


def connect_cursor(
    title:    str,
    port:     int  = 8000,
    launch:   bool = False,
    url:      str | None = None,
    force:    bool = False,
    scope:    str  = "global",
    mcp_path: str  = "/mcp",
    host:     str  = "localhost",
    auth_token: str | None = None,
) -> str:
    """
    Register an MCP server in Cursor config without an MCPApi instance.

    Parameters
    ----------
    title:    Server name — used as key in config.
    port:     Local port (ignored if url is given).
    launch:   True — open Cursor and show a restart reminder notification.
    url:      Full remote URL e.g. "https://myapi.com/mcp/mcp".
    force:    Overwrite existing entry (default: False).
    scope:    "global" — ~/.cursor/mcp.json. "project" — .cursor/mcp.json.
    mcp_path: MCP path prefix (default: "/mcp").
    host:     Host where the server runs (default: localhost).
    auth_token: Bearer token if the server uses MCPApi(auth_token=...).
    """
    import json, pathlib

    config_path = (pathlib.Path.home() / ".cursor" / "mcp.json") if scope == "global" \
                  else (pathlib.Path.cwd() / ".cursor" / "mcp.json")

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[simcpi] {config_path} is not valid JSON — leaving it "
                  f"untouched. Fix the file and re-run.")
            return str(config_path)
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)

    if not isinstance(config, dict):
        print(f"[simcpi] {config_path} is not a JSON object — leaving it untouched.")
        return str(config_path)
    if "mcpServers" not in config:
        config["mcpServers"] = {}

    mcp_url   = url if url else f"http://{host}:{port}{mcp_path}/mcp"
    entry_key = title if url else f"{title}:{port}"

    entry = _mcp_config_entry(mcp_url, native=False, auth_token=auth_token)

    if force or config["mcpServers"].get(entry_key) != entry:
        config["mcpServers"][entry_key] = entry
        tmp = config_path.with_name(config_path.name + ".simcpi-tmp")
        tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
        tmp.replace(config_path)
        print(f"[simcpi] Connected '{entry_key}' -> {mcp_url}")
        print(f"[simcpi] Config  : {config_path}")
    else:
        print(f"[simcpi] '{entry_key}' already up to date in Cursor config.")

    print(f"[simcpi] Restart Cursor to apply.")
    if launch and _launch_cursor():
        _notify_restart_cursor()
    return str(config_path)
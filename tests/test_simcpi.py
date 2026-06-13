"""
Core simcpi tests — file-store lifecycle, auth middleware, feedback memory,
client-config generation. Run: pytest tests/ -q
"""

import os
import json
import time
import pathlib

import pytest
from fastapi.testclient import TestClient

from simcpi import MCPApi, FeedbackMemory
from simcpi.mcpserver import _mcp_config_entry, _rewrite_docstring_in_source


# ─────────────────────────────────────────────────────────────────────────────
# File store — TTL, LRU cap, temp cleanup
# ─────────────────────────────────────────────────────────────────────────────

def test_serve_file_bytes_roundtrip():
    app = MCPApi(title="t-files", mcpark_path=None)
    url = app.serve_file("hello.txt", b"hello world")
    token = url.split("/files/")[1].split("/")[0]
    assert token in app._file_store
    path, _, is_temp = app._file_store[token]
    assert is_temp and path.exists()

    with TestClient(app.api) as client:
        r = client.get(f"/files/{token}/hello.txt")
        assert r.status_code == 200
        assert r.content == b"hello world"


def test_file_ttl_expiry_deletes_temp_file():
    app = MCPApi(title="t-ttl", mcpark_path=None, file_ttl=3600)
    url = app.serve_file("x.txt", b"data")
    token = url.split("/files/")[1].split("/")[0]
    path = app._file_store[token][0]

    # age the entry past the TTL
    app._file_store[token] = (path, time.time() - 7200, True)

    with TestClient(app.api) as client:
        r = client.get(f"/files/{token}/x.txt")
        assert r.status_code == 404
    assert token not in app._file_store
    assert not path.exists()


def test_file_store_cap_evicts_oldest():
    app = MCPApi(title="t-cap", mcpark_path=None, file_store_max=3)
    urls = [app.serve_file(f"f{i}.txt", f"d{i}".encode()) for i in range(5)]
    assert len(app._file_store) == 3
    first_token = urls[0].split("/files/")[1].split("/")[0]
    last_token = urls[-1].split("/files/")[1].split("/")[0]
    assert first_token not in app._file_store
    assert last_token in app._file_store


def test_user_paths_never_deleted_on_eviction(tmp_path):
    app = MCPApi(title="t-user", mcpark_path=None)
    user_file = tmp_path / "mine.txt"
    user_file.write_text("precious")

    url = app.serve_file("mine.txt", str(user_file))
    token = url.split("/files/")[1].split("/")[0]
    assert app._file_store[token][2] is False  # not temp
    app._evict_file(token)
    assert user_file.exists()


def test_prune_on_store_keeps_fresh_entries():
    app = MCPApi(title="t-prune", mcpark_path=None, file_ttl=3600)
    u1 = app.serve_file("a.txt", b"a")
    t1 = u1.split("/files/")[1].split("/")[0]
    # age it, then trigger pruning via a new serve_file
    p1 = app._file_store[t1][0]
    app._file_store[t1] = (p1, time.time() - 7200, True)
    app.serve_file("b.txt", b"b")
    assert t1 not in app._file_store
    assert not p1.exists()


def test_sweep_temp_dir_removes_stale_files():
    d = MCPApi._temp_dir()
    stale = d / "stale_test_file.txt"
    stale.write_bytes(b"old")
    old = time.time() - 90000  # > 24h
    os.utime(stale, (old, old))
    MCPApi._sweep_temp_dir()
    assert not stale.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Auth middleware
# ─────────────────────────────────────────────────────────────────────────────

def test_no_auth_by_default():
    app = MCPApi(title="t-open", mcpark_path=None)
    with TestClient(app.api) as client:
        assert client.get("/openapi.json").status_code == 200


def test_auth_rejects_without_credentials():
    app = MCPApi(title="t-auth", mcpark_path=None, auth_token="s3cret")
    with TestClient(app.api) as client:
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/openapi.json",
                          headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/openapi.json?key=wrong").status_code == 401


def test_auth_accepts_bearer_query_and_cookie():
    app = MCPApi(title="t-auth2", mcpark_path=None, auth_token="s3cret")
    with TestClient(app.api) as client:
        r = client.get("/openapi.json", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200

        # query param works and plants the cookie…
        r = client.get("/openapi.json?key=s3cret")
        assert r.status_code == 200
        assert r.cookies.get("simcpi_key") == "s3cret"

        # …so the next bare request passes on the cookie alone
        assert client.get("/openapi.json").status_code == 200


def test_auth_protects_files_route():
    app = MCPApi(title="t-auth3", mcpark_path=None, auth_token="s3cret")
    url = app.serve_file("a.txt", b"a")
    token = url.split("/files/")[1].split("/")[0]
    with TestClient(app.api) as client:
        assert client.get(f"/files/{token}/a.txt").status_code == 401
        r = client.get(f"/files/{token}/a.txt",
                       headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Client-config entries (connect_claude / connect_cursor / get_claude_config)
# ─────────────────────────────────────────────────────────────────────────────

def test_config_entry_without_auth():
    e = _mcp_config_entry("http://localhost:8000/mcp/mcp", native=False, auth_token=None)
    assert e == {"command": "npx", "args": ["mcp-remote", "http://localhost:8000/mcp/mcp"]}
    e = _mcp_config_entry("http://localhost:8000/mcp/mcp", native=True, auth_token=None)
    assert e == {"type": "streamableHttp", "url": "http://localhost:8000/mcp/mcp"}


def test_config_entry_with_auth():
    e = _mcp_config_entry("http://x/mcp/mcp", native=False, auth_token="tok")
    assert e["args"] == ["mcp-remote", "http://x/mcp/mcp", "--header", "Authorization: Bearer tok"]
    e = _mcp_config_entry("http://x/mcp/mcp", native=True, auth_token="tok")
    assert e["headers"] == {"Authorization": "Bearer tok"}


def test_get_claude_config_carries_auth():
    app = MCPApi(title="t-cfg", mcpark_path=None, auth_token="tok")
    cfg = app.get_claude_config(native=True)
    entry = cfg["mcpServers"]["t-cfg"]
    assert entry["headers"] == {"Authorization": "Bearer tok"}


@pytest.mark.skipif(os.name != "nt", reason="connect_claude reads %APPDATA% only on Windows")
def test_connect_claude_preserves_other_servers_and_prefs(tmp_path, monkeypatch):
    from simcpi import connect_claude
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "mcpServers": {"other": {"command": "x"}},
        "preferences": {"theme": "dark"},
    }), encoding="utf-8")

    connect_claude("simcpi", port=8000)          # launch defaults to False

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "other" in data["mcpServers"]             # someone else's server survives
    assert "simcpi:8000" in data["mcpServers"]       # ours was added
    assert data["preferences"] == {"theme": "dark"}  # non-mcp keys untouched


@pytest.mark.skipif(os.name != "nt", reason="connect_claude reads %APPDATA% only on Windows")
def test_connect_claude_refuses_to_clobber_invalid_json(tmp_path, monkeypatch):
    from simcpi import connect_claude
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    bad = '{ "mcpServers": { invalid,, } '   # not valid JSON
    cfg.write_text(bad, encoding="utf-8")

    connect_claude("simcpi", port=8000)

    assert cfg.read_text(encoding="utf-8") == bad    # left exactly as-is, NOT wiped


# ─────────────────────────────────────────────────────────────────────────────
# Docstring optimizer
# ─────────────────────────────────────────────────────────────────────────────

import asyncio

from simcpi.optimize import build_eval_set, score_description, optimize_tool

RATED = [
    {"prompt": "greet Mohan in Hindi",   "tool_calls": [{"name": "greet_hindi"}],  "rating": 1},
    {"prompt": "say namaste to Ravi",    "tool_calls": [{"name": "greet_hindi"}],  "rating": 1},
    {"prompt": "greet Mohan in Telugu",  "tool_calls": [{"name": "greet_hindi"}],  "rating": 0},
    {"prompt": "telugu hello for Sita",  "tool_calls": [{"name": "greet_telugu"}], "rating": 1},
]

TOOLS = [
    {"name": "greet_hindi",  "description": "Greet in Hindi.",  "inputSchema": {}},
    {"name": "greet_telugu", "description": "Greet in Telugu.", "inputSchema": {}},
]


def test_build_eval_set_classification():
    items = build_eval_set(RATED, "greet_hindi")
    by_prompt = {i["prompt"]: i["expect"] for i in items}
    assert by_prompt["greet Mohan in Hindi"] == "select"     # own positive
    assert by_prompt["greet Mohan in Telugu"] == "avoid"     # own negative (rated 0)
    assert by_prompt["telugu hello for Sita"] == "avoid"     # foreign positive — no stealing
    assert len(items) == 4


def test_score_description_with_stub_selector():
    items = build_eval_set(RATED, "greet_hindi")

    async def perfect(prompt, tools):
        return "greet_hindi" if "hindi" in prompt.lower() or "namaste" in prompt.lower() \
            else "greet_telugu"

    async def greedy(prompt, tools):
        return "greet_hindi"  # steals everything

    score, misses = asyncio.run(
        score_description("greet_hindi", "x", TOOLS, items, perfect))
    assert score == 1.0 and misses == []

    score, misses = asyncio.run(
        score_description("greet_hindi", "x", TOOLS, items, greedy))
    assert score == 0.5  # both "avoid" items wrongly selected
    assert len(misses) == 2


def test_optimize_tool_keeps_track_of_improvement():
    async def bad_then_good(prompt, tools):
        # selector behaves "correctly" only when the candidate text is live
        desc = next(t["description"] for t in tools if t["name"] == "greet_hindi")
        if desc == "IMPROVED":
            return "greet_hindi" if "hindi" in prompt.lower() or "namaste" in prompt.lower() \
                else "greet_telugu"
        return "greet_hindi"

    async def generate(system, user):
        return "IMPROVED"

    r = asyncio.run(optimize_tool(
        TOOLS[0], TOOLS, RATED, bad_then_good, generate))
    assert r["candidate"] == "IMPROVED"
    assert r["improved"] and r["candidate_score"] > r["current_score"]


def test_optimize_tool_skips_without_data():
    r = asyncio.run(optimize_tool(
        {"name": "lonely", "description": "", "inputSchema": {}},
        TOOLS, RATED, None, None))
    assert "skipped" in r


def test_apply_docstring_route_updates_live_tool():
    app = MCPApi(title="t-apply")

    @app.create_tool_api("/hello")
    def hello(name: str) -> str:
        """Old description."""
        return f"hi {name}"

    with TestClient(app.api) as client:
        r = client.post("/mcpark/apply-docstring",
                        json={"tool": "hello", "description": "New description."})
        assert r.status_code == 200 and r.json()["ok"]

        tools = client.post("/mcpark/tools").json()["tools"]
        assert tools[0]["description"] == "New description."

        r = client.post("/mcpark/apply-docstring",
                        json={"tool": "nope", "description": "x"})
        assert r.status_code == 400


def test_rewrite_docstring_replaces_existing():
    src = (
        "import x\n"
        "@deco\n"
        "def greet(name):\n"
        '    """Old description."""\n'
        "    return name\n"
    )
    out = _rewrite_docstring_in_source(src, "greet", "New and better.", near_line=2)
    import ast as _ast
    tree = _ast.parse(out)
    fn = next(n for n in tree.body if getattr(n, "name", None) == "greet")
    assert _ast.get_docstring(fn) == "New and better."
    assert "Old description" not in out
    assert "return name" in out  # body untouched


def test_rewrite_docstring_inserts_when_missing():
    src = "def f(a):\n    return a\n"
    out = _rewrite_docstring_in_source(src, "f", "Now documented.")
    import ast as _ast
    fn = _ast.parse(out).body[0]
    assert _ast.get_docstring(fn) == "Now documented."
    assert "return a" in out


def test_rewrite_docstring_rejects_triple_quote():
    with pytest.raises(ValueError):
        _rewrite_docstring_in_source('def f():\n    """x"""\n    pass\n',
                                     "f", 'has """ inside')


def test_rewrite_docstring_unknown_function():
    with pytest.raises(ValueError):
        _rewrite_docstring_in_source("def f():\n    pass\n", "nope", "x")


def test_write_docstring_to_source_end_to_end(tmp_path):
    mod = tmp_path / "mymod.py"
    mod.write_text(
        "def hello(name):\n"
        '    """Old."""\n'
        "    return name\n",
        encoding="utf-8",
    )
    import importlib.util
    spec = importlib.util.spec_from_file_location("mymod", mod)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    app = MCPApi(title="t-write")
    app._registry["hello"] = m.hello   # register the real on-disk function

    result = app._write_docstring_to_source("hello", "Brand new docs.")
    assert result["ok"] and result["file"] == str(mod)
    assert 'Brand new docs.' in mod.read_text(encoding="utf-8")

    bad = app._write_docstring_to_source("ghost", "x")
    assert not bad["ok"]


def test_write_docstring_endpoint_rejects_non_local():
    # TestClient reports host "testclient" → treated as local; verify the gate
    # logic directly instead.
    from types import SimpleNamespace
    assert MCPApi._is_local_request(
        SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))
    assert not MCPApi._is_local_request(
        SimpleNamespace(client=SimpleNamespace(host="203.0.113.5")))
    assert not MCPApi._is_local_request(SimpleNamespace(client=None))


def test_optimize_route_requires_feedback():
    app = MCPApi(title="t-noopt")  # feedback OFF
    with TestClient(app.api) as client:
        r = client.post("/mcpark/optimize", json={})
        assert r.status_code == 400
        assert "feedback" in r.json()["error"]


# ─────────────────────────────────────────────────────────────────────────────
# Feedback memory basics
# ─────────────────────────────────────────────────────────────────────────────

def test_skill_download_route():
    app = MCPApi(title="t-skill")
    with TestClient(app.api) as client:
        r = client.get("/mcpark/skill")
        assert r.status_code == 200
        assert "simcpi" in r.text.lower()
        assert "attachment" in r.headers.get("content-disposition", "").lower()
        assert "SKILL.md" in r.headers.get("content-disposition", "")


def test_skill_md_shipped_in_package():
    from simcpi.mcpserver import _skill_md_path
    p = _skill_md_path()
    assert p.exists(), f"SKILL.md must be bundled at {p}"
    assert p.parent.name == "simcpi"  # inside the package, not repo root


def test_feedback_clear(tmp_path):
    fm = FeedbackMemory(str(tmp_path / "fb.db"))
    fm.log_call("a", [{"name": "t"}]); fm.log_call("b", [{"name": "t"}])
    assert fm.stats()["total"] == 2
    removed = fm.clear()
    assert removed == 2
    assert fm.stats()["total"] == 0
    assert fm.recent_calls() == []


def test_feedback_clear_route_localhost_only(tmp_path):
    app = MCPApi(title="t-clear", feedback=True, feedback_db=str(tmp_path / "fb.db"))
    app._feedback.log_call("x", [{"name": "t"}])
    with TestClient(app.api) as client:
        # TestClient host counts as local → clear works
        r = client.post("/mcpark/feedback-clear")
        assert r.status_code == 200 and r.json()["removed"] == 1
        assert app._feedback.stats()["total"] == 0

    plain = MCPApi(title="t-noclear")  # feedback OFF
    with TestClient(plain.api) as client:
        assert client.post("/mcpark/feedback-clear").status_code == 400


def test_feedback_threshold_knob_passed(tmp_path, monkeypatch):
    app = MCPApi(title="t-knob", feedback=True, feedback_db=str(tmp_path / "fb.db"),
                 feedback_k=3, feedback_threshold=0.42)
    assert app.feedback_k == 3
    assert app.feedback_threshold == 0.42

    captured = {}
    def fake_retrieve(embedding, k=5, threshold=0.6, only_positive=False):
        captured.update(k=k, threshold=threshold)
        return []
    monkeypatch.setattr(app._feedback, "retrieve", fake_retrieve)
    app._feedback.retrieve([0.1], k=app.feedback_k, threshold=app.feedback_threshold)
    assert captured == {"k": 3, "threshold": 0.42}


def test_rating_note_stored_and_surfaced(tmp_path):
    fm = FeedbackMemory(str(tmp_path / "fb.db"))
    cid = fm.log_call("greet in telugu", [{"name": "greet_hindi"}])
    assert fm.set_rating(cid, 0, note="wanted Telugu, got Hindi")

    rated = fm.rated_calls()
    assert rated[0]["note"] == "wanted Telugu, got Hindi"
    assert fm.recent_calls()[0]["note"] == "wanted Telugu, got Hindi"

    # blank note normalises to None
    cid2 = fm.log_call("x", [{"name": "t"}])
    fm.set_rating(cid2, 1, note="   ")
    assert fm.rated_calls()[0]["note"] is None


def test_rate_route_accepts_note(tmp_path):
    app = MCPApi(title="t-note", feedback=True, feedback_db=str(tmp_path / "fb.db"))
    cid = app._feedback.log_call("p", [{"name": "t"}])
    with TestClient(app.api) as client:
        r = client.post("/mcpark/rate",
                        json={"call_id": cid, "rating": 0, "note": "too vague"})
        assert r.status_code == 200
    assert app._feedback.rated_calls()[0]["note"] == "too vague"


def test_note_migration_on_old_db(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    # simulate a pre-note DB
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE calls(id TEXT PRIMARY KEY, ts REAL, session_id TEXT, "
                "prompt TEXT, embedding BLOB, tool_calls TEXT, rating INTEGER)")
    con.execute("INSERT INTO calls VALUES ('a', 1.0, 's', 'hi', NULL, '[]', 1)")
    con.commit(); con.close()

    fm = FeedbackMemory(str(db))   # __init__ should migrate
    assert fm.set_rating("a", 0, note="added after migration")
    assert fm.recent_calls()[0]["note"] == "added after migration"


def test_optimizer_proposal_includes_user_notes():
    import asyncio
    from simcpi.optimize import propose_description

    rated = [
        {"prompt": "greet in telugu", "tool_calls": [{"name": "greet_hindi"}],
         "rating": 0, "note": "I wanted Telugu not Hindi"},
        {"prompt": "say hi in hindi", "tool_calls": [{"name": "greet_hindi"}],
         "rating": 1, "note": None},
    ]
    from simcpi.optimize import build_eval_set
    items = build_eval_set(rated, "greet_hindi")

    captured = {}
    async def gen(system, user):
        captured["user"] = user
        return "Greets in Hindi only."

    tool = {"name": "greet_hindi", "description": "Greet.", "inputSchema": {}}
    asyncio.run(propose_description(tool, [tool], items, gen))
    assert "I wanted Telugu not Hindi" in captured["user"]


def test_feedback_rows_route(tmp_path):
    app = MCPApi(title="t-rows", feedback=True,
                 feedback_db=str(tmp_path / "fb.db"))
    cid = app._feedback.log_call("add 1 and 2", [{"name": "add"}], session_id="s1")
    app._feedback.set_rating(cid, 1)

    with TestClient(app.api) as client:
        r = client.get("/mcpark/feedback-rows")
        assert r.status_code == 200
        data = r.json()
        assert data["stats"]["total"] == 1
        assert data["rows"][0]["prompt"] == "add 1 and 2"
        assert data["rows"][0]["rating"] == 1
        assert data["rows"][0]["tool_calls"] == [{"name": "add"}]

    plain = MCPApi(title="t-norows")  # feedback OFF
    with TestClient(plain.api) as client:
        assert client.get("/mcpark/feedback-rows").status_code == 400


def test_split_result_text_and_images():
    from simcpi.mcpserver import _split_result

    class B:
        def __init__(self, **kw): self.__dict__.update(kw)
    class R:
        def __init__(self, content): self.content = content

    txt, imgs = _split_result(R([B(type="text", text="hello")]))
    assert txt == "hello" and imgs == []

    txt, imgs = _split_result(R([B(type="image", mimeType="image/png", data="QUJD")]))
    assert "1 image(s)" in txt
    assert imgs == [{"mimeType": "image/png", "data": "QUJD"}]

    txt, imgs = _split_result(R([B(type="text", text="chart below"),
                                 B(type="image", mimeType="image/png", data="QUJD")]))
    assert txt == "chart below" and len(imgs) == 1


def test_cli_help_exits_zero():
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "simcpi", "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "MCP server URL" in r.stdout


def test_feedback_log_rate_retrieve(tmp_path):
    db = str(tmp_path / "fb.db")
    fm = FeedbackMemory(db)
    cid = fm.log_call("add two numbers", [{"name": "add", "arguments": {"a": 1, "b": 2}}],
                      embedding=[1.0, 0.0], session_id="s1")
    assert fm.set_rating(cid, 1)
    got = fm.retrieve([1.0, 0.0])
    assert len(got) == 1 and got[0]["rating"] == 1
    stats = fm.stats()
    assert stats["total"] == 1 and stats["rated"] == 1

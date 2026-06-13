"""
optimize.py — the "Improve docstrings" engine
=============================================
v2 of the eval/optimize arc. Uses the rated dataset collected by the MCPark
feedback memory (v1) as ground truth to measure and improve tool descriptions.

Per tool:
  1. build_eval_set    — turn rated calls into selection expectations
  2. score_description — replay each rated prompt through a selection-only
                         LLM call ("which tool would you pick?") and compare
                         to the expectation. Tools are NEVER executed.
  3. propose_description — ask the LLM to rewrite the docstring given the
                           evidence (what worked, what flopped, what it stole)
  4. score the candidate the same way — keep it only if the score improves.

`select_fn` / `generate_fn` are injected so tests can stub the LLM.
"""

from __future__ import annotations

import json


# ─────────────────────────────────────────────────────────────────────────────
# Evidence — turn rated calls into selection expectations
# ─────────────────────────────────────────────────────────────────────────────

def build_eval_set(rated_calls: list[dict], tool_name: str, cap: int = 16) -> list[dict]:
    """
    Each item: {"prompt", "expect"} where expect is:
      "select" — a rating-1 call where this tool was used (it should win again)
      "avoid"  — a rating-0 call where this tool was used (it failed the user),
                 or a rating-1 call won by a DIFFERENT tool (ours must not
                 steal it — guards against over-broad rewrites).
    Own examples come first; foreign positives fill the remaining cap.
    """
    own, foreign = [], []
    for r in rated_calls:
        prompt = r.get("prompt")
        if not prompt:
            continue
        note = (r.get("note") or "").strip() or None
        called = {tc.get("name") for tc in (r.get("tool_calls") or [])}
        if tool_name in called:
            own.append({"prompt": prompt, "note": note,
                        "expect": "select" if r["rating"] == 1 else "avoid"})
        elif r["rating"] == 1 and called:
            foreign.append({"prompt": prompt, "note": note, "expect": "avoid"})
    return (own + foreign)[:cap]


# ─────────────────────────────────────────────────────────────────────────────
# Scoring — selection-only replay
# ─────────────────────────────────────────────────────────────────────────────

async def score_description(
    tool_name:   str,
    description: str,
    tools:       list[dict],
    eval_items:  list[dict],
    select_fn,
) -> tuple[float, list[str]]:
    """
    Score `description` for `tool_name` against the eval set.

    select_fn(prompt, tools) -> name of the tool the LLM would call, or None.
    Returns (score in 0..1, prompts that went wrong).
    """
    patched = [
        {**t, "description": description} if t["name"] == tool_name else t
        for t in tools
    ]
    correct, misses = 0, []
    for item in eval_items:
        selected = await select_fn(item["prompt"], patched)
        ok = (selected == tool_name) if item["expect"] == "select" else (selected != tool_name)
        if ok:
            correct += 1
        else:
            misses.append(item["prompt"])
    return (correct / len(eval_items) if eval_items else 0.0), misses


# ─────────────────────────────────────────────────────────────────────────────
# Proposal — rewrite the docstring from evidence
# ─────────────────────────────────────────────────────────────────────────────

async def propose_description(
    tool:        dict,
    tools:       list[dict],
    eval_items:  list[dict],
    generate_fn,
) -> str:
    """
    One LLM call: rewrite the tool description using the rated evidence.
    generate_fn(system, user) -> str.
    """
    def _fmt(items):
        # Show the prompt, plus the user's reason when they left one.
        out = []
        for i in items:
            out.append({"prompt": i["prompt"], "user_said": i["note"]} if i.get("note")
                       else {"prompt": i["prompt"]})
        return out

    should   = _fmt([i for i in eval_items if i["expect"] == "select"])
    shouldnt = _fmt([i for i in eval_items if i["expect"] == "avoid"])
    notes    = [i["note"] for i in eval_items if i.get("note")]
    siblings = [
        {"name": t["name"], "description": t.get("description") or ""}
        for t in tools if t["name"] != tool["name"]
    ]
    params = list((tool.get("inputSchema") or {}).get("properties", {}).keys())

    system = (
        "You write MCP tool descriptions. The description is the ONLY thing an "
        "LLM sees when deciding whether to call the tool — write it for the LLM. "
        "Return ONLY the new description text: plain text, 1-4 sentences, no "
        "markdown, no quotes, no preamble."
    )
    notes_block = (
        "\nUser-written reasons explaining what went wrong (treat these as the "
        f"strongest signal — fix exactly what they complain about):\n{json.dumps(notes, indent=2)}\n"
        if notes else ""
    )
    user = (
        f"Tool: {tool['name']}\n"
        f"Parameters: {params}\n"
        f"Current description:\n{tool.get('description') or '(empty)'}\n\n"
        f"Real user prompts that SHOULD trigger this tool (rated good):\n"
        f"{json.dumps(should, indent=2)}\n\n"
        f"Real user prompts that should NOT trigger it (rated bad, or belong to "
        f"sibling tools):\n{json.dumps(shouldnt, indent=2)}\n"
        f"{notes_block}\n"
        f"Sibling tools on the same server (do not overlap with them):\n"
        f"{json.dumps(siblings, indent=2)}\n\n"
        "Rewrite the description so the right prompts trigger it and the wrong "
        "ones don't. Include a 'Use this when...' sentence. Mention what "
        "distinguishes it from the most similar sibling."
    )
    text = await generate_fn(system, user)
    return text.strip().strip('"').strip()


# ─────────────────────────────────────────────────────────────────────────────
# Production LLM adapters (OpenAI / Anthropic)
# ─────────────────────────────────────────────────────────────────────────────

def make_selector(provider: str, api_key: str, model: str,
                  base_url: str | None = None, headers: dict | None = None):
    """Build select_fn(prompt, tools) -> tool name | None for the given provider."""
    if provider == "openai":
        from openai import AsyncOpenAI
        kw = dict(api_key=api_key)
        if base_url: kw["base_url"] = base_url
        if headers:  kw["default_headers"] = headers
        client = AsyncOpenAI(**kw)

        async def select(prompt: str, tools: list[dict]):
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name":        t["name"],
                        "description": t.get("description") or "",
                        "parameters":  t.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                } for t in tools],
            )
            tcs = resp.choices[0].message.tool_calls
            return tcs[0].function.name if tcs else None
    else:
        import anthropic as _sdk
        kw = dict(api_key=api_key)
        if base_url: kw["base_url"] = base_url
        if headers:  kw["default_headers"] = headers
        client = _sdk.AsyncAnthropic(**kw)

        async def select(prompt: str, tools: list[dict]):
            resp = await client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
                tools=[{
                    "name":         t["name"],
                    "description":  t.get("description") or "",
                    "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
                } for t in tools],
            )
            block = next((b for b in resp.content if b.type == "tool_use"), None)
            return block.name if block else None

    return select


def make_generator(provider: str, api_key: str, model: str,
                   base_url: str | None = None, headers: dict | None = None):
    """Build generate_fn(system, user) -> str for the given provider."""
    if provider == "openai":
        from openai import AsyncOpenAI
        kw = dict(api_key=api_key)
        if base_url: kw["base_url"] = base_url
        if headers:  kw["default_headers"] = headers
        client = AsyncOpenAI(**kw)

        async def generate(system: str, user: str) -> str:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user",   "content": user}],
            )
            return resp.choices[0].message.content or ""
    else:
        import anthropic as _sdk
        kw = dict(api_key=api_key)
        if base_url: kw["base_url"] = base_url
        if headers:  kw["default_headers"] = headers
        client = _sdk.AsyncAnthropic(**kw)

        async def generate(system: str, user: str) -> str:
            resp = await client.messages.create(
                model=model, max_tokens=1024, system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text

    return generate


# ─────────────────────────────────────────────────────────────────────────────
# The full loop for one tool
# ─────────────────────────────────────────────────────────────────────────────

async def optimize_tool(
    tool:         dict,
    tools:        list[dict],
    rated_calls:  list[dict],
    select_fn,
    generate_fn,
    min_examples: int = 3,
) -> dict:
    """Score current description, propose a rewrite, score it, report both."""
    items = build_eval_set(rated_calls, tool["name"])
    n_own = sum(1 for r in rated_calls
                if tool["name"] in {tc.get("name") for tc in (r.get("tool_calls") or [])})
    if n_own == 0 or len(items) < min_examples:
        return {
            "tool":    tool["name"],
            "skipped": f"needs rated runs that used this tool "
                       f"(has {n_own}, eval set {len(items)}/{min_examples})",
            "n_examples": len(items),
        }

    current_score, current_misses = await score_description(
        tool["name"], tool.get("description") or "", tools, items, select_fn)
    candidate = await propose_description(tool, tools, items, generate_fn)
    candidate_score, candidate_misses = await score_description(
        tool["name"], candidate, tools, items, select_fn)

    return {
        "tool":             tool["name"],
        "current":          tool.get("description") or "",
        "current_score":    current_score,
        "current_misses":   current_misses,
        "candidate":        candidate,
        "candidate_score":  candidate_score,
        "candidate_misses": candidate_misses,
        "improved":         candidate_score > current_score,
        "n_examples":       len(items),
    }

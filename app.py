"""Cursor-powered Test Suite & Report Generator.

Flask backend that:
1. Accepts an uploaded source file.
2. Launches a Cursor Cloud Agent (POST /v0/agents).
3. Polls the agent until FINISHED, then fetches the conversation.
4. Extracts a strict JSON analysis (quality score + test cases) from the
   last assistant message.
5. For Python files, materialises the AI-generated pytest snippets and
   executes them locally, capturing pass/fail metrics.
6. Stores the merged report in memory and exposes it via /report/<id>
   plus a Markdown download via /export/<id>.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
TEST_FOLDER = "generated_tests"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEST_FOLDER, exist_ok=True)

CURSOR_API_KEY = os.getenv("CURSOR_API_KEY", "").strip()
CURSOR_REPO_URL = os.getenv(
    "CURSOR_REPO_URL", "https://github.com/cursor/cursor"
).strip()
CURSOR_MODEL = os.getenv("CURSOR_MODEL", "default").strip() or "default"
CURSOR_TIMEOUT_SECONDS = int(os.getenv("CURSOR_TIMEOUT_SECONDS", "120"))
CURSOR_BASE_URL = "https://api.cursor.com"

REPORTS: dict[str, dict[str, Any]] = {}

LANGUAGE_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".c": "c",
    ".rs": "rust",
}


# ---------------------------------------------------------------------------
# Cursor Cloud Agents API client
# ---------------------------------------------------------------------------


@dataclass
class CursorError(Exception):
    """Raised when the Cursor API returns an unexpected response."""

    message: str
    status_code: int | None = None
    payload: Any = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"CursorError({self.status_code}): {self.message}"


def _cursor_auth() -> tuple[str, str]:
    if not CURSOR_API_KEY or CURSOR_API_KEY == "CURSOR_API_KEY":
        raise CursorError(
            "CURSOR_API_KEY is not configured. Add a real key to your .env file "
            "(generate one at https://cursor.com/dashboard/integrations)."
        )
    return (CURSOR_API_KEY, "")


def cursor_launch(prompt_text: str) -> dict[str, Any]:
    """Launch a new Cursor cloud agent and return the raw response JSON."""

    payload = {
        "prompt": {"text": prompt_text},
        "model": CURSOR_MODEL,
        "source": {"repository": CURSOR_REPO_URL},
    }
    resp = requests.post(
        f"{CURSOR_BASE_URL}/v0/agents",
        auth=_cursor_auth(),
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 300:
        raise CursorError(
            f"Failed to launch Cursor agent: {resp.text[:500]}",
            status_code=resp.status_code,
            payload=resp.text,
        )
    return resp.json()


def cursor_status(agent_id: str) -> dict[str, Any]:
    resp = requests.get(
        f"{CURSOR_BASE_URL}/v0/agents/{agent_id}",
        auth=_cursor_auth(),
        timeout=30,
    )
    if resp.status_code >= 300:
        raise CursorError(
            f"Failed to fetch agent status: {resp.text[:500]}",
            status_code=resp.status_code,
        )
    return resp.json()


def cursor_wait(agent_id: str, timeout: int | None = None) -> dict[str, Any]:
    """Poll until the agent reaches a terminal status."""

    deadline = time.time() + (timeout or CURSOR_TIMEOUT_SECONDS)
    terminal = {"FINISHED", "ERROR", "FAILED", "CANCELLED", "EXPIRED"}
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = cursor_status(agent_id)
        if str(last.get("status", "")).upper() in terminal:
            return last
        time.sleep(2)
    raise CursorError(
        f"Cursor agent {agent_id} did not finish within "
        f"{timeout or CURSOR_TIMEOUT_SECONDS}s "
        f"(last status: {last.get('status')})"
    )


def cursor_conversation(agent_id: str) -> list[dict[str, Any]]:
    resp = requests.get(
        f"{CURSOR_BASE_URL}/v0/agents/{agent_id}/conversation",
        auth=_cursor_auth(),
        timeout=30,
    )
    if resp.status_code >= 300:
        raise CursorError(
            f"Failed to fetch agent conversation: {resp.text[:500]}",
            status_code=resp.status_code,
        )
    body = resp.json()
    return body.get("messages", []) or []


# ---------------------------------------------------------------------------
# Prompt + JSON extraction
# ---------------------------------------------------------------------------


PROMPT_TEMPLATE = """You are an expert software QA engineer, code reviewer, security
auditor, and algorithms specialist.

You will be given a single source file. Do NOT modify the repository, do not
clone or browse it, and do not produce any commits. Only analyse the code that
appears between the <CODE> tags and respond with a single fenced ```json block
matching the schema below. No prose before or after the JSON.

Required schema (ALL fields must be present; use empty arrays/objects when not
applicable):
```json
{{
  "language": "<detected language>",
  "summary": "<3-5 sentence overview of what this code does and notable concerns>",
  "quality_score": <integer 0-100, overall quality>,
  "quality_breakdown": {{
    "readability":     <integer 0-100>,
    "maintainability": <integer 0-100>,
    "complexity":      <integer 0-100, higher = simpler/lower complexity>,
    "testability":     <integer 0-100>
  }},
  "test_cases": {{
    "functional": [
      {{
        "name": "test_<snake_case_name>",
        "description": "<what this test verifies>",
        "inputs": "<concrete sample inputs>",
        "expected": "<expected behaviour or output>",
        "pytest_code": "<self-contained pytest code if language is python, otherwise empty string>"
      }}
    ],
    "edge":     [ /* same shape, boundary/edge conditions */ ],
    "negative": [ /* same shape, invalid inputs / failure modes */ ]
  }},
  "api_documentation": {{
    "has_api": <true if the file defines OR consumes any HTTP API>,
    "framework": "<flask|fastapi|express|django|spring|none|...>",
    "base_url_hint": "<best guess base URL e.g. http://localhost:5000>",
    "endpoints": [
      {{
        "method": "GET|POST|PUT|PATCH|DELETE|...",
        "path": "/example/<id>",
        "name": "<short human title>",
        "description": "<what it does>",
        "auth": "none|bearer|basic|apikey|cookie|<other>",
        "headers": [{{ "key": "Content-Type", "value": "application/json", "description": "" }}],
        "path_params": [{{ "name": "id", "type": "string", "description": "" }}],
        "query_params": [{{ "name": "limit", "type": "integer", "description": "" }}],
        "body": {{
          "type": "json|form-data|x-www-form-urlencoded|multipart|text|none",
          "schema": "<JSON-shape or short description>",
          "example": "<sample payload>"
        }},
        "responses": [
          {{ "status": 200, "description": "Success", "example": "<sample json>" }},
          {{ "status": 400, "description": "Bad request", "example": "" }}
        ]
      }}
    ],
    "external_calls": [
      {{
        "method": "GET|POST|...",
        "url": "https://api.example.com/v1/...",
        "purpose": "<what this call does>",
        "auth": "<auth header / scheme used>",
        "headers": [{{ "key": "Authorization", "value": "Bearer <token>" }}],
        "body_example": "<sample payload or empty>",
        "where_in_code": "<function or line reference>"
      }}
    ]
  }},
  "suggestions": [
    {{
      "title": "<short title>",
      "category": "logic|readability|maintainability|performance|style",
      "severity": "low|medium|high",
      "where": "<function name and/or line range>",
      "rationale": "<why this matters>",
      "before": "<existing snippet from the code>",
      "after":  "<replacement snippet>",
      "impact": "<concrete effect, e.g. 'removes 12 lines, fewer branches'>"
    }}
  ],
  "security": [
    {{
      "title": "<short title, e.g. 'Plaintext password storage'>",
      "severity": "critical|high|medium|low|info",
      "category": "auth|injection|crypto|secrets|xss|csrf|access-control|deserialization|other",
      "cwe": "CWE-XXX or empty",
      "where": "<function name and/or line range>",
      "rationale": "<why this is a problem>",
      "before": "<vulnerable snippet>",
      "after":  "<hardened snippet>",
      "references": ["https://owasp.org/...", "..."]
    }}
  ],
  "dsa_improvements": [
    {{
      "title": "<short title, e.g. 'Replace nested loop with hashmap'>",
      "category": "time|space|algorithm|data-structure",
      "current_complexity": "O(n^2)",
      "improved_complexity": "O(n)",
      "where": "<function name and/or line range>",
      "rationale": "<why the current approach is suboptimal>",
      "before": "<current snippet>",
      "after":  "<optimised snippet>",
      "impact": "<concrete effect, e.g. '100x faster on n=10000, same memory'>"
    }}
  ]
}}
```

Detection rules:
- For `api_documentation.endpoints`, detect Flask routes (`@app.route`, `@bp.route`),
  FastAPI (`@app.get/post/...`), Django urls (`urlpatterns`), Express (`app.METHOD(...)`),
  Spring (`@GetMapping/@PostMapping`), generic decorators with `path=` / `route=`.
- For `api_documentation.external_calls`, detect Python `requests.*`, `httpx.*`,
  `urllib`, `http.client`, JS `fetch(`, `axios.*`, Java `HttpClient.send`, etc.
- If neither is present, set `has_api: false` and return empty arrays.
- Always include `before` / `after` snippets that are short (<= 30 lines) and
  syntactically valid in the source language.

Rules for `pytest_code` (only when language is python):
- Each entry must be a complete `def test_<name>(): ...` function.
- Assume the uploaded module is importable as `{module_name}` (it is on sys.path).
- Use only stdlib + pytest + unittest.mock. Do not require network access or external services.
- IMPORTANT: any third-party package the uploaded code imports (chromadb, openai,
  google.generativeai, requests, redis, sqlalchemy, boto3, etc.) will be replaced
  by a `unittest.mock.MagicMock` at import time. Your tests MUST therefore:
  * never assume real return shapes from those libraries (they return MagicMock),
  * use `unittest.mock.patch` / `monkeypatch` for any specific behaviour you need,
  * focus on pure-Python logic, conditional branches, helper functions, input
    validation, and Flask test_client interactions on routes that don't depend
    on real external state.
- Wrap calls that may legitimately raise in `pytest.raises(...)`.
- Keep each test independent; no shared state.

Provide:
- 2-4 cases per test category.
- 0-6 suggestions, 0-6 security findings, 0-6 dsa improvements (only real ones,
  do not pad). If nothing applies, return an empty array.

Filename: {filename}

<CODE language="{language}">
{code}
</CODE>
"""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from an assistant message."""

    if not text:
        raise ValueError("Empty assistant response")

    match = _JSON_FENCE_RE.search(text)
    candidate = match.group(1) if match else None

    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start : end + 1]

    if candidate is None:
        raise ValueError("No JSON object found in assistant response")

    return json.loads(candidate)


def last_assistant_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("type") == "assistant_message" and msg.get("text"):
            return str(msg["text"])
    return ""


# ---------------------------------------------------------------------------
# Pytest runner
# ---------------------------------------------------------------------------


_PYTEST_SUMMARY_RE = re.compile(
    r"(?:(\d+)\s+passed)?(?:[,\s]+(\d+)\s+failed)?(?:[,\s]+(\d+)\s+error)?"
    r"(?:[,\s]+(\d+)\s+skipped)?",
    re.IGNORECASE,
)
_PYTEST_LINE_RE = re.compile(
    r"^(?P<file>[^\s:]+)::(?P<name>[^\s]+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED)",
    re.MULTILINE,
)


def _normalise_pytest_code(snippet: str) -> str:
    """Strip stray markdown fences / leading whitespace from a snippet."""

    cleaned = snippet.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n", "", cleaned)
        cleaned = re.sub(r"\n```$", "", cleaned)
    return cleaned.strip()


def _detect_top_level_imports(source: str) -> list[str]:
    """Return the set of top-level package names imported by `source`.

    Used so we can auto-stub any third-party packages that aren't installed
    in the runner's venv. Without this, an uploaded file that imports e.g.
    `chromadb` would make every test ERROR at collection time.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return sorted(m for m in mods if m and not m.startswith("_"))


# Always-stubbed packages that are awkward to require for hackathon demos.
_NEVER_STUB = {"pytest", "unittest", "typing", "dataclasses", "abc", "enum"}


def build_pytest_file(
    module_name: str, test_cases: dict[str, Any], source_code: str
) -> str:
    """Combine all AI-generated pytest snippets into one importable file.

    The header auto-stubs any top-level imports detected in the uploaded
    source that aren't actually installed in the runner's venv. This lets
    us run AI-generated tests against arbitrary code without the user
    having to `pip install` every transitive dependency.
    """

    detected = [m for m in _detect_top_level_imports(source_code) if m != module_name]
    detected_literal = json.dumps(detected)

    parts: list[str] = [
        "import importlib",
        "import os",
        "import sys",
        "from unittest.mock import MagicMock as _AutoMock",
        "",
        "import pytest",
        "",
        f"sys.path.insert(0, os.path.abspath({UPLOAD_FOLDER!r}))",
        "",
        f"_DETECTED_IMPORTS = {detected_literal}",
        f"_NEVER_STUB = {sorted(_NEVER_STUB)!r}",
        "_AUTO_STUBBED = []",
        "",
        "for _mod in _DETECTED_IMPORTS:",
        "    if _mod in _NEVER_STUB or _mod in sys.modules:",
        "        continue",
        "    try:",
        "        importlib.import_module(_mod)",
        "    except Exception:",
        "        sys.modules[_mod] = _AutoMock()",
        "        _AUTO_STUBBED.append(_mod)",
        "",
        "try:",
        f"    import {module_name} as target_module  # noqa: F401",
        "    _IMPORT_OK = True",
        "    _IMPORT_ERROR = None",
        "except Exception as exc:  # pragma: no cover - defensive",
        "    target_module = None  # type: ignore",
        "    _IMPORT_OK = False",
        "    _IMPORT_ERROR = exc",
        "",
        "def test_module_imports():",
        f"    assert _IMPORT_OK, (",
        f"        f'Failed to import {module_name}: '",
        "        f'{_IMPORT_ERROR!r} (auto-stubbed: {_AUTO_STUBBED})'",
        "    )",
        "",
    ]

    seen: set[str] = set()
    for category in ("functional", "edge", "negative"):
        for case in test_cases.get(category, []) or []:
            snippet = _normalise_pytest_code(case.get("pytest_code") or "")
            if not snippet or "def test_" not in snippet:
                continue
            name_match = re.search(r"def\s+(test_[A-Za-z0-9_]+)\s*\(", snippet)
            if not name_match:
                continue
            base = name_match.group(1)
            unique = base
            counter = 1
            while unique in seen:
                counter += 1
                unique = f"{base}_{counter}"
            seen.add(unique)
            if unique != base:
                snippet = snippet.replace(f"def {base}(", f"def {unique}(", 1)
            parts.append(f"# --- {category}: {case.get('name', unique)} ---")
            parts.append(snippet)
            parts.append("")

    return "\n".join(parts) + "\n"


def run_pytest(test_file_path: str) -> dict[str, Any]:
    """Run pytest against a generated file and parse the results."""

    try:
        result = subprocess.run(
            [
                "python",
                "-m",
                "pytest",
                test_file_path,
                "-v",
                "--tb=short",
                "--no-header",
                "-rN",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {
            "ran": True,
            "timeout": True,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "total": 0,
            "tests": [],
            "stdout": "Test execution timed out after 60 seconds.",
            "exit_code": -1,
        }
    except FileNotFoundError as exc:
        return {
            "ran": False,
            "error": f"Could not invoke pytest: {exc}",
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "total": 0,
            "tests": [],
            "stdout": "",
            "exit_code": -1,
        }

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = stdout + ("\n" + stderr if stderr else "")

    tests = []
    for m in _PYTEST_LINE_RE.finditer(stdout):
        tests.append(
            {
                "name": m.group("name"),
                "status": m.group("status").upper(),
            }
        )

    summary_line = ""
    for line in reversed(stdout.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            summary_line = line
            break

    passed = failed = errors = skipped = 0
    if summary_line:
        for count, label in re.findall(r"(\d+)\s+(passed|failed|error|errors|skipped)", summary_line):
            n = int(count)
            label = label.lower()
            if label == "passed":
                passed = n
            elif label == "failed":
                failed = n
            elif label.startswith("error"):
                errors = n
            elif label == "skipped":
                skipped = n

    if not tests and (passed or failed or errors or skipped):
        pass

    total = passed + failed + errors + skipped or len(tests)

    return {
        "ran": True,
        "timeout": False,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "total": total,
        "tests": tests,
        "stdout": combined[-4000:],
        "exit_code": result.returncode,
        "summary_line": summary_line.strip(),
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def detect_language(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return LANGUAGE_BY_EXT.get(ext, "unknown")


def build_report(filename: str, code: str) -> dict[str, Any]:
    language = detect_language(filename)
    module_name = os.path.splitext(os.path.basename(filename))[0]
    safe_module = re.sub(r"[^A-Za-z0-9_]", "_", module_name) or "module"

    prompt = PROMPT_TEMPLATE.format(
        filename=filename,
        language=language,
        module_name=safe_module,
        code=code,
    )

    launch = cursor_launch(prompt)
    agent_id = launch.get("id")
    if not agent_id:
        raise CursorError(f"Cursor launch response missing id: {launch}")

    final_status = cursor_wait(agent_id)
    status_str = str(final_status.get("status", "")).upper()

    messages = cursor_conversation(agent_id)
    assistant_text = last_assistant_text(messages)

    parsed: dict[str, Any]
    parse_error: str | None = None
    try:
        parsed = extract_json(assistant_text)
    except (ValueError, json.JSONDecodeError) as exc:
        parsed = {}
        parse_error = str(exc)

    test_cases = parsed.get("test_cases") or {}
    test_run: dict[str, Any] | None = None

    if language == "python" and test_cases:
        test_file_path = os.path.join(TEST_FOLDER, f"test_{safe_module}.py")
        test_source = build_pytest_file(safe_module, test_cases, code)
        with open(test_file_path, "w", encoding="utf-8") as fh:
            fh.write(test_source)
        test_run = run_pytest(test_file_path)
        test_run["test_file"] = test_file_path

    report_id = uuid.uuid4().hex[:12]
    report = {
        "id": report_id,
        "filename": filename,
        "language": parsed.get("language") or language,
        "module_name": safe_module,
        "agent_id": agent_id,
        "agent_status": status_str,
        "agent_url": (final_status.get("target") or {}).get("url"),
        "summary": parsed.get("summary") or "",
        "quality_score": parsed.get("quality_score"),
        "quality_breakdown": parsed.get("quality_breakdown") or {},
        "test_cases": test_cases,
        "test_run": test_run,
        "api_documentation": parsed.get("api_documentation") or {
            "has_api": False,
            "framework": "none",
            "base_url_hint": "",
            "endpoints": [],
            "external_calls": [],
        },
        "suggestions": parsed.get("suggestions") or [],
        "security": parsed.get("security") or [],
        "dsa_improvements": parsed.get("dsa_improvements") or [],
        "parse_error": parse_error,
        "raw_assistant_text": assistant_text if parse_error else None,
        "code_preview": code[:4000],
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    REPORTS[report_id] = report
    return report


# ---------------------------------------------------------------------------
# Postman Collection (v2.1) export
# ---------------------------------------------------------------------------


def _postman_url(path: str, query_params: list[dict[str, Any]] | None) -> dict[str, Any]:
    raw_path = path or "/"
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    segments = [s for s in raw_path.strip("/").split("/") if s]
    url: dict[str, Any] = {
        "raw": "{{baseUrl}}" + raw_path,
        "host": ["{{baseUrl}}"],
        "path": segments,
    }
    if query_params:
        url["query"] = [
            {
                "key": str(q.get("name", "")),
                "value": "",
                "description": q.get("description", ""),
            }
            for q in query_params
            if q.get("name")
        ]
    return url


def _postman_body(body: dict[str, Any] | None) -> dict[str, Any] | None:
    if not body:
        return None
    btype = (body.get("type") or "").lower()
    example = body.get("example") or body.get("schema") or ""
    if btype in ("none", "", None):
        return None
    if btype == "json":
        return {"mode": "raw", "raw": str(example), "options": {"raw": {"language": "json"}}}
    if btype == "text":
        return {"mode": "raw", "raw": str(example)}
    if btype in ("form-data", "multipart"):
        return {"mode": "formdata", "formdata": []}
    if btype == "x-www-form-urlencoded":
        return {"mode": "urlencoded", "urlencoded": []}
    return {"mode": "raw", "raw": str(example)}


def _endpoint_to_postman_item(ep: dict[str, Any]) -> dict[str, Any]:
    method = (ep.get("method") or "GET").upper()
    path = ep.get("path") or "/"
    name = ep.get("name") or f"{method} {path}"
    headers = [
        {
            "key": h.get("key", ""),
            "value": h.get("value", ""),
            "description": h.get("description", ""),
        }
        for h in (ep.get("headers") or [])
        if h.get("key")
    ]
    request: dict[str, Any] = {
        "method": method,
        "header": headers,
        "url": _postman_url(path, ep.get("query_params") or []),
        "description": ep.get("description") or "",
    }
    body = _postman_body(ep.get("body"))
    if body is not None:
        request["body"] = body

    responses = []
    for r in ep.get("responses") or []:
        responses.append(
            {
                "name": f"{r.get('status', '')} - {r.get('description', '')}",
                "originalRequest": request,
                "code": int(r.get("status") or 0) if str(r.get("status", "")).isdigit() else 0,
                "status": str(r.get("description", "")),
                "_postman_previewlanguage": "json",
                "header": [],
                "body": str(r.get("example", "")),
            }
        )

    return {"name": name, "request": request, "response": responses}


def _external_call_to_postman_item(call: dict[str, Any]) -> dict[str, Any]:
    method = (call.get("method") or "GET").upper()
    url = call.get("url") or ""
    headers = [
        {"key": h.get("key", ""), "value": h.get("value", "")}
        for h in (call.get("headers") or [])
        if h.get("key")
    ]
    body = call.get("body_example")
    request: dict[str, Any] = {
        "method": method,
        "header": headers,
        "url": {"raw": url},
        "description": call.get("purpose") or call.get("where_in_code") or "",
    }
    if body:
        request["body"] = {"mode": "raw", "raw": str(body)}
    name = f"{method} {url}" if url else (call.get("purpose") or "External call")
    return {"name": name, "request": request, "response": []}


def to_postman_collection(report: dict[str, Any]) -> dict[str, Any]:
    """Convert the api_documentation block into a Postman v2.1 collection."""

    api = report.get("api_documentation") or {}
    endpoints = api.get("endpoints") or []
    external = api.get("external_calls") or []

    items: list[dict[str, Any]] = [_endpoint_to_postman_item(ep) for ep in endpoints]
    if external:
        items.append(
            {
                "name": "External APIs (consumed by code)",
                "item": [_external_call_to_postman_item(c) for c in external],
            }
        )

    base_url = api.get("base_url_hint") or "http://localhost:5000"

    return {
        "info": {
            "_postman_id": str(uuid.uuid4()),
            "name": f"Cursor Report — {report.get('filename', 'unknown')}",
            "description": (
                f"Auto-generated by the Cursor Test & Quality Report app.\n\n"
                f"Source file: {report.get('filename', '')}\n"
                f"Detected framework: {api.get('framework', 'none')}\n"
                f"Generated: {report.get('generated_at', '')}"
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
        "variable": [{"key": "baseUrl", "value": base_url, "type": "string"}],
    }


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------


def report_to_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Cursor Test & Quality Report — {report['filename']}")
    lines.append("")
    lines.append(f"- **Language:** `{report.get('language', 'unknown')}`")
    lines.append(f"- **Generated:** {report.get('generated_at', '')}")
    lines.append(f"- **Cursor Agent:** `{report.get('agent_id', '')}` "
                 f"(status: {report.get('agent_status', '')})")
    if report.get("agent_url"):
        lines.append(f"- **Agent URL:** {report['agent_url']}")
    lines.append("")

    if report.get("summary"):
        lines.append("## Summary")
        lines.append("")
        lines.append(report["summary"])
        lines.append("")

    score = report.get("quality_score")
    if score is not None:
        lines.append("## Quality Score")
        lines.append("")
        lines.append(f"**Overall:** {score} / 100")
        lines.append("")
        breakdown = report.get("quality_breakdown") or {}
        if breakdown:
            lines.append("| Metric | Score |")
            lines.append("| --- | ---: |")
            for k, v in breakdown.items():
                lines.append(f"| {k.title()} | {v} |")
            lines.append("")

    test_cases = report.get("test_cases") or {}
    if test_cases:
        lines.append("## Test Cases")
        lines.append("")
        for category in ("functional", "edge", "negative"):
            cases = test_cases.get(category) or []
            if not cases:
                continue
            lines.append(f"### {category.title()} ({len(cases)})")
            lines.append("")
            for i, case in enumerate(cases, 1):
                lines.append(f"**{i}. {case.get('name', 'unnamed')}**")
                if case.get("description"):
                    lines.append(f"- _{case['description']}_")
                if case.get("inputs"):
                    lines.append(f"- **Inputs:** `{case['inputs']}`")
                if case.get("expected"):
                    lines.append(f"- **Expected:** {case['expected']}")
                if case.get("pytest_code"):
                    lines.append("")
                    lines.append("```python")
                    lines.append(_normalise_pytest_code(case["pytest_code"]))
                    lines.append("```")
                lines.append("")

    test_run = report.get("test_run")
    if test_run:
        lines.append("## Test Run Results")
        lines.append("")
        lines.append(
            f"- Passed: **{test_run.get('passed', 0)}**, "
            f"Failed: **{test_run.get('failed', 0)}**, "
            f"Errors: **{test_run.get('errors', 0)}**, "
            f"Skipped: **{test_run.get('skipped', 0)}**, "
            f"Total: **{test_run.get('total', 0)}**"
        )
        if test_run.get("summary_line"):
            lines.append(f"- pytest summary: `{test_run['summary_line']}`")
        lines.append("")
        if test_run.get("tests"):
            lines.append("| Test | Status |")
            lines.append("| --- | --- |")
            for t in test_run["tests"]:
                lines.append(f"| `{t['name']}` | {t['status']} |")
            lines.append("")
        if test_run.get("stdout"):
            lines.append("<details><summary>pytest output</summary>")
            lines.append("")
            lines.append("```")
            lines.append(test_run["stdout"])
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    api = report.get("api_documentation") or {}
    endpoints = api.get("endpoints") or []
    external = api.get("external_calls") or []
    if api.get("has_api") or endpoints or external:
        lines.append("## API Details")
        lines.append("")
        lines.append(
            f"- **Framework:** {api.get('framework') or 'none'}  "
            f"  **Base URL hint:** `{api.get('base_url_hint') or '-'}`"
        )
        lines.append("")

        if endpoints:
            lines.append("### Endpoints")
            lines.append("")
            for ep in endpoints:
                method = (ep.get("method") or "GET").upper()
                lines.append(f"#### `{method}` `{ep.get('path', '/')}` — {ep.get('name', '')}")
                if ep.get("description"):
                    lines.append("")
                    lines.append(ep["description"])
                if ep.get("auth"):
                    lines.append(f"- **Auth:** {ep['auth']}")
                for label, key in (
                    ("Path params", "path_params"),
                    ("Query params", "query_params"),
                    ("Headers", "headers"),
                ):
                    items = ep.get(key) or []
                    if items:
                        lines.append(f"- **{label}:**")
                        for it in items:
                            name = it.get("name") or it.get("key", "")
                            t = it.get("type") or it.get("value", "")
                            d = it.get("description", "")
                            lines.append(f"  - `{name}` {('('+t+')') if t else ''} {('— '+d) if d else ''}")
                body = ep.get("body") or {}
                if body and (body.get("type") or "").lower() not in ("none", ""):
                    lines.append(f"- **Body ({body.get('type')}):**")
                    if body.get("schema"):
                        lines.append(f"  - schema: `{body['schema']}`")
                    if body.get("example"):
                        lines.append("")
                        lines.append("```")
                        lines.append(str(body["example"]))
                        lines.append("```")
                if ep.get("responses"):
                    lines.append("- **Responses:**")
                    for r in ep["responses"]:
                        lines.append(f"  - `{r.get('status', '')}` — {r.get('description', '')}")
                lines.append("")

        if external:
            lines.append("### External API Calls")
            lines.append("")
            for c in external:
                lines.append(
                    f"- `{(c.get('method') or 'GET').upper()}` "
                    f"`{c.get('url', '')}` — {c.get('purpose', '')}"
                )
                if c.get("where_in_code"):
                    lines.append(f"  - location: {c['where_in_code']}")
                if c.get("auth"):
                    lines.append(f"  - auth: {c['auth']}")
            lines.append("")

    def _emit_findings(title: str, items: list[dict[str, Any]], extras: list[tuple[str, str]] = ()) -> None:
        if not items:
            return
        lines.append(f"## {title}")
        lines.append("")
        for i, it in enumerate(items, 1):
            sev = it.get("severity") or it.get("category") or ""
            lines.append(f"### {i}. {it.get('title', '(no title)')}"
                         + (f" — _{sev}_" if sev else ""))
            for key, label in (("category", "Category"), ("where", "Location"),
                               ("cwe", "CWE"), ("current_complexity", "Current"),
                               ("improved_complexity", "Improved")) + tuple(extras):
                val = it.get(key)
                if val:
                    lines.append(f"- **{label}:** {val}")
            if it.get("rationale"):
                lines.append("")
                lines.append(it["rationale"])
            if it.get("before"):
                lines.append("")
                lines.append("**Before:**")
                lines.append("")
                lines.append("```")
                lines.append(str(it["before"]).rstrip())
                lines.append("```")
            if it.get("after"):
                lines.append("")
                lines.append("**After:**")
                lines.append("")
                lines.append("```")
                lines.append(str(it["after"]).rstrip())
                lines.append("```")
            if it.get("impact"):
                lines.append("")
                lines.append(f"_Impact: {it['impact']}_")
            refs = it.get("references") or []
            if refs:
                lines.append("")
                lines.append("References:")
                for r in refs:
                    lines.append(f"- {r}")
            lines.append("")

    _emit_findings("Suggestions", report.get("suggestions") or [])
    _emit_findings("Security", report.get("security") or [])
    _emit_findings("DSA Improvements", report.get("dsa_improvements") or [])

    if report.get("parse_error"):
        lines.append("## Notes")
        lines.append("")
        lines.append(
            f"- Could not parse strict JSON from the agent's reply: {report['parse_error']}"
        )
        if report.get("raw_assistant_text"):
            lines.append("")
            lines.append("<details><summary>Raw assistant response</summary>")
            lines.append("")
            lines.append("```")
            lines.append(report["raw_assistant_text"][:4000])
            lines.append("```")
            lines.append("")
            lines.append("</details>")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def home():
    return render_template(
        "index.html",
        cursor_repo=CURSOR_REPO_URL,
        cursor_model=CURSOR_MODEL,
    )


@app.route("/upload", methods=["POST"])
def upload_file():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file selected"}), 400

    safe_name = os.path.basename(file.filename)
    file_path = os.path.join(UPLOAD_FOLDER, safe_name)
    file.save(file_path)

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            code = fh.read()
    except OSError as exc:
        return jsonify({"error": f"Could not read uploaded file: {exc}"}), 400

    if not code.strip():
        return jsonify({"error": "Uploaded file is empty"}), 400

    try:
        report = build_report(safe_name, code)
    except CursorError as exc:
        return (
            jsonify(
                {
                    "error": "Cursor API call failed",
                    "details": str(exc),
                    "status_code": exc.status_code,
                }
            ),
            502,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"error": "Unexpected server error", "details": str(exc)}), 500

    return jsonify(report)


@app.route("/report/<report_id>")
def get_report(report_id: str):
    report = REPORTS.get(report_id)
    if not report:
        return jsonify({"error": "Report not found"}), 404
    return jsonify(report)


@app.route("/export/<report_id>")
def export_report(report_id: str):
    report = REPORTS.get(report_id)
    if not report:
        return jsonify({"error": "Report not found"}), 404

    md = report_to_markdown(report)
    filename = f"cursor-report-{report['filename'].rsplit('.', 1)[0]}-{report_id}.md"
    return Response(
        md,
        mimetype="text/markdown",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/postman/<report_id>")
def export_postman(report_id: str):
    report = REPORTS.get(report_id)
    if not report:
        return jsonify({"error": "Report not found"}), 404
    collection = to_postman_collection(report)
    base = report["filename"].rsplit(".", 1)[0]
    filename = f"cursor-postman-{base}-{report_id}.json"
    return Response(
        json.dumps(collection, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "cursor_key_configured": bool(CURSOR_API_KEY)
            and CURSOR_API_KEY != "CURSOR_API_KEY",
            "cursor_repo": CURSOR_REPO_URL,
            "cursor_model": CURSOR_MODEL,
        }
    )


if __name__ == "__main__":
    # use_reloader=False prevents Flask from restarting the worker when we
    # write generated_tests/*.py mid-request, which would otherwise drop the
    # client connection (ERR_CONNECTION_RESET). threaded=True lets the long
    # poll-the-Cursor-agent request not block other requests.
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False, threaded=True)

"""Only the approver page's decision route may call the approval queue's resolve().

The database refuses an approval from any connection but the approver role's, but the api
process holds that connection, so nothing in the process stops a route from calling
`resolve()`. This test is that stop: it reads every module under src/opskit as a syntax tree
(not text) and fails if anything else calls it, aliases it, or reaches it by name.

`resolve` is also pathlib's method, so a call counts as the queue's unless it is the
no-argument form `Path.resolve()` (optionally `strict=`). This catches the direct ways round
the rule, not every way: a name built at run time (`getattr(q, "re" + "solve")`) or reached
through `__dict__` is not detected, so a reviewer still reads new callers.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "opskit"
DEFINED_IN = "core/pg/approvals.py"
ALLOWED = ("api/routers/approver.py", "decide")  # (module, function) with the one call
BY_NAME_CALLS = {"getattr", "attrgetter", "methodcaller", "hasattr", "setattr"}


def _is_path_resolve(call: ast.Call) -> bool:
    return not call.args and all(k.arg == "strict" for k in call.keywords)


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []  # (enclosing function, line)
        self.other: list[tuple[str, int, str]] = []  # (what, line, why)
        self._functions: list[str] = []
        self._classes: list[str] = []
        self._called: set[int] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._classes.append(node.name)
        self.generic_visit(node)
        self._classes.pop()

    def visit_Constant(self, node: ast.Constant) -> None:
        if node.value == "resolve":
            self.other.append(("resolve", node.lineno, "names resolve in a string"))

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name == "resolve" and self._classes[-1:] != ["PgApprovalQueue"]:
            self.other.append((node.name, node.lineno, "defines a method named resolve"))
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "resolve":
            self._called.add(id(func))
            if not _is_path_resolve(node):
                # Only a top-level function counts as the route; a nested one is named after it.
                if not self._functions:
                    where = "<module>"
                elif len(self._functions) == 1:
                    where = self._functions[0]
                else:
                    where = f"{self._functions[0]}.<nested>"
                self.calls.append((where, node.lineno))
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in BY_NAME_CALLS and any(
            isinstance(a, ast.Constant) and a.value == "resolve" for a in node.args
        ):
            self.other.append((name, node.lineno, "reaches resolve by name"))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "resolve" and id(node) not in self._called:
            self.other.append(("resolve", node.lineno, "uses resolve without calling it"))
        self.generic_visit(node)


def violations(modules: dict[str, str]) -> list[str]:
    """Why each module breaks the rule; `modules` maps a path under src/opskit to its source."""
    found: list[str] = []
    allowed_calls = 0
    for path, source in sorted(modules.items()):
        scan = _Scan()
        scan.visit(ast.parse(source))
        for where, line in scan.calls:
            if (path, where) == ALLOWED:
                allowed_calls += 1
            else:
                found.append(f"{path}:{line} calls resolve() in {where}()")
        for what, line, why in scan.other:
            found.append(f"{path}:{line} {why} ({what})")
    if allowed_calls != 1 and ALLOWED[0] in modules:
        found.append(f"{ALLOWED[0]}: {ALLOWED[1]}() must call resolve() exactly once")
    return found


def _sources() -> dict[str, str]:
    return {
        str(path.relative_to(SRC)): path.read_text(encoding="utf-8") for path in SRC.rglob("*.py")
    }


def test_only_the_approver_decision_route_calls_resolve() -> None:
    sources = _sources()
    assert ALLOWED[0] in sources and DEFINED_IN in sources, "the scan did not find its files"
    assert violations(sources) == []


def test_the_decision_route_is_the_approver_pages_post_decision() -> None:
    tree = ast.parse(_sources()[ALLOWED[0]])
    route = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == ALLOWED[1]
    )
    decorators = [ast.unparse(d) for d in route.decorator_list]
    assert decorators == ["router.post('/approvals/{approval_id}/decision')"]
    assert "prefix='/approver'" in ast.unparse(tree)


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("async def other(q):\n    await q.resolve(i, decision=d, principal=p)", "calls resolve()"),
        ("def f(q):\n    return q.resolve(i)", "calls resolve()"),
        ("def f(q):\n    return q.resolve(**kw)", "calls resolve()"),
        ("def f(q):\n    go = q.resolve\n    return go", "uses resolve without calling it"),
        ("def f(q):\n    return getattr(q, 'resolve')", "reaches resolve by name"),
        ("def f(q):\n    return operator.methodcaller('resolve')", "reaches resolve by name"),
        ("class Q:\n    async def resolve(self): ...", "defines a method named resolve"),
        ("x = q.resolve(decision=d)", "calls resolve() in <module>"),
        ("def f(q):\n    return q.__dict__['resolve']", "names resolve in a string"),
        ("class Other:\n    async def resolve(self): ...", "defines a method named resolve"),
    ],
)
def test_the_scan_catches_the_ways_round_it(snippet: str, expected: str) -> None:
    result = violations({"api/routers/other.py": snippet})
    assert any(expected in line for line in result), result


@pytest.mark.parametrize(
    "snippet",
    ["p = Path(__file__).resolve()", "p = path.resolve(strict=True)", "def resolved(): ..."],
)
def test_pathlib_resolve_is_not_the_queues(snippet: str) -> None:
    assert violations({"api/routers/other.py": snippet}) == []


def test_a_second_call_even_inside_the_decision_module_is_caught() -> None:
    source = (
        "async def decide(a):\n    await a.resolve(i, decision=d, principal=p)\n"
        "async def sneaky(a):\n    await a.resolve(i, decision=d, principal=p)\n"
    )
    result = violations({ALLOWED[0]: source})
    assert any("sneaky" in line for line in result), result


def test_a_nested_function_named_decide_is_not_the_route() -> None:
    source = (
        "async def decide(a):\n    await a.resolve(i, decision=d, principal=p)\n"
        "async def other(a):\n    async def decide(b):\n"
        "        await b.resolve(i, decision=d, principal=p)\n"
    )
    result = violations({ALLOWED[0]: source})
    assert any("other.<nested>" in line for line in result), result


def test_the_queue_may_define_resolve_only_in_the_queue_class() -> None:
    ok = "class PgApprovalQueue:\n    async def resolve(self): ...\n"
    assert violations({DEFINED_IN: ok}) == []
    other = "class Helper:\n    async def resolve(self): ...\n"
    assert any("defines a method" in line for line in violations({DEFINED_IN: other}))

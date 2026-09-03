"""Static checks on the API module that catch a class of bug I actually shipped.

Adding a `detail` query parameter to the trail endpoint was done with a global
string replace, which also rewrote the *photos* endpoint — a function with no
`detail` in scope. The result was `NameError: name 'detail' is not defined` on every
photo request, a 500 that no unit test touched because the endpoints need the real
index to exercise.

This walks the AST instead: for each route handler, every bare name it reads must be
defined somewhere — as a parameter, a local, an import, or a module global. That is
cheap, needs no index, and would have failed the moment the replace went wrong.
"""

from __future__ import annotations

import ast
import builtins
import pathlib

API_PATH = pathlib.Path(__file__).resolve().parent.parent / "discovery_api.py"
TREE = ast.parse(API_PATH.read_text())

MODULE_NAMES = set(dir(builtins))
for node in TREE.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            MODULE_NAMES.add((alias.asname or alias.name).split(".")[0])
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                MODULE_NAMES.add(target.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        MODULE_NAMES.add(node.target.id)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        MODULE_NAMES.add(node.name)


def _handlers():
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _params(fn) -> set[str]:
    args = fn.args
    names = {a.arg for group in (args.posonlyargs, args.args, args.kwonlyargs)
             for a in group}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _bound_names(fn: ast.AST) -> set[str]:
    """Parameters, assignments, imports, comprehension and with/except targets.

    Nested helpers count too: a route that defines `def _point(raw, label)` reads
    `raw` inside it, and treating that as undefined is a false positive.
    """
    names = _params(fn)

    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
            names |= _params(node)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Lambda):
            names |= _params(node)
    return names


def test_every_handler_only_reads_names_that_exist():
    problems = []
    for fn in _handlers():
        bound = _bound_names(fn) | MODULE_NAMES
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in bound:
                    problems.append(f"{fn.name}: reads undefined name {node.id!r} "
                                    f"(line {node.lineno})")
    assert not problems, "\n".join(problems)


def test_the_photos_endpoint_does_not_take_a_detail_parameter():
    """It picks its own level of detail; the caller has no say.

    Pinned because the regression was introduced by a replace that assumed every
    `get_geometry` call sat in a function with a `detail` parameter.
    """
    fn = next(f for f in _handlers() if f.name == "discovery_trail_photos")
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    assert "detail" not in params
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get_geometry"
    ]
    assert calls, "photos endpoint should still fetch geometry"
    for call in calls:
        for kw in call.keywords:
            if kw.arg == "detail":
                assert isinstance(kw.value, ast.Constant), (
                    "photos must pass a literal level of detail, not a variable"
                )

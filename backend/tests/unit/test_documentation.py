"""Keep production definitions and first-party helper classes navigable for engineers."""

import ast
from pathlib import Path


def test_first_party_definitions_have_docstrings():
    """Audit source syntax without importing providers or invoking their resources."""
    backend = Path(__file__).resolve().parents[2]
    missing = []

    def inspect(node, path, parent="", in_class=False, all_functions=True):
        for child in ast.iter_child_nodes(node):
            definition = isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            name = parent + child.name if definition else parent
            is_class = isinstance(child, ast.ClassDef)
            if definition and (all_functions or in_class or is_class):
                if not ast.get_docstring(child):
                    missing.append(f"{path.relative_to(backend)}:{child.lineno} {name}")
            inspect(
                child,
                path,
                name + "." if definition else parent,
                in_class or is_class,
                all_functions,
            )

    for root in ("app", "migrations", "tests"):
        for path in sorted((backend / root).rglob("*.py")):
            inspect(ast.parse(path.read_text()), path, all_functions=root != "tests")
    assert not missing, "Missing first-party documentation:\n" + "\n".join(missing)

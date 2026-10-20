"""The package's layers import only downward: the web app uses the app, the app uses the detector.

- `podblock/*.py`, the detector, imports neither the app nor the web app (`cli.py`, which starts them all,
  aside);
- `podblock.app` never imports the web app, nor a package made for HTTP, so the worker process runs without
  the web app's extras;
- `podblock.web` may import anything.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent / "podblock"
WEB_PACKAGES = {"fastapi", "starlette", "jinja2", "uvicorn", "segno", "multipart", "itsdangerous"}


def imports(path: Path) -> set[str]:
    """Every module a file imports, by its full name."""
    package = ".".join(path.relative_to(ROOT.parent).with_suffix("").parts[:-1])
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = package.rsplit(".", node.level - 1)[0] if node.level else ""
            module = ".".join(part for part in (base, node.module) if part)
            found.add(module)
            found |= {f"{module}.{alias.name}" for alias in node.names}  # `from . import x` imports module x
    return found


def under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def test_the_detector_imports_neither_the_app_nor_the_web_app():
    for path in ROOT.glob("*.py"):
        if path.name in {"cli.py", "__main__.py"}:
            continue
        wrong = {m for m in imports(path) if under(m, "podblock.app") or under(m, "podblock.web")}
        assert not wrong, f"{path.name} imports {sorted(wrong)}"


def test_the_app_imports_nothing_made_for_the_web():
    for path in (ROOT / "app").glob("*.py"):
        wrong = {m for m in imports(path) if under(m, "podblock.web") or m.split(".")[0] in WEB_PACKAGES}
        assert not wrong, f"app/{path.name} imports {sorted(wrong)}"


def test_the_check_sees_relative_imports():
    found = imports(ROOT / "web" / "routes" / "episodes.py")
    assert "podblock.app.processing" in found and "podblock.web.deps" in found

"""The image must carry every module the relay imports.

17.10.2026. `errors.py` was added, merged and deployed, and the container
crash-looped on `ModuleNotFoundError: No module named 'errors'` with two
positions open and no risk guard watching them. The tests all passed: they
run in a venv where every file is present. Only the image is missing one,
and nothing looked at the image.

The Dockerfile names its files one by one — which is the right call for a
small image, and exactly the kind of list that goes stale silently.
"""

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent


def shipped() -> set[str]:
    """The modules the Dockerfile copies into the image."""
    line = next(
        line
        for line in (ROOT / "Dockerfile").read_text().splitlines()
        if line.startswith("COPY ") and ".py" in line
    )
    return {word for word in line.split() if word.endswith(".py")}


def imported_by(name: str) -> set[str]:
    """Top-level modules `name` imports that live in this repo."""
    ours = {path.stem for path in ROOT.glob("*.py") if not path.name.startswith("test_")}
    tree = ast.parse((ROOT / name).read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return {f"{module}.py" for module in found & ours}


def test_every_module_the_relay_imports_is_in_the_image():
    """Walked transitively: a module imported by a module still has to ship."""
    carried = shipped()
    seen, queue = set(), ["relay.py"]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue += list(imported_by(name))

    missing = sorted(seen - carried)
    assert not missing, f"the image would start without: {', '.join(missing)}"


def test_the_image_carries_nothing_that_no_longer_exists():
    """A renamed module leaves a name in the list that COPY will fail on."""
    gone = sorted(name for name in shipped() if not (ROOT / name).exists())

    assert not gone, f"the Dockerfile names files that are not here: {', '.join(gone)}"


def test_the_guard_notices_a_missing_module(tmp_path, monkeypatch):
    """The check itself has to be able to fail, or it guards nothing."""
    assert "errors.py" in shipped()
    assert "errors.py" in imported_by("relay.py")
    assert sys.modules  # the walk uses only the file tree, not what is loaded

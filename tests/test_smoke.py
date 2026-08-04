"""Phase 2a smoke tests — import sanity + README edit verification."""

from pathlib import Path


def test_readme_exists():
    assert Path("README.md").exists(), "README.md must exist"


def test_readme_first_line():
    content = Path("README.md").read_text(encoding="utf-8")
    first = content.splitlines()[0] if content.strip() else ""
    assert first == "# veya", f"README.md first line must be '# veya', got: {first!r}"


def test_config_imports():
    from config.loader import load_config

    cfg = load_config()
    assert isinstance(cfg, dict)


def test_agents_imports():
    from agents import resolve_persona

    p = resolve_persona("build")
    assert p.name == "build"


def test_assembly_smoke():
    import server.assembly  # noqa: F401 — verifies import chain ok

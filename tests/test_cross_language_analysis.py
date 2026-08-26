from veya.oprim.cross_language import Language, create_cross_language_translator


def test_analyze_project_skips_dependency_and_build_trees(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    for excluded in (".venv", "node_modules", "build", "dist", "__pycache__"):
        generated = tmp_path / excluded
        generated.mkdir()
        (generated / "generated.py").write_text("def generated():\n    return 0\n", encoding="utf-8")

    stats = create_cross_language_translator().analyze_project(str(tmp_path))

    assert stats == {Language.PYTHON: {"files": 1, "total_lines": 2}}

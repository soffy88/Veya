import json

from server.routes.artifacts import _artifact_file, _artifact_is_owned_by, _safe_artifact_id


def test_artifact_ids_reject_traversal_and_reserved_store(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    for value in ("..", ".", "../outside", "artifact_links.json", "x/y", "", "../auth.db"):
        assert _safe_artifact_id(value) is False
        assert _artifact_file(value) is None


def test_artifact_owner_manifest_is_required_and_exact(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    artifact = root / "item-1"
    artifact.mkdir(parents=True)
    (artifact / "file.txt").write_text("published")
    monkeypatch.setenv("VEYA_ARTIFACTS_DIR", str(root))
    assert _artifact_file("item-1") == (artifact / "file.txt").resolve()
    assert _artifact_is_owned_by("item-1", "alice") is False
    (artifact / "artifact.json").write_text(json.dumps({"owner_user_id": "alice"}))
    assert _artifact_is_owned_by("item-1", "alice") is True
    assert _artifact_is_owned_by("item-1", "bob") is False


def test_artifact_file_symlink_cannot_escape_root(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("not public")
    try:
        (root / "leak").symlink_to(outside)
    except OSError:
        return
    monkeypatch.setenv("VEYA_ARTIFACTS_DIR", str(root))
    assert _artifact_file("leak") is None

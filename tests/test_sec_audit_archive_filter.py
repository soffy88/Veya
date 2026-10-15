from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_archive_extractors_use_data_filter():
    prepare = (ROOT / "deploy/prepare_hicode_3o_context.py").read_text()
    pins = (ROOT / "scripts/check_3o_canonical_pins.py").read_text()
    assert 'extractall(destination, filter="data")' in prepare
    assert 'extractall(path=str(dest), filter="data")' in pins

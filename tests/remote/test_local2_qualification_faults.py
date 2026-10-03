from __future__ import annotations

import threading

from veya.remote.qualification_faults import QualificationFault, checkpoint, enabled


def test_qualification_faults_are_disabled_without_full_context(monkeypatch) -> None:
    monkeypatch.delenv("VEYA_QUALIFICATION_FAULT_INJECTION", raising=False)
    monkeypatch.delenv("VEYA_QUALIFICATION_RUN_ID", raising=False)
    monkeypatch.delenv("VEYA_QUALIFICATION_CONTROL_DIR", raising=False)
    checkpoint("AFTER_VALIDATE", dispatch_id="not-injected")
    assert enabled() is False


def test_fail_checkpoint_is_out_of_band_and_test_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VEYA_QUALIFICATION_FAULT_INJECTION", "1")
    monkeypatch.setenv("VEYA_QUALIFICATION_RUN_ID", "unit-fail")
    monkeypatch.setenv("VEYA_QUALIFICATION_CONTROL_DIR", str(tmp_path))
    monkeypatch.setenv("VEYA_QUALIFICATION_FAULT_CHECKPOINT", "AFTER_VALIDATE")
    monkeypatch.setenv("VEYA_QUALIFICATION_FAULT_ACTION", "FAIL")

    try:
        checkpoint("AFTER_VALIDATE", dispatch_id="unit-dispatch")
    except QualificationFault as exc:
        assert exc.checkpoint == "AFTER_VALIDATE"
    else:
        raise AssertionError("qualification FAIL hook did not fire")

    assert (tmp_path / "checkpoint.json").exists()


def test_pause_checkpoint_releases_without_product_restart(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VEYA_QUALIFICATION_FAULT_INJECTION", "1")
    monkeypatch.setenv("VEYA_QUALIFICATION_RUN_ID", "unit-pause")
    monkeypatch.setenv("VEYA_QUALIFICATION_CONTROL_DIR", str(tmp_path))
    monkeypatch.setenv("VEYA_QUALIFICATION_FAULT_CHECKPOINT", "AFTER_VALIDATE")
    monkeypatch.setenv("VEYA_QUALIFICATION_FAULT_ACTION", "PAUSE")

    worker = threading.Thread(
        target=checkpoint, args=("AFTER_VALIDATE",), kwargs={"dispatch_id": "unit-dispatch"}
    )
    worker.start()
    assert (tmp_path / "checkpoint.json").exists() or worker.is_alive()
    (tmp_path / "release").touch()
    worker.join(timeout=2)
    assert not worker.is_alive()

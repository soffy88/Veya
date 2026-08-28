from __future__ import annotations

import json

from cli.main import main
from runtime.harness.models import HarnessDoctorReport


def test_harness_doctor_cli_is_routed_without_starting_the_runtime(monkeypatch, tmp_path, capsys):
    report = HarnessDoctorReport(
        status="HARNESS_DEGRADED",
        workspace_path=str(tmp_path),
        checks=[],
        degraded_reasons=["no project guide"],
    )
    calls: list[tuple[str, bool]] = []

    def fake_doctor(path: str, *, unattended: bool = False):
        calls.append((path, unattended))
        return report

    monkeypatch.setattr("cli.harness.run_harness_doctor", fake_doctor)

    assert main(["harness", "doctor", "--path", str(tmp_path), "--unattended", "--json"]) == 0
    assert calls == [(str(tmp_path), True)]
    assert json.loads(capsys.readouterr().out)["status"] == "HARNESS_DEGRADED"

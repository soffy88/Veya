#!/usr/bin/env python3
"""CLI runner for CAD sales simulation trainer (the article's 90-point exam)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.cad_sales.trainer import CadSalesTrainer, format_exam_report


def main() -> int:
    trainer = CadSalesTrainer()
    result = trainer.run_exam()
    report = format_exam_report(result)
    print(report)

    evidence_dir = Path(__file__).resolve().parents[1] / "cadbot_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "simulation_trainer_report.md").write_text(report, encoding="utf-8")
    (evidence_dir / "simulation_trainer_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nSaved evidence to {evidence_dir / 'simulation_trainer_report.md'}")

    return 0 if result["passed_90"] else 1


if __name__ == "__main__":
    sys.exit(main())

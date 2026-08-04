import os
from pathlib import Path

import yaml

from hicode.logging import log_error, log_info, setup_logging


def parse_config(config_path: Path):
    with config_path.open() as f:
        config = yaml.safe_load(f)
    return config


def run_workflow(workflow):
    for job in workflow["jobs"]:
        log_info(f"Running job: {job['name']}")
        for step in job["steps"]:
            log_info(f"  Running step: {step}")


if __name__ == "__main__":
    setup_logging()
    current_dir = Path(os.getcwd())
    config_path = current_dir / "hicode" / "hicode.yml"
    try:
        config = parse_config(config_path)
        for workflow in config["workflows"]:
            run_workflow(workflow)
    except Exception as e:
        log_error(f"Error running CI/CD workflow: {e}")
